#!/usr/bin/env python3
"""Night-shift aggregate helper retained at the warehouse review desk."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


FAMILIES = [f"FAM-{letter}" for letter in "ABCD"]


def canonical(value: object) -> bytes:
    """Serialize one desk summary."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def build_result(payload: dict, raw: bytes) -> dict:
    """Produce a provisional summary from sorted resource lists."""
    entries = sorted(
        row["resource_id"]
        for row in payload["resources"]
        if row["role"] == "entry"
    )
    shared = sorted(
        row["resource_id"]
        for row in payload["resources"]
        if row["role"] == "shared"
    )
    crews = sorted(payload["crews"], key=lambda row: row["crew_ref"])
    jobs = {row["job_ref"]: row for row in payload["jobs"]}
    route_by_crew = {}
    crew_rows = []
    family_by_crew = {}
    for index, crew in enumerate(crews):
        route = [entries[index], shared[index]]
        crew_ref = crew["crew_ref"]
        route_by_crew[crew_ref] = route
        family_by_crew[crew_ref] = FAMILIES[index]
        crew_rows.append(
            {
                "crew_ref": crew_ref,
                "family_id": FAMILIES[index],
                "resources": route,
            }
        )
    events = []
    for crew in crews:
        crew_ref = crew["crew_ref"]
        for start_hour, job_ref in enumerate(crew["job_refs"]):
            job = jobs[job_ref]
            for stage, resource in enumerate(route_by_crew[crew_ref]):
                events.append(
                    {
                        "job_ref": job_ref,
                        "crew_ref": crew_ref,
                        "family_id": family_by_crew[crew_ref],
                        "stage": stage,
                        "hour": max(job["release_hour"], start_hour) + stage,
                        "resource_id": resource,
                    }
                )
    events.sort(key=lambda row: (row["hour"], row["resource_id"], row["job_ref"]))
    simulation = [
        {
            "strategy_id": strategy["strategy_id"],
            "completed_count": 0,
            "throughput_per_hour": 0.0,
            "last_finish_hour": 0,
        }
        for strategy in sorted(
            payload["strategies"],
            key=lambda row: row["strategy_id"],
        )
    ]
    return {
        "schema_version": 1,
        "snapshot_id": payload["snapshot_id"],
        "run_id": payload["run_id"],
        "input_digest": hashlib.sha256(raw).hexdigest(),
        "crews": crew_rows,
        "events": events,
        "simulation": simulation,
    }


def digest(value: object) -> str:
    """Hash one canonical ledger member."""
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def append_ledger(path: Path, result: dict, result_bytes: bytes) -> None:
    """Append a provisional ledger row."""
    rows = []
    if path.exists() and path.stat().st_size:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
    previous = rows[-1]["entry_hash"] if rows else "0" * 64
    row = {
        "run_id": result["run_id"],
        "snapshot_id": result["snapshot_id"],
        "input_digest": result["input_digest"],
        "result_digest": hashlib.sha256(result_bytes).hexdigest(),
        "previous_hash": previous,
    }
    row["entry_hash"] = digest(row)
    with path.open("ab") as handle:
        handle.write(canonical(row))


def main() -> int:
    """Run the retained desk path."""
    if len(sys.argv) != 4:
        return 2
    input_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    ledger_path = Path(sys.argv[3])
    raw = input_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    result = build_result(payload, raw)
    result_bytes = canonical(result)
    result_path.write_bytes(result_bytes)
    append_ledger(ledger_path, result, result_bytes)
    sys.stdout.write(f"OK {result['run_id']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
