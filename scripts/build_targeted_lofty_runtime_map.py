#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def slugify(value: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", value.lower())).strip("-")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an exact Lofty runtime map from verified targets.")
    parser.add_argument("--target-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--payload-dir", type=Path, required=True)
    args = parser.parse_args()

    targets = json.loads(args.target_map.read_text(encoding="utf-8")).get("records") or []
    properties = []
    seen: set[str] = set()
    for target in targets:
        property_id = str(target.get("lofty_property_id") or "").strip()
        property_name = str(target.get("property_name") or "").strip()
        updates_md = Path(str(target.get("updates_md") or ""))
        if not property_id or property_id in seen or not updates_md.is_file():
            raise SystemExit(f"Invalid or duplicate target: {property_name!r} {property_id!r}")
        seen.add(property_id)
        snapshot_dir = updates_md.parent
        properties.append(
            {
                "property_name": property_name,
                "full_address": property_name,
                "slug": slugify(property_name),
                "lofty_property_id": property_id,
                "updates_md": str(updates_md),
                "description_md": str(snapshot_dir / "DESCRIPTION.md"),
                "financials_md": str(snapshot_dir / "FINANCIALS.md"),
                "get_manager_properties_payload_file": str(args.payload_dir / "manager.get-manager-properties.payload.json"),
                "save_payload_file": str(args.payload_dir / f"{property_id}.update-manager-property.payload.json"),
                "send_payload_file": str(args.payload_dir / f"{property_id}.send-property-updates.payload.json"),
            }
        )
    output = {"properties": properties, "records": properties}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "ok", "property_count": len(properties), "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
