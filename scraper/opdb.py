"""Fetch all machines from OPDB and save per-machine JSON to data/raw/opdb/."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from open_pinball_db import Client

OUT_DIR = Path("data/raw/opdb")

# Matches variant keywords at the end of a machine name
# "Premium/LE" must come before "Premium" and "LE" to avoid partial stripping
MODEL_TYPE_RE = re.compile(
    r"[\s\(]*(Pro|Premium/LE|Premium|Limited Edition|LE|Home|Vault Edition)\s*\)?\s*$",
    re.IGNORECASE,
)

MFR_PINBALL_SUFFIX_RE = re.compile(r"\s+pinball\s*$", re.IGNORECASE)


def parse_model_type(name: str) -> str | None:
    m = MODEL_TYPE_RE.search(name)
    if m:
        return m.group(1).title()
    return None


def extract_manufacturer(mfr) -> str:
    if isinstance(mfr, dict):
        raw = mfr.get("name") or mfr.get("manufacturer_name") or ""
    else:
        raw = str(mfr) if mfr else ""
    return MFR_PINBALL_SUFFIX_RE.sub("", raw).strip()


def machine_to_record(m: dict) -> dict:
    name = (m.get("name") or "").strip()
    # Strip variant suffix from game title
    game = MODEL_TYPE_RE.sub("", name).rstrip(": ").strip() or name
    return {
        "game": game,
        "manufacturer": extract_manufacturer(m.get("manufacturer")),
        "year": m.get("year"),
        "model_type": parse_model_type(name),
        "opdb_id": m.get("opdb_id") or m.get("id") or "",
        "group_id": None,  # placeholder for future groups-endpoint join
    }


def safe_filename(opdb_id: str) -> str:
    return re.sub(r"[#/\\]", "_", opdb_id).lower() + ".json"


def _normalise_response(data) -> list[dict]:
    """Accept both a bare list and a dict wrapper {"machines": [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("machines", "data", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def main() -> int:
    load_dotenv()

    import os
    api_key = os.getenv("OPDB_API_KEY")
    if not api_key:
        print("ERROR: OPDB_API_KEY not set in environment.", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description="Scrape OPDB machine metadata.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N machines")
    parser.add_argument("--out", type=Path, default=OUT_DIR,
                        help="Output directory (default: data/raw/opdb)")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing files")
    args = parser.parse_args()

    client = Client(api_key=api_key)

    print("Fetching all machines from OPDB...", flush=True)
    try:
        raw = client.export_machines_and_aliases()
    except Exception as e:
        print(f"ERROR fetching OPDB export: {e}", file=sys.stderr)
        return 1

    machines = _normalise_response(raw)
    if not machines:
        print(f"ERROR: unexpected response shape — got {type(raw)}", file=sys.stderr)
        print(f"First 200 chars: {str(raw)[:200]}", file=sys.stderr)
        return 1

    print(f"Fetched {len(machines)} machines", flush=True)

    if args.limit is not None:
        machines = machines[: args.limit]
        print(f"Limiting to first {len(machines)}", flush=True)

    args.out.mkdir(parents=True, exist_ok=True)

    saved = skipped = 0
    total = len(machines)

    for i, m in enumerate(machines, 1):
        opdb_id = m.get("opdb_id") or m.get("id") or ""
        if not opdb_id:
            print(f"[{i}/{total}] (no opdb_id) — skipped", file=sys.stderr, flush=True)
            skipped += 1
            continue

        out_path = args.out / safe_filename(opdb_id)
        if out_path.exists() and not args.force:
            skipped += 1
            continue

        record = machine_to_record(m)
        out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{i}/{total}] {record['game'] or opdb_id} — saved", flush=True)
        saved += 1

    print(f"Done. saved={saved} skipped={skipped}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
