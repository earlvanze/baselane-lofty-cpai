import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "sync_lofty_updates_local_from_live.py"


def load_module():
    spec = importlib.util.spec_from_file_location("sync_lofty_updates_local_from_live", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PayloadPropertyIdsTest(unittest.TestCase):
    def test_accepts_current_live_capture_records(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "capture.json"
            path.write_text(
                json.dumps(
                    {
                        "records": [
                            {"lofty_property_id": "property-a"},
                            {"property_id": "property-b"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                module.payload_property_ids([path]),
                {"property-a", "property-b"},
            )


if __name__ == "__main__":
    unittest.main()
