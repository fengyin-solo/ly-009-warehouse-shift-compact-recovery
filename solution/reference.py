#!/usr/bin/env python3
"""Recover compact warehouse events and score counterfactual strategies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any


SCHEMA = "warehouse-shift-aggregate/v3"
FAMILIES = [f"FAM-{letter}" for letter in "ABCD"]
ROOT_KEYS = {
    "schema",
    "snapshot_id",
    "run_id",
    "resources",
    "crews",
    "jobs",
    "hourly",
    "transition_support",
    "strategies",
}
RESOURCE_KEYS = {"resource_id", "role"}
CREW_KEYS = {"crew_ref", "job_refs"}
JOB_KEYS = {"job_ref", "crew_ref", "release_hour"}
HOURLY_KEYS = {"hour", "busy_resources", "completed_count"}
SUPPORT_KEYS = {"cut_hour", "from_resource", "to_resource", "count"}
STRATEGY_KEYS = {
    "strategy_id",
    "start_hour",
    "horizon_hours",
    "job_refs",
    "family_order",
}
LEDGER_KEYS = {
    "run_id",
    "snapshot_id",
    "input_digest",
    "result_digest",
    "previous_hash",
    "entry_hash",
}
RESULT_KEYS = {
    "schema_version",
    "snapshot_id",
    "run_id",
    "input_digest",
    "crews",
    "events",
    "simulation",
}
ID_PATTERNS = {
    "resource": re.compile(r"^RC-[A-Z0-9]{4}$"),
    "crew": re.compile(r"^CRW-[A-Z0-9]{3}$"),
    "job": re.compile(r"^JOB-[A-Z0-9]{5}$"),
    "strategy": re.compile(r"^STR-[A-Z0-9]{4}$"),
    "opaque": re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,63}$"),
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Refused(Exception):
    """A public refusal with one stable code."""

    code = "BAD_INPUT"


class BadInput(Refused):
    """The aggregate does not satisfy the published input contract."""

    code = "BAD_INPUT"


class RecoveryInfeasible(Refused):
    """The aggregate cannot close routes or events."""

    code = "RECOVERY_INFEASIBLE"


class LedgerConflict(Refused):
    """A retained run identifier points at different bytes."""

    code = "LEDGER_CONFLICT"


class LedgerCorrupt(Refused):
    """The retained ledger is malformed or broken."""

    code = "LEDGER_CORRUPT"


def canonical_bytes(value: object, newline: bool = True) -> bytes:
    """Serialize a value with the one accepted JSON appearance."""
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (text + ("\n" if newline else "")).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(value).hexdigest()


def digest_value(value: object) -> str:
    """Hash one JSON value without a trailing newline."""
    return digest_bytes(canonical_bytes(value, newline=False))


def quantize(value: float) -> float:
    """Round a nonnegative ratio to three decimal places, half up."""
    quantum = Decimal("0.001")
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while refusing duplicate members."""
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise BadInput(f"duplicate key: {key}")
        output[key] = value
    return output


def reject_nonfinite(token: str) -> None:
    """Refuse JSON extensions that are not finite numbers."""
    raise BadInput(f"non-finite number: {token}")


def parse_bytes(raw: bytes) -> object:
    """Parse strict UTF-8 JSON with duplicate and non-finite checks."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadInput("input is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=no_duplicate_object,
            parse_constant=reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise BadInput("input is not complete JSON") from exc


def require_object(value: object, keys: set[str], name: str) -> dict[str, Any]:
    """Require one object with exactly the named keys."""
    if not isinstance(value, dict):
        raise BadInput(f"{name} must be an object")
    if set(value) != keys:
        raise BadInput(f"{name} has the wrong key set")
    return value


def require_array(
    value: object,
    name: str,
    low: int,
    high: int,
) -> list[Any]:
    """Require a JSON array in a closed size range."""
    if not isinstance(value, list):
        raise BadInput(f"{name} must be an array")
    if len(value) < low or len(value) > high:
        raise BadInput(f"{name} is outside its size range")
    return value


def require_int(value: object, name: str, low: int, high: int) -> int:
    """Require a non-boolean integer in a closed range."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadInput(f"{name} must be an integer")
    if value < low or value > high:
        raise BadInput(f"{name} is outside its range")
    return value


def require_text(
    value: object,
    pattern: re.Pattern[str],
    name: str,
) -> str:
    """Require text matching a published identifier pattern."""
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise BadInput(f"{name} is not a valid identifier")
    return value


def unique_strings(values: object, name: str, low: int, high: int) -> list[str]:
    """Require a bounded string array with no repeated member."""
    records = require_array(values, name, low, high)
    result = []
    for value in records:
        if not isinstance(value, str):
            raise BadInput(f"{name} must contain strings")
        result.append(value)
    if len(set(result)) != len(result):
        raise BadInput(f"{name} contains duplicates")
    return result


def sorted_unique_strings(
    values: object,
    name: str,
    low: int,
    high: int,
) -> list[str]:
    """Require a bounded sorted string array."""
    result = unique_strings(values, name, low, high)
    if result != sorted(result):
        raise BadInput(f"{name} must be sorted")
    return result


def validate_resources(value: object) -> tuple[list[str], list[str]]:
    """Validate the four-entry and four-shared inventory."""
    records = require_array(value, "resources", 8, 8)
    entries: list[str] = []
    shared: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        record = require_object(item, RESOURCE_KEYS, f"resources[{index}]")
        resource_id = require_text(
            record["resource_id"],
            ID_PATTERNS["resource"],
            "resource_id",
        )
        if resource_id in seen:
            raise BadInput("resource identifiers are not unique")
        seen.add(resource_id)
        if record["role"] == "entry":
            entries.append(resource_id)
        elif record["role"] == "shared":
            shared.append(resource_id)
        else:
            raise BadInput("resource role is not admitted")
    if len(entries) != 4 or len(shared) != 4:
        raise BadInput("resources must contain four entries and four shared")
    return sorted(entries), sorted(shared)


def validate_jobs(
    value: object,
    known_crews: set[str],
) -> dict[str, dict[str, object]]:
    """Validate and index the eight jobs."""
    records = require_array(value, "jobs", 8, 8)
    jobs: dict[str, dict[str, object]] = {}
    for index, item in enumerate(records):
        record = require_object(item, JOB_KEYS, f"jobs[{index}]")
        job_ref = require_text(record["job_ref"], ID_PATTERNS["job"], "job_ref")
        crew_ref = require_text(record["crew_ref"], ID_PATTERNS["crew"], "crew_ref")
        if job_ref in jobs:
            raise BadInput("job identifiers are not unique")
        if crew_ref not in known_crews:
            raise BadInput("job references an unknown crew")
        jobs[job_ref] = {
            "job_ref": job_ref,
            "crew_ref": crew_ref,
            "release_hour": require_int(
                record["release_hour"],
                "release_hour",
                0,
                1,
            ),
        }
    return jobs


def validate_crews(
    value: object,
    jobs: dict[str, dict[str, object]],
) -> dict[str, list[str]]:
    """Validate the two-job partition represented by four crews."""
    records = require_array(value, "crews", 4, 4)
    crews: dict[str, list[str]] = {}
    covered: list[str] = []
    for index, item in enumerate(records):
        record = require_object(item, CREW_KEYS, f"crews[{index}]")
        crew_ref = require_text(record["crew_ref"], ID_PATTERNS["crew"], "crew_ref")
        if crew_ref in crews:
            raise BadInput("crew identifiers are not unique")
        refs = sorted_unique_strings(record["job_refs"], "job_refs", 2, 2)
        if any(ref not in jobs for ref in refs):
            raise BadInput("crew references an unknown job")
        crews[crew_ref] = refs
        covered.extend(refs)
    if len(set(covered)) != 8 or set(covered) != set(jobs):
        raise BadInput("crews do not partition the jobs")
    for job_ref, job in jobs.items():
        if job_ref not in crews[str(job["crew_ref"])]:
            raise BadInput("job and crew references disagree")
    return crews


def validate_hourly(
    value: object,
    known_resources: set[str],
) -> list[dict[str, object]]:
    """Validate three bounded hourly aggregate rows."""
    records = require_array(value, "hourly", 3, 3)
    hourly = []
    for hour, item in enumerate(records):
        record = require_object(item, HOURLY_KEYS, f"hourly[{hour}]")
        if require_int(record["hour"], "hour", 0, 2) != hour:
            raise BadInput("hourly rows are not sequential")
        busy = sorted_unique_strings(
            record["busy_resources"],
            "busy_resources",
            0,
            8,
        )
        if any(resource not in known_resources for resource in busy):
            raise RecoveryInfeasible("hourly row references an unknown resource")
        hourly.append(
            {
                "hour": hour,
                "busy_resources": busy,
                "completed_count": require_int(
                    record["completed_count"],
                    "completed_count",
                    0,
                    8,
                ),
            }
        )
    return hourly


def validate_support(
    value: object,
    known_resources: set[str],
) -> list[dict[str, object]]:
    """Validate transition rows and their four-tuple uniqueness."""
    records = require_array(value, "transition_support", 1, 8)
    support = []
    seen: set[tuple[int, str, str]] = set()
    for index, item in enumerate(records):
        record = require_object(item, SUPPORT_KEYS, f"support[{index}]")
        cut_hour = require_int(record["cut_hour"], "cut_hour", 0, 1)
        source = require_text(
            record["from_resource"],
            ID_PATTERNS["resource"],
            "from_resource",
        )
        target = require_text(
            record["to_resource"],
            ID_PATTERNS["resource"],
            "to_resource",
        )
        if source not in known_resources or target not in known_resources:
            raise RecoveryInfeasible("support references an unknown resource")
        key = (cut_hour, source, target)
        if key in seen:
            raise BadInput("transition support is duplicated")
        seen.add(key)
        support.append(
            {
                "cut_hour": cut_hour,
                "from_resource": source,
                "to_resource": target,
                "count": require_int(record["count"], "count", 1, 4),
            }
        )
    return support


def validate_strategies(value: object, job_refs: set[str]) -> list[dict[str, object]]:
    """Validate two or three closed counterfactual strategies."""
    records = require_array(value, "strategies", 2, 3)
    strategies = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        record = require_object(item, STRATEGY_KEYS, f"strategies[{index}]")
        strategy_id = require_text(
            record["strategy_id"],
            ID_PATTERNS["strategy"],
            "strategy_id",
        )
        if strategy_id in seen:
            raise BadInput("strategy identifiers are not unique")
        seen.add(strategy_id)
        refs = sorted_unique_strings(record["job_refs"], "strategy job_refs", 1, 8)
        if any(ref not in job_refs for ref in refs):
            raise BadInput("strategy references an unknown job")
        family_order = unique_strings(record["family_order"], "family_order", 4, 4)
        if sorted(family_order) != FAMILIES:
            raise BadInput("family_order is not a four-family permutation")
        strategies.append(
            {
                "strategy_id": strategy_id,
                "start_hour": require_int(record["start_hour"], "start_hour", 0, 2),
                "horizon_hours": require_int(
                    record["horizon_hours"],
                    "horizon_hours",
                    1,
                    4,
                ),
                "job_refs": refs,
                "family_order": family_order,
            }
        )
    return sorted(strategies, key=lambda row: str(row["strategy_id"]))


def validate_payload(value: object) -> dict[str, object]:
    """Validate and index the complete aggregate."""
    payload = require_object(value, ROOT_KEYS, "root")
    if payload["schema"] != SCHEMA:
        raise BadInput("schema is not supported")
    snapshot_id = require_text(
        payload["snapshot_id"],
        ID_PATTERNS["opaque"],
        "snapshot_id",
    )
    run_id = require_text(payload["run_id"], ID_PATTERNS["opaque"], "run_id")
    entries, shared = validate_resources(payload["resources"])
    resources = entries + shared
    crew_refs = {
        item["crew_ref"]
        for item in require_array(payload["crews"], "crews", 4, 4)
        if isinstance(item, dict) and isinstance(item.get("crew_ref"), str)
    }
    jobs = validate_jobs(payload["jobs"], crew_refs)
    crews = validate_crews(payload["crews"], jobs)
    if len(crews) != 4:
        raise BadInput("root must contain four crews")
    hourly = validate_hourly(payload["hourly"], set(resources))
    support = validate_support(payload["transition_support"], set(resources))
    strategies = validate_strategies(payload["strategies"], set(jobs))
    return {
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "entries": entries,
        "shared": shared,
        "crews": crews,
        "jobs": jobs,
        "hourly": hourly,
        "support": support,
        "strategies": strategies,
    }


def bipartite_route_map(
    entries: list[str],
    shared: list[str],
    support: list[dict[str, object]],
) -> dict[str, str]:
    """Recover one entry-to-shared route by augmenting paths, not permutation."""
    edges: dict[str, list[str]] = {entry: [] for entry in entries}
    for row in support:
        source = str(row["from_resource"])
        target = str(row["to_resource"])
        if source in edges and target not in edges[source]:
            edges[source].append(target)
    for source in edges:
        edges[source].sort()
    matched: dict[str, str] = {}

    def augment(source: str, visited: set[str]) -> bool:
        for target in edges[source]:
            if target in visited:
                continue
            visited.add(target)
            if target not in matched or augment(matched[target], visited):
                matched[target] = source
                return True
        return False

    for source in sorted(entries):
        if not augment(source, set()):
            raise RecoveryInfeasible("support does not admit a complete route map")
    reverse = {source: target for target, source in matched.items()}
    if set(reverse) != set(entries) or set(reverse.values()) != set(shared):
        raise RecoveryInfeasible("route map does not cover all resources")
    return reverse


def observe_transitions(
    events: list[dict[str, object]],
) -> dict[int, dict[tuple[str, str], int]]:
    """Aggregate the two-hour transitions represented by recovered events."""
    by_job: dict[str, dict[int, dict[str, object]]] = {}
    for event in events:
        by_job.setdefault(str(event["job_ref"]), {})[int(event["stage"])] = event
    observed: dict[int, dict[tuple[str, str], int]] = {}
    for stages in by_job.values():
        first = stages[0]
        second = stages[1]
        key = (str(first["resource_id"]), str(second["resource_id"]))
        cut = int(first["hour"])
        observed.setdefault(cut, {})
        observed[cut][key] = observed[cut].get(key, 0) + 1
    return observed


def supplied_transitions(
    support: list[dict[str, object]],
) -> dict[int, dict[tuple[str, str], int]]:
    """Index the complete nonzero transition set for each supplied cut."""
    supplied: dict[int, dict[tuple[str, str], int]] = {}
    for row in support:
        cut = int(row["cut_hour"])
        key = (str(row["from_resource"]), str(row["to_resource"]))
        supplied.setdefault(cut, {})
        supplied[cut][key] = int(row["count"])
    return supplied


def recover(
    payload: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Recover canonical crew routes, families, and two-stage events."""
    entries = list(payload["entries"])
    shared = list(payload["shared"])
    crews = dict(payload["crews"])
    jobs = dict(payload["jobs"])
    support = list(payload["support"])
    hourly = list(payload["hourly"])
    route_map = bipartite_route_map(entries, shared, support)
    routes = sorted([entry, route_map[entry]] for entry in entries)
    crew_refs = sorted(crews)
    crew_rows = []
    route_by_crew: dict[str, list[str]] = {}
    family_by_crew: dict[str, str] = {}
    for index, (crew_ref, route) in enumerate(zip(crew_refs, routes)):
        family_id = FAMILIES[index]
        crew_rows.append(
            {
                "crew_ref": crew_ref,
                "family_id": family_id,
                "resources": route,
            }
        )
        route_by_crew[crew_ref] = route
        family_by_crew[crew_ref] = family_id

    events = []
    for crew_ref in crew_refs:
        for start_hour, job_ref in enumerate(crews[crew_ref]):
            job = jobs[job_ref]
            if start_hour < int(job["release_hour"]):
                raise RecoveryInfeasible("released job cannot fit the two-hour route")
            for stage, resource in enumerate(route_by_crew[crew_ref]):
                events.append(
                    {
                        "job_ref": job_ref,
                        "crew_ref": crew_ref,
                        "family_id": family_by_crew[crew_ref],
                        "stage": stage,
                        "hour": start_hour + stage,
                        "resource_id": resource,
                    }
                )
    events.sort(key=lambda row: (row["hour"], row["resource_id"], row["job_ref"]))

    totals: dict[str, int] = {}
    for row in hourly:
        for resource in row["busy_resources"]:
            totals[resource] = totals.get(resource, 0) + 1
    if totals != {resource: 2 for resource in entries + shared}:
        raise RecoveryInfeasible("resource totals do not equal two uses")

    event_busy: list[set[str]] = [set(), set(), set()]
    for event in events:
        event_busy[int(event["hour"])].add(str(event["resource_id"]))
    for hour, row in enumerate(hourly):
        if sorted(event_busy[hour]) != row["busy_resources"]:
            raise RecoveryInfeasible("hourly resource union does not close")
    completed = [0, 0, 0]
    for event in events:
        if event["stage"] == 1:
            completed[int(event["hour"])] += 1
    if completed != [int(row["completed_count"]) for row in hourly]:
        raise RecoveryInfeasible("completed counts do not close")
    if observe_transitions(events) != supplied_transitions(support):
        raise RecoveryInfeasible("transition support does not close")
    return crew_rows, events


def simulate(
    payload: dict[str, object],
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Score deterministic counterfactual policies on the recovered routes."""
    route_by_crew: dict[str, list[str]] = {}
    family_by_crew: dict[str, str] = {}
    for event in events:
        crew_ref = str(event["crew_ref"])
        family_by_crew[crew_ref] = str(event["family_id"])
        if event["stage"] == 0:
            route_by_crew[crew_ref] = [str(event["resource_id"])]
        else:
            route_by_crew.setdefault(crew_ref, []).append(str(event["resource_id"]))
    jobs = dict(payload["jobs"])
    simulation = []
    for strategy in payload["strategies"]:
        start = int(strategy["start_hour"])
        horizon = int(strategy["horizon_hours"])
        end = start + horizon
        rank = {
            family: index
            for index, family in enumerate(strategy["family_order"])
        }
        ordered_jobs = sorted(
            (jobs[ref] for ref in strategy["job_refs"]),
            key=lambda job: (
                rank[family_by_crew[str(job["crew_ref"])]],
                str(job["job_ref"]),
            ),
        )
        occupied: dict[str, set[int]] = {}
        finish_hours: list[int] = []
        for job in ordered_jobs:
            route = route_by_crew[str(job["crew_ref"])]
            earliest = start + int(job["release_hour"])
            for hour in range(earliest, end):
                first_free = hour not in occupied.get(route[0], set())
                second_free = hour + 1 not in occupied.get(route[1], set())
                if first_free and second_free:
                    occupied.setdefault(route[0], set()).add(hour)
                    occupied.setdefault(route[1], set()).add(hour + 1)
                    finish_hours.append(hour + 1)
                    break
        completed_count = len(finish_hours)
        last_finish = max(finish_hours, default=start) - start
        simulation.append(
            {
                "strategy_id": strategy["strategy_id"],
                "completed_count": completed_count,
                "throughput_per_hour": quantize(completed_count / horizon),
                "last_finish_hour": last_finish,
            }
        )
    return simulation


def parse_ledger(path: Path) -> list[dict[str, object]]:
    """Read and validate the retained append-only ledger."""
    if not path.exists():
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise LedgerCorrupt("ledger cannot be read") from exc
    if not raw:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise LedgerCorrupt("ledger is not UTF-8") from exc
    if not text.endswith("\n"):
        raise LedgerCorrupt("ledger does not end in one newline")
    entries = []
    previous = "0" * 64
    snapshot_id = None
    run_ids: set[str] = set()
    for line in text.splitlines():
        if not line:
            raise LedgerCorrupt("ledger contains a blank line")
        try:
            parsed = json.loads(
                line,
                object_pairs_hook=no_duplicate_object,
                parse_constant=reject_nonfinite,
            )
        except (json.JSONDecodeError, BadInput) as exc:
            raise LedgerCorrupt("ledger line is not strict JSON") from exc
        record = require_object(parsed, LEDGER_KEYS, "ledger entry")
        if any(not isinstance(record[key], str) for key in LEDGER_KEYS):
            raise LedgerCorrupt("ledger values must be strings")
        if record["previous_hash"] != previous:
            raise LedgerCorrupt("ledger chain is broken")
        unsigned = {key: value for key, value in record.items() if key != "entry_hash"}
        if digest_value(unsigned) != record["entry_hash"]:
            raise LedgerCorrupt("ledger entry digest does not match")
        digest_keys = ("input_digest", "result_digest", "previous_hash", "entry_hash")
        if any(HEX64.fullmatch(str(record[key])) is None for key in digest_keys):
            raise LedgerCorrupt("ledger contains a malformed digest")
        if record["run_id"] in run_ids:
            raise LedgerCorrupt("ledger contains a duplicate run")
        if snapshot_id is None:
            snapshot_id = record["snapshot_id"]
        if record["snapshot_id"] != snapshot_id:
            raise LedgerCorrupt("ledger mixes snapshots")
        previous = str(record["entry_hash"])
        run_ids.add(str(record["run_id"]))
        entries.append(record)
    return entries


def ledger_entry(
    result: dict[str, object],
    result_bytes: bytes,
    previous_hash: str,
) -> dict[str, str]:
    """Build one hash-linked ledger entry."""
    entry = {
        "run_id": str(result["run_id"]),
        "snapshot_id": str(result["snapshot_id"]),
        "input_digest": str(result["input_digest"]),
        "result_digest": digest_bytes(result_bytes),
        "previous_hash": previous_hash,
    }
    entry["entry_hash"] = digest_value(entry)
    return entry


def prepare_ledger(
    path: Path,
    result: dict[str, object],
    result_bytes: bytes,
) -> tuple[bytes | None, bool]:
    """Return the next ledger bytes and whether publication may be skipped."""
    entries = parse_ledger(path)
    current = next(
        (entry for entry in entries if entry["run_id"] == result["run_id"]),
        None,
    )
    result_digest = digest_bytes(result_bytes)
    if current is not None:
        if (
            current["snapshot_id"] == result["snapshot_id"]
            and current["input_digest"] == result["input_digest"]
            and current["result_digest"] == result_digest
        ):
            return None, True
        raise LedgerConflict("run identity was reused with different bytes")
    previous = str(entries[-1]["entry_hash"]) if entries else "0" * 64
    new_entry = ledger_entry(result, result_bytes, previous)
    old_bytes = path.read_bytes() if path.exists() else b""
    return old_bytes + canonical_bytes(new_entry), False


def regular_or_absent(path: Path) -> None:
    """Require a publish target to be absent or one regular file."""
    if not path.exists():
        if not path.parent.is_dir():
            raise BadInput("publish parent does not exist")
        return
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or path.is_symlink():
        raise BadInput("publish target is not a regular file")


def replace_bytes(path: Path, data: bytes) -> None:
    """Replace one ordinary file from a same-directory temporary file."""
    regular_or_absent(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".warehouse-",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def publish(
    result_path: Path,
    ledger_path: Path,
    result_bytes: bytes,
    ledger_bytes: bytes | None,
    skip: bool,
) -> None:
    """Publish the result and optional ledger append."""
    if skip:
        if not result_path.exists() or result_path.read_bytes() != result_bytes:
            raise LedgerConflict("retained result bytes changed")
        return
    regular_or_absent(result_path)
    regular_or_absent(ledger_path)
    old_result = result_path.read_bytes() if result_path.exists() else None
    try:
        replace_bytes(result_path, result_bytes)
        if ledger_bytes is not None:
            replace_bytes(ledger_path, ledger_bytes)
    except Exception:
        if old_result is None:
            result_path.unlink(missing_ok=True)
        else:
            replace_bytes(result_path, old_result)
        raise


def build_result(
    payload: dict[str, object],
    raw: bytes,
) -> tuple[dict[str, object], bytes]:
    """Build the complete canonical result."""
    crews, events = recover(payload)
    result = {
        "schema_version": 1,
        "snapshot_id": payload["snapshot_id"],
        "run_id": payload["run_id"],
        "input_digest": digest_bytes(raw),
        "crews": crews,
        "events": events,
        "simulation": simulate(payload, events),
    }
    if set(result) != RESULT_KEYS:
        raise AssertionError("result root changed")
    result_bytes = canonical_bytes(result)
    return result, result_bytes


def main() -> int:
    """Run one recovery and ledger publication."""
    if len(sys.argv) != 4:
        sys.stderr.write("REFUSED BAD_INPUT\n")
        return 3
    input_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    ledger_path = Path(sys.argv[3])
    try:
        raw = input_path.read_bytes()
        payload = validate_payload(parse_bytes(raw))
        result, result_bytes = build_result(payload, raw)
        ledger_bytes, skip = prepare_ledger(ledger_path, result, result_bytes)
        publish(result_path, ledger_path, result_bytes, ledger_bytes, skip)
    except Refused as exc:
        sys.stderr.write(f"REFUSED {exc.code}\n")
        return 3
    except (OSError, UnicodeError):
        sys.stderr.write("REFUSED BAD_INPUT\n")
        return 3
    sys.stdout.write(f"OK {result['run_id']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
