# Baselane MCP

Local MCP server for guarded Baselane finance operations. It depends on an already authenticated, visible browser session reachable through CDP; it contains no credential, MFA, cookie, or browser-profile automation.

## Tools

| Tool | Purpose | Live-action guard |
| --- | --- | --- |
| `get_auth_status` | Read-only attached-session check | No mutation |
| `export_statements`, `export_ledger`, `weekly_unprocessed_report` | Read/export finance evidence | `dry_run=true` by default |
| `split_mortgage`, `batch_split` | Preview supported split workflows | Explicit `dry_run=false` after review |
| `list_transfer_accounts` | List eligible internal accounts with masked numbers | No mutation |
| `transfer_cash` | Move cash within one Baselane workspace | Exact dry-run confirmation token; internal accounts only; tag 24 only |

## Run

```bash
cd skills/baselane-mcp
uv run baselane-mcp
```

Set `OPENCLAW_WORKSPACE_ROOT` to the repository root and ensure `PYTHONPATH` includes `skills/baselane-mcp/src` and `scripts`.

Always preview first. For a transfer, confirm the source/destination accounts, property ID, label, date, amount, and confirmation token; then independently reconcile both post-transfer mirrors. Never use the MCP to send to an external recipient.
