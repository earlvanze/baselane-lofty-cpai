#!/usr/bin/env python3
"""Check Hemlane and Baselane for tenant rent deposits.

Integrates with the Hemlane skill's CDP auth capture and GraphQL query scripts.
Runs daily at 8 PM Eastern. Sends Telegram DM if deposits are not detected.

Auth strategy:
1. Try existing captured auth files (.hemlane_auth.json, /tmp/hemlane-auth.json)
2. If stale or missing, run the Hemlane skill's CDP auth capture script
   (capture_hemlane_auth_via_cdp.py) to get fresh headers from Brave
3. Use captured headers to query Hemlane GraphQL for recent transactions
4. Cross-reference with Baselane daily sync report for deposit confirmation
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib import request, error

# Add workspace to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

def default_root() -> Path:
    env_root = os.environ.get("WORKSPACE_ROOT")
    if env_root:
        return Path(env_root)
    return Path(__file__).absolute().parents[1]

ROOT = default_root()
REPORT_DIR = ROOT / "reports"
STATE_FILE = REPORT_DIR / "deposit_check_state.json"
HEMLANE_SKILL_DIR = ROOT / "skills" / "hemlane"
HEMLANE_AUTH_FILE = ROOT / ".hemlane_auth.json"
HEMLANE_AUTH_FALLBACKS = [
    HEMLANE_AUTH_FILE,
    Path("/tmp/hemlane-auth.json"),
    REPORT_DIR / "hemlane_auth.json",
]

# Hemlane GraphQL config
HEMLANE_ENDPOINT = 'https://api.hemlane.com/graphql'
HEMLANE_TRANSACTIONS_QUERY = """
query TransactionsNextCursorQuery($status: TransactionStatus, $sourceType: TransactionSourceType, $pagination: PagedPaginationInput = {page: 1, limit: 50}, $propertyId: ID, $propertyUnitId: ID, $portfolioId: ID, $ownerUserId: ID, $sourceUserId: ID, $sourceTenantGroupId: ID, $paymentCategoryId: ID, $paymentSubcategoryId: ID, $destinationUserId: ID, $dueDateBegin: ISO8601DateTime, $dueDateEnd: ISO8601DateTime, $transactionDateBegin: ISO8601DateTime, $transactionDateEnd: ISO8601DateTime, $postedAtBegin: ISO8601DateTime, $postedAtEnd: ISO8601DateTime, $search: String) {
  transactionsCursor(status: $status, sourceType: $sourceType, pagination: $pagination, propertyId: $propertyId, propertyUnitId: $propertyUnitId, portfolioId: $portfolioId, ownerUserId: $ownerUserId, sourceUserId: $sourceUserId, sourceTenantGroupId: $sourceTenantGroupId, paymentCategoryId: $paymentCategoryId, paymentSubcategoryId: $paymentSubcategoryId, destinationUserId: $destinationUserId, dueDateBegin: $dueDateBegin, dueDateEnd: $dueDateEnd, transactionDateBegin: $transactionDateBegin, transactionDateEnd: $transactionDateEnd, postedAtBegin: $postedAtBegin, postedAtEnd: $postedAtEnd, search: $search) {
    pageInfo { page hasNextPage hasPreviousPage __typename }
    data {
      id amount status postedAt transactionDate dueDate
      property { id nickname addressStreet __typename }
      propertyUnit { id unitNumber nicknameWithUnit __typename }
      paymentCategory { id label __typename }
      paymentSubcategory { id label __typename }
      sourceUser { id fullName __typename }
      sourceTenantGroup { id status __typename }
      destinationUser { id fullName __typename }
      __typename
    }
    __typename
  }
}
"""

# 25 Circle Dr property identifiers
PROPERTY_KEYWORDS = ["25 circle", "circle dr", "dixmoor"]

def load_env_file(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    return env

def telegram_config() -> tuple[str | None, str | None]:
    """Load Telegram bot token and chat ID from OpenClaw config."""
    config_path = ROOT.parent / "openclaw.json"
    token = None
    chat_id = None

    # Try environment first
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("OPENCLAW_BOTTOKEN")
    chat_id = os.environ.get("BASELANE_EOD_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")

    if token and chat_id:
        return token, chat_id

    # Try config file
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text())
            telegram_cfg = config.get("channels", {}).get("telegram", {})
            env = {**load_env_file(ROOT.parent / ".env"), **os.environ}
            if not token:
                ref = telegram_cfg.get("botToken")
                if ref:
                    if ref.startswith("${") and ref.endswith("}"):
                        env_key = ref[2:-1]
                        token = env.get(env_key)
                    else:
                        token = ref
            if not chat_id:
                allow = telegram_cfg.get("allowFrom") or telegram_cfg.get("groupAllowFrom") or []
                if allow:
                    chat_id = str(allow[0])
        except Exception:
            pass

    return token, chat_id

def send_telegram_dm(message: str, token: str, chat_id: str) -> bool:
    """Send a Telegram DM."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        req = request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except Exception as e:
        print(f"Failed to send Telegram message: {e}")
        return False

def find_hemlane_auth() -> Path | None:
    """Find an existing Hemlane auth file."""
    for path in HEMLANE_AUTH_FALLBACKS:
        if path.exists():
            # Check if it's recent enough (within 12 hours)
            mtime = datetime.fromtimestamp(path.stat().st_mtime)
            age = datetime.now() - mtime
            if age < timedelta(hours=12):
                print(f"Found fresh Hemlane auth at {path} (age: {age})")
                return path
            else:
                print(f"Hemlane auth at {path} is stale (age: {age})")
    return None

def capture_hemlane_auth_via_skill() -> Path | None:
    """Capture fresh Hemlane auth using the Hemlane skill's CDP script."""
    capture_script = HEMLANE_SKILL_DIR / "scripts" / "capture_hemlane_auth_via_cdp.py"
    if not capture_script.exists():
        print(f"Hemlane CDP capture script not found: {capture_script}")
        return None

    auth_out = HEMLANE_AUTH_FILE
    print(f"Capturing fresh Hemlane auth via CDP skill script...")

    try:
        result = subprocess.run(
            ["python3", str(capture_script),
             "--endpoint-kind", "get-transactions",
             "--out-file", str(auth_out)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0 and auth_out.exists():
            print(f"Fresh Hemlane auth captured to {auth_out}")
            return auth_out
        else:
            print(f"CDP auth capture failed: {result.stderr[:500]}")
            # Try MCP server as fallback
            return capture_hemlane_auth_via_mcp()
    except subprocess.TimeoutExpired:
        print("CDP auth capture timed out")
        return capture_hemlane_auth_via_mcp()
    except Exception as e:
        print(f"CDP auth capture error: {e}")
        return capture_hemlane_auth_via_mcp()

def capture_hemlane_auth_via_mcp() -> Path | None:
    """Fallback: use the Hemlane MCP server to capture auth."""
    print("Trying Hemlane MCP server for auth capture...")
    auth_out = Path("/tmp/hemlane-auth.json")
    try:
        # The MCP server's capture_auth tool is available via the gateway
        # In cron context, we call the Python script directly
        capture_script = HEMLANE_SKILL_DIR / "scripts" / "capture_hemlane_auth_via_cdp.py"
        if capture_script.exists():
            result = subprocess.run(
                ["python3", str(capture_script),
                 "--endpoint-kind", "get-transactions",
                 "--out-file", str(auth_out)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0 and auth_out.exists():
                print(f"Hemlane auth captured via fallback to {auth_out}")
                return auth_out
            print(f"MCP fallback capture failed: {result.stderr[:500]}")
    except Exception as e:
        print(f"MCP fallback error: {e}")
    return None

def load_hemlane_auth() -> dict | None:
    """Load Hemlane auth headers, capturing fresh if needed."""
    auth_path = find_hemlane_auth()
    if not auth_path:
        auth_path = capture_hemlane_auth_via_skill()
    if not auth_path or not auth_path.exists():
        print("No Hemlane auth available - skipping Hemlane check")
        return None

    try:
        data = json.loads(auth_path.read_text())
        headers = data.get("headers", data) if isinstance(data, dict) else {}
        out = {}
        for k, v in headers.items():
            if v is None:
                continue
            out[k] = str(v)
        out.setdefault("content-type", "application/json")
        out.setdefault("accept", "application/json")
        return out
    except Exception as e:
        print(f"Failed to load Hemlane auth: {e}")
        return None

def gql_query(headers: dict, query: str, variables: dict, operation_name: str) -> dict | None:
    """Execute a GraphQL query against Hemlane API."""
    payload = {"query": query, "variables": variables, "operationName": operation_name}
    req = request.Request(
        HEMLANE_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST"
    )
    try:
        with request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
        if data.get("errors"):
            print(f"GraphQL errors: {data['errors']}")
            return None
        return data.get("data")
    except error.HTTPError as e:
        print(f"HTTP error {e.code}: {e.read().decode()[:500]}")
        return None
    except Exception as e:
        print(f"Request failed: {e}")
        return None

def check_hemlane_deposits(days_back: int = 3) -> list[dict]:
    """Check Hemlane for recent deposits (incoming payments) for 25 Circle Dr."""
    headers = load_hemlane_auth()
    if not headers:
        return []

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)

    variables = {
        "pagination": {"page": 1, "limit": 50},
        "postedAtBegin": start_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "postedAtEnd": end_date.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "status": "Completed"
    }

    data = gql_query(headers, HEMLANE_TRANSACTIONS_QUERY, variables, "TransactionsNextCursorQuery")
    if not data:
        return []

    cursor_data = data.get("transactionsCursor", {})
    transactions = cursor_data.get("data", [])

    deposits = []
    for tx in transactions:
        amount = tx.get("amount")
        if amount is None or float(amount) <= 0:
            continue

        prop = tx.get("property", {})
        prop_nickname = (prop.get("nickname") or "").lower()
        prop_address = (prop.get("addressStreet") or "").lower()

        is_target = any(kw in prop_nickname or prop_address for kw in PROPERTY_KEYWORDS)

        if is_target:
            deposits.append({
                "source": "hemlane",
                "id": tx.get("id"),
                "amount": float(amount),
                "posted_at": tx.get("postedAt"),
                "status": tx.get("status"),
                "property": prop.get("nickname", "Unknown"),
                "category": tx.get("paymentCategory", {}).get("label", "Uncategorized"),
            })

    return deposits

def check_baselane_deposits() -> list[dict]:
    """Check Baselane for recent deposits via existing daily sync report."""
    baselane_report = REPORT_DIR / "baselane_daily_sync_report.json"
    if not baselane_report.exists():
        print("No Baselane daily sync report found - skipping Baselane check")
        return []

    try:
        data = json.loads(baselane_report.read_text())
        deposits = []

        for tx in data.get("transactions", []):
            amount_str = tx.get("amount", "0")
            try:
                amount = float(str(amount_str).replace(",", "").replace("$", ""))
            except ValueError:
                continue

            if amount <= 0:
                continue

            desc = (tx.get("description") or "").lower()
            is_likely_deposit = any(kw in desc for kw in ["rent", "hemlane", "deposit", "payment"])

            if is_likely_deposit:
                deposits.append({
                    "source": "baselane",
                    "id": tx.get("transaction_id", "unknown"),
                    "amount": amount,
                    "posted_at": tx.get("date"),
                    "description": tx.get("description", "Unknown"),
                    "property": "25 Circle Dr",
                })

        return deposits
    except Exception as e:
        print(f"Failed to parse Baselane report: {e}")
        return []

def load_state(state_file: Path = None) -> dict:
    """Load the last check state."""
    sf = state_file or STATE_FILE
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except Exception:
            pass
    return {"last_alert_sent": None, "deposits_found_last_check": [], "first_run_date": None}

def save_state(state: dict, state_file: Path = None):
    """Save the current check state."""
    sf = state_file or STATE_FILE
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state, indent=2))

def is_first_run_of_month(state: dict) -> bool:
    """Determine if this is the first run of the current month."""
    now = datetime.now()
    current_month = now.strftime("%Y-%m")
    last_month = (state.get("first_run_date") or "")[:7]
    if last_month != current_month:
        state["first_run_date"] = now.isoformat()
        return True
    return False

def main():
    parser = argparse.ArgumentParser(description="Check Hemlane and Baselane for deposits")
    parser.add_argument("--dry-run", action="store_true", help="Don't send Telegram alerts")
    parser.add_argument("--force-alert", action="store_true", help="Send alert even if deposits found (for testing)")
    parser.add_argument("--output", default=str(REPORT_DIR / "check_deposits_report.json"))
    parser.add_argument("--state-file", default=str(STATE_FILE))
    args = parser.parse_args()

    state_file = Path(args.state_file)

    print(f"Deposit check started at {datetime.now().isoformat()}")

    state = load_state(state_file)
    first_run = is_first_run_of_month(state)

    # Check both platforms
    hemlane_deposits = check_hemlane_deposits(days_back=3)
    baselane_deposits = check_baselane_deposits()

    all_deposits = hemlane_deposits + baselane_deposits

    print(f"Found {len(hemlane_deposits)} deposits in Hemlane")
    print(f"Found {len(baselane_deposits)} deposits in Baselane")
    print(f"Total deposits: {len(all_deposits)}")

    for dep in all_deposits:
        print(f"  - {dep['source']}: ${dep['amount']:.2f} ({dep.get('property', 'Unknown')})")

    deposits_found = len(all_deposits) > 0
    should_alert = args.force_alert or (not deposits_found and first_run)

    # Avoid duplicate alerts within 24 hours
    if should_alert and not args.dry_run:
        last_alert = state.get("last_alert_sent")
        if last_alert:
            try:
                last_alert_time = datetime.fromisoformat(last_alert)
                if datetime.now() - last_alert_time < timedelta(hours=24):
                    print("Alert already sent within last 24 hours - skipping")
                    should_alert = False
            except Exception:
                pass

    if should_alert and not args.dry_run:
        token, chat_id = telegram_config()
        if not token or not chat_id:
            print("ERROR: Telegram credentials not configured")
            sys.exit(1)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z")
        if not deposits_found:
            message = f"""<b>⚠️ Deposit Alert: 25 Circle Dr</b>

No deposits detected as of {now_str}.

Checked:
• Hemlane: {len(hemlane_deposits)} deposits
• Baselane: {len(baselane_deposits)} deposits

Please verify rent collection status."""
        else:
            lines = []
            for d in all_deposits:
                lines.append(f"• {d['source'].title()}: ${d['amount']:.2f} ({d.get('property', 'Unknown')})")
            message = f"""<b>🏠 Deposit Check: 25 Circle Dr</b>

{len(all_deposits)} deposit(s) found as of {now_str}:

{chr(10).join(lines)}

Checked Hemlane + Baselane."""

        print("Sending Telegram alert...")
        if send_telegram_dm(message, token, chat_id):
            print("Alert sent successfully")
            state["last_alert_sent"] = datetime.now().isoformat()
        else:
            print("Failed to send alert")
            sys.exit(1)
    elif args.dry_run:
        print("DRY RUN: Would send alert" if should_alert else "DRY RUN: No alert needed")
    else:
        print("Deposits found - no alert needed")

    # Write report
    report = {
        "timestamp": datetime.now().isoformat(),
        "is_first_run": first_run,
        "deposits_found": len(all_deposits),
        "hemlane_deposits": hemlane_deposits,
        "baselane_deposits": baselane_deposits,
        "unpaid_properties": [] if deposits_found else [{"property_name": "25 Circle Dr", "address": "25 Circle Dr, Dixmoor, IL 60426", "expected_amount": "787.95", "due_date": datetime.now().strftime("%Y-%m-%d")}],
        "alert_sent": should_alert and not args.dry_run,
    }
    Path(args.output).write_text(json.dumps(report, indent=2))

    # Update state
    state["deposits_found_last_check"] = all_deposits
    state["last_check_time"] = datetime.now().isoformat()
    save_state(state)

    print(f"Deposit check completed at {datetime.now().isoformat()}")

if __name__ == "__main__":
    main()