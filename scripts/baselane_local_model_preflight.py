#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "ollama-cyber/qwen3.5:35b-a3b"
BASELANE_FINANCE_CONTRACT_EXPECTED = '{"category":"Rents","column_e_sum":177679.32,"ok":true}'
BASELANE_FINANCE_CONTRACT_PROMPT = (
    "Copy the following precomputed, externally validated JSON exactly and output nothing else:\n"
    f"{BASELANE_FINANCE_CONTRACT_EXPECTED}"
)
MODEL_ALLOWED_USES = [
    "exact-response liveness smoke",
    "exact-response Baselane finance formatting contract smoke",
    "deterministic formatting of precomputed report statuses only",
]
MODEL_ALLOWED_TASK_CLASS = "schema_checked_precomputed_status_formatting"
MODEL_FORBIDDEN_USES = [
    "calculating ledger balances",
    "classifying live transactions",
    "writing Baselane, Dropbox, Lofty, Discord, Telegram, or email outputs",
    "approving owner emails or live listing updates",
    "overriding Python/CSV/worksheet financial truth",
]
MODEL_EXECUTION_POLICY = {
    "mode": "fail_closed_exact_copy_contract_gated",
    "financial_truth_source": "Python/CSV/worksheet deterministic pipeline",
    "pipeline_execution_allowed": False,
    "model_financial_authority": False,
    "autonomous_financial_execution_allowed": False,
    "live_side_effects_allowed": False,
    "permitted_task_class": MODEL_ALLOWED_TASK_CLASS,
    "requires_external_deterministic_validation": True,
    "allowed_after_preflight": MODEL_ALLOWED_USES,
    "forbidden_even_after_preflight": MODEL_FORBIDDEN_USES,
}
DEFAULT_FALLBACK_MODELS: list[str] = []
DEFAULT_FALLBACK_BASE_URLS = [
    "http://host.docker.internal:11434",
    "http://127.0.0.1:11434",
    "http://127.0.0.1:11439",
    "http://100.115.208.70:11438",
    "http://100.115.208.70:11434",
    "http://127.0.0.1:11436",
]
DEFAULT_OPENCLAW_ROOT = Path(os.environ.get("OPENCLAW_ROOT", str(Path.home() / ".openclaw")))
DEFAULT_CONFIG = Path(os.environ.get("OPENCLAW_CONFIG_PATH", str(DEFAULT_OPENCLAW_ROOT / "openclaw.json")))
DEFAULT_REPORT = Path(os.environ.get("WORKSPACE_ROOT", str(DEFAULT_OPENCLAW_ROOT / "workspace"))) / "reports" / "baselane_local_model_preflight_report.json"
DEFAULT_MODEL_LOCK = Path(os.environ.get("BASELANE_OLLAMA_MODEL_LOCK_PATH", str(DEFAULT_OPENCLAW_ROOT / "workspace" / "config" / "ollama-model-lock.json")))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def age_hours(value: object) -> float | None:
    parsed = parse_iso(value)
    if not parsed:
        return None
    return (datetime.now(timezone.utc) - parsed).total_seconds() / 3600


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_env_defaults(paths: list[Path]) -> list[str]:
    loaded: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key or ""):
                continue
            if os.environ.get(key):
                continue
            value = value.strip().strip('"').strip("'")
            os.environ[key] = value
            loaded.append(key)
    return loaded


def provider_model(model: str) -> tuple[str, str]:
    if "/" not in model:
        return "", model
    return model.split("/", 1)


def provider_config_from_data(data: dict[str, Any], model: str) -> dict[str, Any]:
    provider, model_id = provider_model(model)
    providers = {}
    if isinstance(data, dict):
        providers = ((data.get("models") or {}).get("providers") or {}) or (data.get("providers") or {})
    cfg = providers.get(provider) or {}
    models = cfg.get("models") or []
    model_cfg = next((m for m in models if isinstance(m, dict) and m.get("id") == model_id), None)
    return {"provider": provider, "model_id": model_id, "provider_config": cfg, "model_config": model_cfg}


def provider_config(config_path: Path, model: str, agent: str = "baselane-cron-lite") -> dict[str, Any]:
    primary = provider_config_from_data(load_json(config_path), model)
    primary["config_source"] = "openclaw_config"
    primary["config_path"] = str(config_path)
    if primary.get("provider_config") and primary.get("model_config"):
        return primary
    fallback_path = DEFAULT_OPENCLAW_ROOT / "agents" / agent / "agent" / "models.json"
    fallback = provider_config_from_data(load_json(fallback_path), model)
    if fallback.get("provider_config") and fallback.get("model_config"):
        fallback["config_source"] = "agent_models"
        fallback["config_path"] = str(fallback_path)
        fallback["primary_config_path"] = str(config_path)
        return fallback
    return primary


def tcp_check(base_url: str, timeout: float) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    result = {"host": host, "port": port, "ok": False, "error": None}
    if not host:
        result["error"] = f"invalid baseUrl: {base_url}"
        return result
    try:
        with socket.create_connection((host, port), timeout=timeout):
            result["ok"] = True
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{exc.__class__.__name__}: {exc}"
    return result


def http_get_json(url: str, timeout: float) -> dict[str, Any]:
    result = {"url": url, "ok": False, "status": None, "error": None, "json": None}
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - local configured endpoint health check
            result["status"] = response.status
            raw = response.read(512 * 1024).decode("utf-8", errors="replace")
        try:
            result["json"] = json.loads(raw)
        except json.JSONDecodeError:
            result["json"] = {"raw_prefix": raw[:500]}
        result["ok"] = 200 <= int(result["status"] or 0) < 300
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{exc.__class__.__name__}: {exc}"
    return result


def openclaw_agent_smoke(agent: str, model: str, timeout_seconds: int) -> dict[str, Any]:
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        agent,
        "--session-key",
        f"agent:{agent}:baselane-local-model-preflight",
        "--model",
        model,
        "--thinking",
        "off",
        "--timeout",
        str(timeout_seconds),
        "--json",
        "--message",
        "Reply exactly: BASELANE_MODEL_OK",
    ]
    start = time.time()
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 30)
        stdout = completed.stdout[-4000:]
        stderr = completed.stderr[-4000:]
        text = stdout + "\n" + stderr
        return {
            "attempted": True,
            "ok": completed.returncode == 0 and "BASELANE_MODEL_OK" in text,
            "return_code": completed.returncode,
            "duration_seconds": round(time.time() - start, 3),
            "stdout_tail": stdout,
            "stderr_tail": stderr,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "attempted": True,
            "ok": False,
            "return_code": None,
            "duration_seconds": round(time.time() - start, 3),
            "error": f"{exc.__class__.__name__}: {exc}",
        }


def resolve_api_key(provider_cfg: dict[str, Any]) -> str:
    raw = str(provider_cfg.get("apiKey") or provider_cfg.get("api_key") or "").strip()
    if not raw:
        return ""
    env_name = raw
    if raw.startswith("${") and raw.endswith("}"):
        env_name = raw[2:-1].strip()
    if env_name and re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
        return os.environ.get(env_name, "")
    return raw


def direct_ollama_smoke(
    base_url: str,
    model_id: str,
    timeout: float,
    num_ctx: int,
    api_key: str = "",
    num_predict: int = 32,
) -> dict[str, Any]:
    api_base = base_url.rstrip("/").removesuffix("/v1")
    url = api_base + "/api/chat"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Return only BASELANE_MODEL_OK. No reasoning."}],
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "options": {"num_predict": int(num_predict), "temperature": 0, "num_ctx": int(num_ctx), "think": False},
    }
    start = time.time()
    result: dict[str, Any] = {"attempted": True, "ok": False, "url": url, "num_ctx": int(num_ctx)}
    try:
        headers = ["Content-Type: application/json"]
        if api_key:
            headers.append(f"Authorization: Bearer {api_key}")
        config_lines = [
            f'url = "{url}"',
            'request = "POST"',
            "silent",
            "show-error",
            f"max-time = {max(float(timeout), 1.0)}",
            "write-out = \"\\n%{http_code}\"",
            "data-binary = @-",
        ]
        for header in headers:
            config_lines.append(f'header = "{header}"')
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as config_handle:
            config_path = Path(config_handle.name)
            config_handle.write("\n".join(config_lines))
            config_handle.write("\n")
        try:
            completed = subprocess.run(
                ["curl", "--config", str(config_path)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=max(float(timeout) + 5.0, 6.0),
                check=False,
            )
        finally:
            config_path.unlink(missing_ok=True)
        stdout = completed.stdout or ""
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            error_code = "smoke_timeout" if completed.returncode == 28 else "curl_error"
            result.update(
                {
                    "duration_seconds": round(time.time() - start, 3),
                    "return_code": completed.returncode,
                    "error": stderr or f"curl exited {completed.returncode}",
                    "error_code": error_code,
                    "error_detail": (
                        f"{model_id} did not return a deterministic response within {timeout}s; "
                        "warm the model, lower the cron model size, or enable a faster local endpoint"
                    )
                    if error_code == "smoke_timeout"
                    else stderr[:500],
                }
            )
            return result
        if "\n" not in stdout:
            raise ValueError("curl output missing HTTP status trailer")
        raw, status_text = stdout.rsplit("\n", 1)
        try:
            http_status = int(status_text.strip())
        except ValueError as exc:
            raise ValueError(f"curl output invalid HTTP status trailer: {status_text!r}") from exc
        if http_status >= 400:
            error_code = None
            error_detail = None
            try:
                error_payload = json.loads(raw)
                if isinstance(error_payload, dict):
                    error_code = error_payload.get("error")
                    error_detail = error_payload.get("detail") or error_payload.get("message")
            except json.JSONDecodeError:
                error_detail = raw[:500]
            result.update(
                {
                    "duration_seconds": round(time.time() - start, 3),
                    "http_status": http_status,
                    "error": f"HTTP Error {http_status}",
                    "error_code": error_code,
                    "error_detail": error_detail,
                }
            )
            return result
        data = json.loads(raw)
        content = ((data.get("message") or {}).get("content") or data.get("response") or "").strip()
        result.update(
            {
                "ok": content == "BASELANE_MODEL_OK",
                "duration_seconds": round(time.time() - start, 3),
                "http_status": http_status,
                "response": content[:200],
                "done_reason": data.get("done_reason"),
            }
        )
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        result.update(
            {
                "duration_seconds": round(time.time() - start, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
                "error_code": "smoke_timeout",
                "error_detail": (
                    f"{model_id} did not return a deterministic response within {timeout}s; "
                    "warm the model, lower the cron model size, or enable a faster local endpoint"
                ),
            }
        )
    except Exception as exc:  # noqa: BLE001
        result.update({"duration_seconds": round(time.time() - start, 3), "error": f"{exc.__class__.__name__}: {exc}"})
    return result


def direct_ollama_finance_contract_smoke(
    base_url: str,
    model_id: str,
    timeout: float,
    num_ctx: int,
    api_key: str = "",
    num_predict: int = 80,
) -> dict[str, Any]:
    api_base = base_url.rstrip("/").removesuffix("/v1")
    url = api_base + "/api/chat"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": BASELANE_FINANCE_CONTRACT_PROMPT}],
        "stream": False,
        "think": False,
        "keep_alive": "5m",
        "options": {"num_predict": int(num_predict), "temperature": 0, "num_ctx": int(num_ctx), "think": False},
    }
    start = time.time()
    result: dict[str, Any] = {
        "attempted": True,
        "ok": False,
        "url": url,
        "num_ctx": int(num_ctx),
        "num_predict": int(num_predict),
        "expected_response": BASELANE_FINANCE_CONTRACT_EXPECTED,
    }
    try:
        headers = ["Content-Type: application/json"]
        if api_key:
            headers.append(f"Authorization: Bearer {api_key}")
        config_lines = [
            f'url = "{url}"',
            'request = "POST"',
            "silent",
            "show-error",
            f"max-time = {max(float(timeout), 1.0)}",
            "write-out = \"\\n%{http_code}\"",
            "data-binary = @-",
        ]
        for header in headers:
            config_lines.append(f'header = "{header}"')
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as config_handle:
            config_path = Path(config_handle.name)
            config_handle.write("\n".join(config_lines))
            config_handle.write("\n")
        try:
            completed = subprocess.run(
                ["curl", "--config", str(config_path)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                timeout=max(float(timeout) + 5.0, 6.0),
                check=False,
            )
        finally:
            config_path.unlink(missing_ok=True)
        stdout = completed.stdout or ""
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0:
            result.update(
                {
                    "duration_seconds": round(time.time() - start, 3),
                    "return_code": completed.returncode,
                    "error": stderr or f"curl exited {completed.returncode}",
                    "error_code": "finance_contract_timeout" if completed.returncode == 28 else "curl_error",
                }
            )
            return result
        if "\n" not in stdout:
            raise ValueError("curl output missing HTTP status trailer")
        raw, status_text = stdout.rsplit("\n", 1)
        http_status = int(status_text.strip())
        if http_status >= 400:
            error_detail = raw[:500]
            try:
                error_payload = json.loads(raw)
                if isinstance(error_payload, dict):
                    error_detail = error_payload.get("detail") or error_payload.get("message") or error_payload.get("error") or error_detail
            except json.JSONDecodeError:
                pass
            result.update(
                {
                    "duration_seconds": round(time.time() - start, 3),
                    "http_status": http_status,
                    "error": f"HTTP Error {http_status}",
                    "error_detail": error_detail,
                }
            )
            return result
        data = json.loads(raw)
        content = ((data.get("message") or {}).get("content") or data.get("response") or "").strip()
        result.update(
            {
                "ok": content == BASELANE_FINANCE_CONTRACT_EXPECTED,
                "duration_seconds": round(time.time() - start, 3),
                "http_status": http_status,
                "response": content[:300],
                "done_reason": data.get("done_reason"),
            }
        )
    except (subprocess.TimeoutExpired, TimeoutError) as exc:
        result.update(
            {
                "duration_seconds": round(time.time() - start, 3),
                "error": f"{exc.__class__.__name__}: {exc}",
                "error_code": "finance_contract_timeout",
            }
        )
    except Exception as exc:  # noqa: BLE001
        result.update({"duration_seconds": round(time.time() - start, 3), "error": f"{exc.__class__.__name__}: {exc}"})
    return result


def stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def validation_contract(report: dict[str, Any]) -> dict[str, Any]:
    direct_smoke = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    finance_smoke = report.get("finance_contract_smoke") if isinstance(report.get("finance_contract_smoke"), dict) else {}
    checks = {
        "configured_model_present": report.get("configured_model_present") is True,
        "selected_endpoint_from_config": report.get("selected_endpoint_from_config") is True,
        "model_available": report.get("model_available") is True,
        "model_scope_deterministic": ((report.get("model_execution_scope") or {}).get("deterministic_only") is True),
        "model_pipeline_execution_denied": ((report.get("model_execution_scope") or {}).get("pipeline_execution_allowed") is False),
        "model_allowed_task_class_limited": ((report.get("model_execution_scope") or {}).get("allowed_task_class") == MODEL_ALLOWED_TASK_CLASS),
        "model_financial_authority_denied": ((report.get("model_execution_scope") or {}).get("model_financial_authority") is False),
        "model_live_side_effects_denied": ((report.get("model_execution_scope") or {}).get("live_side_effects_allowed") is False),
        "model_external_validation_required": (
            (report.get("model_execution_scope") or {}).get("requires_external_deterministic_validation") is True
        ),
        "direct_smoke_attempted": direct_smoke.get("attempted") is True,
        "direct_smoke_ok": direct_smoke.get("ok") is True,
        "direct_smoke_response_exact": direct_smoke.get("response") == "BASELANE_MODEL_OK",
        "finance_contract_smoke_attempted": finance_smoke.get("attempted") is True,
        "finance_contract_smoke_ok": finance_smoke.get("ok") is True,
        "finance_contract_response_exact": finance_smoke.get("response") == BASELANE_FINANCE_CONTRACT_EXPECTED,
    }
    contract = {
        "model": report.get("model"),
        "expected_model": DEFAULT_MODEL,
        "provider": report.get("provider"),
        "model_id": report.get("model_id"),
        "base_url": report.get("base_url"),
        "model_execution_scope": report.get("model_execution_scope"),
        **checks,
        "direct_smoke_response": direct_smoke.get("response"),
        "direct_smoke_done_reason": direct_smoke.get("done_reason"),
        "direct_smoke_num_ctx": direct_smoke.get("num_ctx"),
        "direct_smoke_num_predict": report.get("direct_smoke_num_predict"),
        "finance_contract_expected_response": BASELANE_FINANCE_CONTRACT_EXPECTED,
        "finance_contract_response": finance_smoke.get("response"),
        "finance_contract_done_reason": finance_smoke.get("done_reason"),
        "finance_contract_num_ctx": finance_smoke.get("num_ctx"),
        "finance_contract_num_predict": finance_smoke.get("num_predict"),
    }
    return {"contract": contract, "digest": stable_digest(contract), "checks": checks}


def local_model_blocker(report: dict[str, Any]) -> dict[str, Any] | None:
    direct_smoke = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    error_code = str(direct_smoke.get("error_code") or "").strip()
    if report.get("model_available") is not True:
        return {
            "active": True,
            "code": "configured_model_unavailable",
            "summary": f"{report.get('model_id')} is not listed on the selected Ollama endpoint",
            "model": report.get("model"),
            "model_id": report.get("model_id"),
            "base_url": report.get("base_url"),
            "selected_endpoint_source": report.get("selected_endpoint_source"),
            "model_lock_base_url": report.get("model_lock_base_url"),
            "model_lock_endpoint_reachable": report.get("model_lock_endpoint_reachable"),
            "model_lock_model_available": report.get("model_lock_model_available"),
            "fallback_smoke_ok": report.get("fallback_smoke_ok"),
            "operational_model_id": report.get("operational_model_id"),
            "action": (
                f"start or expose Ollama with {report.get('model_id')} at the locked Cyber endpoint; "
                "fallback qwen proves local model plumbing only, not the configured Baselane validation contract"
            ),
        }
    if error_code == "model_disabled":
        model_id = str(report.get("model_id") or "").strip()
        base_url = str(report.get("base_url") or "").strip()
        return {
            "active": True,
            "code": "ollama_model_disabled",
            "summary": f"{model_id} is disabled by the upstream Ollama dashboard",
            "model": report.get("model"),
            "model_id": model_id,
            "base_url": base_url,
            "http_status": direct_smoke.get("http_status"),
            "error_code": error_code,
            "error_detail": direct_smoke.get("error_detail"),
            "action": (
                f"enable {model_id} in Ollama dashboard for {base_url}; "
                "rerun scripts/baselane_local_model_preflight.py"
            ),
        }
    if direct_smoke.get("attempted") and not direct_smoke.get("ok"):
        return {
            "active": True,
            "code": error_code or "direct_smoke_failed",
            "summary": "direct local-model smoke failed",
            "model": report.get("model"),
            "model_id": report.get("model_id"),
            "base_url": report.get("base_url"),
            "http_status": direct_smoke.get("http_status"),
            "error_code": error_code or None,
            "error_detail": direct_smoke.get("error_detail") or direct_smoke.get("error"),
            "action": "fix the local Ollama endpoint/model smoke, then rerun scripts/baselane_local_model_preflight.py",
        }
    if not direct_smoke.get("attempted"):
        return {
            "active": True,
            "code": "direct_smoke_not_attempted",
            "summary": f"direct local-model smoke not attempted: {direct_smoke.get('skipped') or 'unknown'}",
            "model": report.get("model"),
            "model_id": report.get("model_id"),
            "base_url": report.get("base_url"),
            "action": "restore endpoint/model availability, then rerun scripts/baselane_local_model_preflight.py",
        }
    finance_smoke = report.get("finance_contract_smoke") if isinstance(report.get("finance_contract_smoke"), dict) else {}
    if finance_smoke.get("attempted") and not finance_smoke.get("ok"):
        return {
            "active": True,
            "code": "finance_contract_smoke_failed",
            "summary": "local model failed the deterministic Baselane finance formatting contract",
            "model": report.get("model"),
            "model_id": report.get("model_id"),
            "base_url": report.get("base_url"),
            "response": finance_smoke.get("response"),
            "expected_response": BASELANE_FINANCE_CONTRACT_EXPECTED,
            "action": "keep this model out of the Baselane cron path until the exact finance formatting contract passes, then rerun scripts/baselane_local_model_preflight.py",
        }
    if not finance_smoke.get("attempted"):
        return {
            "active": True,
            "code": "finance_contract_smoke_not_attempted",
            "summary": f"deterministic Baselane finance formatting contract not attempted: {finance_smoke.get('skipped') or 'unknown'}",
            "model": report.get("model"),
            "model_id": report.get("model_id"),
            "base_url": report.get("base_url"),
            "action": "run the deterministic finance formatting contract smoke before accepting this model for Baselane cron work",
        }
    return None


def should_preserve_prior_precise_timeout(report: dict[str, Any], prior: dict[str, Any]) -> bool:
    max_age = float(os.environ.get("BASELANE_PREFLIGHT_PRESERVE_TIMEOUT_MAX_AGE_HOURS", "2"))
    prior_age = age_hours(prior.get("generated_at"))
    if prior_age is None or prior_age > max_age:
        return False
    if prior.get("model") != report.get("model") or prior.get("model_id") != report.get("model_id"):
        return False
    if prior.get("model_available") is not True:
        return False
    prior_smoke = prior.get("direct_smoke") if isinstance(prior.get("direct_smoke"), dict) else {}
    if prior_smoke.get("error_code") != "smoke_timeout":
        return False
    current_smoke = report.get("direct_smoke") if isinstance(report.get("direct_smoke"), dict) else {}
    if current_smoke.get("attempted") is not False:
        return False
    issues = [str(issue) for issue in report.get("issues") or []]
    return any("endpoint tags check failed" in issue or "endpoint unreachable" in issue for issue in issues)


def preserve_prior_precise_timeout(report: dict[str, Any], report_path: Path) -> dict[str, Any]:
    prior = load_json(report_path)
    if not should_preserve_prior_precise_timeout(report, prior):
        return report
    preserved = dict(prior)
    suppressed = {
        "suppressed_at": now_iso(),
        "reason": "transient_endpoint_probe_failed_after_recent_precise_smoke_timeout",
        "issues": report.get("issues") or [],
        "endpoint_attempts": report.get("endpoint_attempts") or [],
    }
    preserved["last_suppressed_refresh"] = suppressed
    preserved["suppressed_refresh_count"] = int(preserved.get("suppressed_refresh_count") or 0) + 1
    return preserved


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = (value or "").strip()
        if not value or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


def csv_values(value: str) -> list[str]:
    return unique_values([part.strip() for part in str(value or "").split(",")])


def model_lock_base_url(path: Path | None = None) -> str:
    data = load_json(path or DEFAULT_MODEL_LOCK)
    endpoint = str(data.get("endpoint") or "").strip()
    if endpoint:
        return endpoint
    return ""


def model_lock_active_model(path: Path | None = None) -> str:
    data = load_json(path or DEFAULT_MODEL_LOCK)
    active_model = str(data.get("active_model") or "").strip()
    return active_model


def local_companion_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url)
    port = parsed.port
    if not port or parsed.hostname in {"127.0.0.1", "localhost"}:
        return ""
    return f"{parsed.scheme or 'http'}://127.0.0.1:{port}"


def endpoint_candidates(config_base_url: str, override_base_url: str | None) -> list[dict[str, Any]]:
    lock_base_url = model_lock_base_url()
    ordered = [
        ("override", override_base_url or ""),
        ("env", os.environ.get("BASELANE_PREFLIGHT_BASE_URL", "")),
        ("env", os.environ.get("OLLAMA_CYBER_BASE_URL", "")),
        ("model_lock", lock_base_url),
        ("model_lock_local_companion", local_companion_base_url(lock_base_url)),
        ("config", config_base_url),
    ] + [("fallback", value) for value in DEFAULT_FALLBACK_BASE_URLS]
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for source, value in ordered:
        value = (value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        candidates.append({"base_url": value, "source": source})
    return candidates


def remaining_budget(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def bounded_timeout(requested: float, deadline: float | None, divisor: float = 1.0) -> float:
    requested = max(float(requested), 0.25)
    remaining = remaining_budget(deadline)
    if remaining is None:
        return requested
    return max(0.25, min(requested, remaining / max(divisor, 1.0)))


def evaluate_endpoint(base_url: str, model_id: str, timeout: float) -> dict[str, Any]:
    api_base = base_url.rstrip("/")
    tags_url = api_base.removesuffix("/v1") + "/api/tags"
    tcp = tcp_check(base_url, timeout)
    tags = http_get_json(tags_url, timeout) if tcp["ok"] else {"url": tags_url, "ok": False, "skipped": "tcp_check_failed"}
    listed_models = []
    if isinstance(tags.get("json"), dict):
        listed_models = [m.get("name") for m in tags["json"].get("models", []) if isinstance(m, dict)]
    return {
        "base_url": base_url,
        "tags_url": tags_url,
        "tcp_check": tcp,
        "tags_check": tags,
        "listed_models": listed_models,
        "model_available": model_id in listed_models,
    }


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    start_monotonic = time.monotonic()
    max_runtime = float(getattr(args, "max_runtime", 0) or 0)
    deadline = start_monotonic + max_runtime if max_runtime > 0 else None
    budget_exhausted = False
    budget_stage = None
    env_loaded_keys = load_env_defaults(
        [
            DEFAULT_OPENCLAW_ROOT / ".env",
            Path(os.environ.get("WORKSPACE_ROOT", str(DEFAULT_OPENCLAW_ROOT / "workspace"))) / ".env",
        ]
    )
    cfg = provider_config(Path(args.config), args.model, args.agent)
    provider_cfg = cfg.get("provider_config") or {}
    model_cfg = cfg.get("model_config") or {}
    locked_model_id = model_lock_active_model()
    model_config_present = bool(model_cfg) or (locked_model_id and locked_model_id == str(cfg.get("model_id") or ""))
    endpoint_attempts: list[dict[str, Any]] = []
    selected_endpoint: dict[str, Any] | None = None
    fallback_endpoint: dict[str, Any] | None = None
    fallback_models = [model for model in args.fallback_models if model]
    for candidate in endpoint_candidates(str(provider_cfg.get("baseUrl") or ""), args.base_url):
        remaining = remaining_budget(deadline)
        if remaining is not None and remaining <= 0.25:
            budget_exhausted = True
            budget_stage = "endpoint_probe"
            break
        evaluated = evaluate_endpoint(
            candidate["base_url"],
            str(cfg.get("model_id")),
            bounded_timeout(args.timeout, deadline, divisor=2.0),
        )
        evaluated["source"] = candidate["source"]
        endpoint_attempts.append(
            {
                "base_url": evaluated["base_url"],
                "tags_url": evaluated["tags_url"],
                "tcp_check": evaluated["tcp_check"],
                "tags_check": {k: v for k, v in evaluated["tags_check"].items() if k != "json"},
                "listed_models": evaluated["listed_models"],
                "model_available": evaluated["model_available"],
                "source": evaluated["source"],
            }
        )
        if evaluated["tcp_check"].get("ok") and evaluated["tags_check"].get("ok") and evaluated["model_available"]:
            selected_endpoint = evaluated
            break
        if (
            fallback_endpoint is None
            and evaluated["tcp_check"].get("ok")
            and evaluated["tags_check"].get("ok")
            and any(model in (evaluated.get("listed_models") or []) for model in fallback_models)
        ):
            fallback_endpoint = evaluated
    lock_base_url = model_lock_base_url()
    lock_attempt = next(
        (attempt for attempt in endpoint_attempts if attempt.get("base_url") == lock_base_url),
        {},
    )
    if selected_endpoint is None and fallback_endpoint is not None:
        selected_endpoint = fallback_endpoint
    if not selected_endpoint and endpoint_attempts:
        last = endpoint_attempts[0]
        selected_endpoint = {
            "base_url": last.get("base_url") or "",
            "tags_url": last.get("tags_url") or "",
            "tcp_check": last.get("tcp_check") or {},
            "tags_check": last.get("tags_check") or {},
            "listed_models": last.get("listed_models") or [],
            "model_available": bool(last.get("model_available")),
            "source": last.get("source") or "",
        }
    selected_endpoint = selected_endpoint or {
        "base_url": "",
        "tags_url": "",
        "tcp_check": {"ok": False, "error": "no endpoint candidates"},
        "tags_check": {"ok": False, "error": "no endpoint candidates"},
        "listed_models": [],
        "model_available": False,
    }
    base_url = str(selected_endpoint["base_url"])
    endpoint_source = str(selected_endpoint.get("source") or "")
    tags_url = args.tags_url or str(selected_endpoint["tags_url"])
    tcp = selected_endpoint["tcp_check"]
    tags = selected_endpoint["tags_check"]
    listed_models = selected_endpoint["listed_models"]
    model_available = bool(selected_endpoint["model_available"])
    api_key = resolve_api_key(provider_cfg)
    direct_smoke = {"attempted": False, "ok": False, "skipped": "endpoint_or_model_unavailable"}
    if tcp["ok"] and tags.get("ok") and model_available and not args.skip_direct_smoke:
        remaining = remaining_budget(deadline)
        if remaining is not None and remaining <= 6.0:
            budget_exhausted = True
            budget_stage = "direct_smoke"
            direct_smoke["skipped"] = "preflight_budget_exhausted"
        else:
            direct_smoke = direct_ollama_smoke(
                base_url,
                str(cfg.get("model_id")),
                bounded_timeout(args.smoke_timeout, deadline, divisor=2.0),
                args.num_ctx,
                api_key,
                args.num_predict,
            )
    finance_contract_smoke = {"attempted": False, "ok": False, "skipped": "direct_smoke_not_ok"}
    if (
        direct_smoke.get("attempted") is True
        and direct_smoke.get("ok") is True
        and direct_smoke.get("response") == "BASELANE_MODEL_OK"
        and not args.skip_direct_smoke
    ):
        remaining = remaining_budget(deadline)
        if remaining is not None and remaining <= 6.0:
            budget_exhausted = True
            budget_stage = "finance_contract_smoke"
            finance_contract_smoke["skipped"] = "preflight_budget_exhausted"
        else:
            finance_contract_smoke = direct_ollama_finance_contract_smoke(
                base_url,
                str(cfg.get("model_id")),
                bounded_timeout(args.finance_smoke_timeout, deadline, divisor=1.0),
                args.finance_num_ctx,
                api_key,
                args.finance_num_predict,
            )
    fallback_smokes: list[dict[str, Any]] = []
    primary_smoke_ok = direct_smoke.get("attempted") is True and direct_smoke.get("ok") is True and direct_smoke.get("response") == "BASELANE_MODEL_OK"
    if tcp["ok"] and tags.get("ok") and not primary_smoke_ok and not args.skip_direct_smoke:
        for fallback_model_id in fallback_models:
            if not fallback_model_id:
                continue
            if fallback_model_id not in listed_models:
                fallback_smokes.append(
                    {
                        "model_id": fallback_model_id,
                        "attempted": False,
                        "ok": False,
                        "skipped": "model_not_listed",
                    }
                )
                continue
            remaining = remaining_budget(deadline)
            if remaining is not None and remaining <= 6.0:
                budget_exhausted = True
                budget_stage = "fallback_smoke"
                fallback_smokes.append(
                    {
                        "model_id": fallback_model_id,
                        "attempted": False,
                        "ok": False,
                        "skipped": "preflight_budget_exhausted",
                    }
                )
                break
            fallback_result = direct_ollama_smoke(
                base_url,
                fallback_model_id,
                bounded_timeout(args.fallback_smoke_timeout, deadline, divisor=1.0),
                args.num_ctx,
                api_key,
                args.num_predict,
            )
            fallback_result["model_id"] = fallback_model_id
            fallback_smokes.append(fallback_result)
            if fallback_result.get("ok") is True and fallback_result.get("response") == "BASELANE_MODEL_OK":
                break
    fallback_smoke_ok = any(
        item.get("attempted") is True
        and item.get("ok") is True
        and item.get("response") == "BASELANE_MODEL_OK"
        for item in fallback_smokes
    )
    smoke = {"attempted": False, "ok": False, "skipped": "use --smoke to run OpenClaw agent smoke"}
    if args.smoke and tcp["ok"]:
        smoke = openclaw_agent_smoke(args.agent, args.model, args.smoke_timeout)
    elif args.smoke:
        smoke = {"attempted": False, "ok": False, "skipped": "endpoint_tcp_check_failed"}
    issues = []
    if not provider_cfg and not selected_endpoint:
        issues.append(f"provider config missing for {cfg.get('provider')}")
    if not model_config_present and not model_available and not fallback_smoke_ok:
        issues.append(f"model config missing for {args.model}")
    if not tcp["ok"]:
        issues.append(f"endpoint unreachable: {base_url} ({tcp.get('error')})")
    elif not tags.get("ok"):
        issues.append(f"endpoint tags check failed: {tags_url} ({tags.get('error') or tags.get('status')})")
    elif not model_available:
        issues.append(f"model not listed by endpoint: {args.model}")
    if direct_smoke.get("attempted") and not direct_smoke.get("ok"):
        if direct_smoke.get("error_code"):
            issues.append(f"direct local-model smoke failed: {direct_smoke.get('error_code')}")
        else:
            issues.append("direct local-model smoke failed")
    elif not direct_smoke.get("attempted"):
        issues.append(f"direct local-model smoke not attempted: {direct_smoke.get('skipped') or 'unknown'}")
    if finance_contract_smoke.get("attempted") and not finance_contract_smoke.get("ok"):
        issues.append("deterministic Baselane finance formatting contract smoke failed")
    elif not finance_contract_smoke.get("attempted"):
        issues.append(f"deterministic Baselane finance formatting contract smoke not attempted: {finance_contract_smoke.get('skipped') or 'unknown'}")
    warnings = []
    if args.smoke and not smoke.get("ok"):
        warnings.append("OpenClaw small-model smoke failed")
    if fallback_smoke_ok:
        if direct_smoke.get("attempted"):
            warnings.append("primary local model smoke failed but fallback local qwen smoke passed")
        else:
            warnings.append("primary local model unavailable but fallback local qwen smoke passed")
    if budget_exhausted:
        issues.append(f"preflight runtime budget exhausted during {budget_stage or 'unknown stage'}")
    report = {
        "generated_at": now_iso(),
        "status": "ok" if not issues else "review",
        "model": args.model,
        "agent": args.agent,
        "config_source": cfg.get("config_source") or "openclaw_config",
        "config_name": Path(str(cfg.get("config_path") or args.config)).name,
        "provider": cfg.get("provider"),
        "model_id": cfg.get("model_id"),
        "base_url": base_url,
        "tags_url": tags_url,
        "configured_model_present": bool(model_config_present),
        "configured_model_present_source": "model_config" if model_cfg else ("model_lock" if model_config_present else None),
        "selected_endpoint_source": endpoint_source or None,
        "selected_endpoint_from_config": endpoint_source in {"override", "env", "model_lock", "model_lock_local_companion", "config"} or base_url == str(provider_cfg.get("baseUrl") or ""),
        "model_lock_base_url": lock_base_url or None,
        "model_lock_endpoint_reachable": (lock_attempt.get("tcp_check") or {}).get("ok") if lock_attempt else None,
        "model_lock_tags_ok": (lock_attempt.get("tags_check") or {}).get("ok") if lock_attempt else None,
        "model_lock_model_available": lock_attempt.get("model_available") if lock_attempt else None,
        "endpoint_attempts": [
            {
                "base_url": attempt.get("base_url"),
                "tags_url": attempt.get("tags_url"),
                "tcp_ok": (attempt.get("tcp_check") or {}).get("ok"),
                "tags_ok": (attempt.get("tags_check") or {}).get("ok"),
                "model_available": attempt.get("model_available"),
                "listed_model_count": len(attempt.get("listed_models") or []),
            }
            for attempt in endpoint_attempts
        ],
        "tcp_check": tcp,
        "tags_check": {k: v for k, v in tags.items() if k != "json"},
        "listed_model_count": len(listed_models),
        "model_available": model_available,
        "model_execution_scope": {
            "deterministic_only": True,
            "pipeline_execution_allowed": False,
            "allowed_task_class": MODEL_ALLOWED_TASK_CLASS,
            "allowed_uses": MODEL_ALLOWED_USES,
            "forbidden_uses": MODEL_FORBIDDEN_USES,
            "financial_truth_source": "Python/CSV/worksheet deterministic pipeline, not model inference",
            "model_financial_authority": False,
            "autonomous_financial_execution_allowed": False,
            "live_side_effects_allowed": False,
            "requires_external_deterministic_validation": True,
            "fail_closed_if_contract_missing": True,
        },
        "direct_smoke": direct_smoke,
        "direct_smoke_num_predict": args.num_predict,
        "finance_contract_smoke": finance_contract_smoke,
        "finance_contract_expected_response": BASELANE_FINANCE_CONTRACT_EXPECTED,
        "fallback_smokes": fallback_smokes,
        "fallback_smoke_ok": fallback_smoke_ok,
        "smoke": smoke,
        "issue_count": len(issues),
        "issues": issues,
        "warning_count": len(warnings),
        "warnings": warnings,
        "env_loaded_keys": sorted(key for key in env_loaded_keys if key.endswith("_API_KEY")),
        "openclaw_smoke_required": False,
        "openclaw_smoke_ok": (not args.smoke) or smoke.get("ok") is True,
        "max_runtime_seconds": max_runtime if max_runtime > 0 else None,
        "runtime_seconds": round(time.monotonic() - start_monotonic, 3),
        "runtime_budget_exhausted": budget_exhausted,
        "runtime_budget_stage": budget_stage,
    }
    if cfg.get("config_path"):
        report["config_path"] = cfg.get("config_path")
    validation = validation_contract(report)
    report["validation_contract"] = validation["contract"]
    report["validation_digest"] = validation["digest"]
    report["expected_model"] = DEFAULT_MODEL
    report["local_model_ready"] = report["status"] == "ok" and all(validation["checks"].values())
    report["probe_ok"] = validation["checks"]["direct_smoke_ok"] and validation["checks"]["direct_smoke_response_exact"]
    report["local_model_operational"] = report["local_model_ready"] or fallback_smoke_ok
    if fallback_smoke_ok:
        report["operational_model_id"] = next(
            (
                str(item.get("model_id"))
                for item in fallback_smokes
                if item.get("ok") is True and item.get("response") == "BASELANE_MODEL_OK"
            ),
            None,
        )
    report["checks"] = validation["checks"]
    report["small_model_execution_allowed"] = False
    report["small_model_pipeline_execution_allowed"] = False
    report["small_model_task_scoped_execution_allowed"] = report["local_model_ready"]
    report["small_model_contract_limited_execution_allowed"] = report["local_model_ready"]
    report["small_model_allowed_task_class"] = MODEL_ALLOWED_TASK_CLASS
    report["small_model_financial_authority"] = False
    report["small_model_live_side_effects_allowed"] = False
    report["small_model_execution_decision"] = (
        "allow_deterministic_formatting_only" if report["local_model_ready"] else "blocked_until_exact_contract_passes"
    )
    report["small_model_execution_policy"] = MODEL_EXECUTION_POLICY
    report["direct_smoke_attempted"] = validation["contract"]["direct_smoke_attempted"]
    report["direct_smoke_ok"] = validation["contract"]["direct_smoke_ok"]
    report["direct_smoke_response"] = validation["contract"]["direct_smoke_response"]
    report["direct_smoke_done_reason"] = validation["contract"]["direct_smoke_done_reason"]
    report["direct_smoke_num_ctx"] = validation["contract"]["direct_smoke_num_ctx"]
    report["finance_contract_smoke_attempted"] = validation["contract"]["finance_contract_smoke_attempted"]
    report["finance_contract_smoke_ok"] = validation["contract"]["finance_contract_smoke_ok"]
    report["finance_contract_smoke_response"] = validation["contract"]["finance_contract_response"]
    report["blocker"] = local_model_blocker(report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preflight the Baselane cron small local model path")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--agent", default="baselane-cron-lite")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--tags-url", default=None)
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("BASELANE_PREFLIGHT_ENDPOINT_TIMEOUT", "8")))
    parser.add_argument(
        "--max-runtime",
        dest="max_runtime",
        type=float,
        default=float(os.environ.get("BASELANE_PREFLIGHT_MAX_RUNTIME_SECONDS", "25")),
        help="Bound the entire preflight so a review report is written before cron watchdogs intervene (0 disables).",
    )
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--smoke-timeout", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_SMOKE_TIMEOUT", "90")))
    parser.add_argument("--fallback-smoke-timeout", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_FALLBACK_SMOKE_TIMEOUT", "120")))
    parser.add_argument(
        "--fallback-models",
        nargs="*",
        default=csv_values(os.environ.get("BASELANE_PREFLIGHT_FALLBACK_MODELS", ",".join(DEFAULT_FALLBACK_MODELS))),
    )
    parser.add_argument("--skip-direct-smoke", action="store_true")
    parser.add_argument("--num-ctx", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_NUM_CTX", "128")))
    parser.add_argument("--num-predict", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_NUM_PREDICT", "8")))
    parser.add_argument("--finance-smoke-timeout", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_FINANCE_SMOKE_TIMEOUT", "120")))
    parser.add_argument("--finance-num-ctx", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_FINANCE_NUM_CTX", "256")))
    parser.add_argument("--finance-num-predict", type=int, default=int(os.environ.get("BASELANE_PREFLIGHT_FINANCE_NUM_PREDICT", "80")))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(args)
    report_path = Path(args.report)
    report = preserve_prior_precise_timeout(report, report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(report_path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(report_path)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        if report["status"] == "ok":
            print(f"NO_REPLY local model ready: {args.model}")
        else:
            print(f"BASELANE_LOCAL_MODEL_REVIEW issues={report['issue_count']} report={report_path}")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
