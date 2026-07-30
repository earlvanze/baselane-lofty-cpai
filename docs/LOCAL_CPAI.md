# Local CPAI Control Plane

The CPAI model is a local observer and drafting assistant, not an accounting system. Deterministic Baselane exports, ECO GL calculations, policy JSON, Cash Flow workbooks, and the guarded `lofty-pm` integration remain the financial source of truth.

## Deployment target

The initial production candidate is `qwen2.5:14b-instruct` on Cyber's RTX 3090. Start with a context size of 8,192 and a conservative quantization that leaves VRAM for the runtime and context cache. It is selected for dependable structured instruction following, not speed.

The existing legacy Qwen3.5 preflight remains isolated and observation-only until a formal replay evaluation approves a replacement. Do not switch its local model lock or production scheduler merely because a model is installed. Qwen3.5's current tool-call/parser behavior is not a suitable financial-action dependency.

## Trust boundary

`scripts/cpai_local_supervisor.py` has one job: consume bounded local reports and produce a strict `DecisionEnvelope`. It cannot run shell commands, call MCP tools, change Google Sheets, publish to Lofty, communicate externally, or mutate Baselane.

The supervisor is deliberately configured in `shadow` mode. Its output is evidence for the deterministic gate, not an instruction to act.

```json
{
  "schema_version": 1,
  "input_digest": "sha256 of report manifest",
  "decision": "proceed | review | escalate",
  "recommended_action": "allowlisted advisory action",
  "reason_codes": ["lowercase_reason_code"],
  "summary": "bounded factual summary"
}
```

Every candidate response must exactly match the report-manifest digest and the allowlisted schema. Parse errors, unknown fields, hallucinated actions, timeouts, model outages, and report changes fail closed to `review`.

## Shadow run

Use reports already created by a deterministic lane. This writes only a local, Git-ignored report:

```bash
python3 scripts/cpai_local_supervisor.py \
  --input-report reports/baselane_daily_sync_report.json \
  --input-report reports/baselane_report_integrity_guard.json \
  --report reports/cpai_daily_shadow.json
```

For repeatable tests, supply `--response-file` with a prebuilt envelope. It never contacts Ollama. Production calls use only the local endpoint specified by `CPAI_OLLAMA_BASE_URL` or the policy default; do not point it at a public model endpoint.

The supervisor accepts only loopback, Docker-host, or Tailnet `http` endpoints. `scripts/cpai_shadow_after_daily.sh` is the optional post-daily wrapper; it checks that the deterministic daily reports exist before producing an advisory report. It is intentionally not installed into a scheduler by this repository.

## Promotion gates

1. Build an anonymized replay corpus of daily, weekly, monthly, stale-source, duplicate-fee, missing-reciprocal, and conflicting-cash cases.
2. Require exact schema parsing for every case and zero false `proceed` decisions for all mutation, conflict, and missing-evidence cases.
3. Run daily in shadow mode for at least two closed reporting cycles; compare envelopes with deterministic outcomes.
4. Only then consider local-only artifact rebuild recommendations. The model must remain unable to dispatch jobs or approve live actions.

No model may independently authorize cash transfers, manual rows, native splits, GL edits, Cash Flow workbooks, Google Sheets, Lofty publication, email, Telegram, or Discord.
