from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "cpai_local_supervisor.py"
SPEC = importlib.util.spec_from_file_location("cpai_local_supervisor", SCRIPT)
assert SPEC and SPEC.loader
SUPERVISOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SUPERVISOR)


def manifest_digest(report: Path) -> str:
    raw = report.read_bytes()
    manifest = [{"name": report.name, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "truncated": False}]
    return SUPERVISOR.digest(manifest)


class LocalSupervisorTests(unittest.TestCase):
    def test_valid_fixture_produces_valid_shadow_report(self) -> None:
        with self.subTest("valid_fixture"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                evidence = tmp_path / "daily.json"
                evidence.write_text('{"status":"review","reason":"source_stale"}\n', encoding="utf-8")
                fixture = tmp_path / "response.json"
                fixture.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "input_digest": manifest_digest(evidence),
                            "decision": "review",
                            "recommended_action": "request_human_review",
                            "reason_codes": ["source_stale"],
                            "summary": "The source report is stale and requires review.",
                        }
                    ),
                    encoding="utf-8",
                )
                output = tmp_path / "supervisor.json"
                rc = SUPERVISOR.main(["--input-report", str(evidence), "--response-file", str(fixture), "--report", str(output)])
                report = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(rc, 0)
                self.assertEqual(report["status"], "ok")
                self.assertEqual(report["mode"], "shadow")
                self.assertEqual(report["dispatch"], "disabled")
                self.assertEqual(report["envelope"]["decision"], "review")

    def test_digest_mismatch_fails_closed(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            evidence = tmp_path / "daily.json"
            evidence.write_text('{"status":"ok"}\n', encoding="utf-8")
            fixture = tmp_path / "response.json"
            fixture.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "input_digest": "0" * 64,
                        "decision": "proceed",
                        "recommended_action": "continue_read_only_pipeline",
                        "reason_codes": ["report_complete"],
                        "summary": "Continue.",
                    }
                ),
                encoding="utf-8",
            )
            output = tmp_path / "supervisor.json"
            SUPERVISOR.main(["--input-report", str(evidence), "--response-file", str(fixture), "--report", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "review")
            self.assertEqual(report["envelope"]["decision"], "review")
            self.assertIn("input_digest_mismatch", report["validation_errors"])

    def test_only_loopback_or_tailnet_model_endpoints_are_allowed(self) -> None:
        self.assertEqual(SUPERVISOR.validate_local_endpoint("http://127.0.0.1:11434"), "http://127.0.0.1:11434")
        self.assertEqual(SUPERVISOR.validate_local_endpoint("http://100.88.253.107:11434"), "http://100.88.253.107:11434")
        with self.assertRaises(ValueError):
            SUPERVISOR.validate_local_endpoint("https://example.com")
