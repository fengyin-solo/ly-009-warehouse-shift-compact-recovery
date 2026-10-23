from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import time
from math import factorial
from pathlib import Path

import pytest


TASK_APP = os.environ.get("TASK_APP", "/app/warehouse_policy.py")
DATA_DIR = Path(os.environ.get("TASK_DATA_DIR", "/app/data"))


def canonical_bytes(value: object) -> bytes:
    """Return the public compact JSON appearance."""
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def load_payload(name: str = "trace.json") -> dict:
    """Load one generated aggregate snapshot."""
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def invoke(
    input_path: Path,
    result_path: Path,
    ledger_path: Path,
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Invoke the candidate with a local watchdog."""
    started = time.monotonic()
    completed = subprocess.run(
        ["python3", TASK_APP, str(input_path), str(result_path), str(ledger_path)],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return completed, time.monotonic() - started


def run_raw(
    directory: Path,
    raw: bytes,
    tag: str,
) -> tuple[Path, Path, Path, subprocess.CompletedProcess[str], float]:
    """Write one raw fixture and invoke the candidate."""
    work = directory / tag
    work.mkdir(parents=True)
    input_path = work / "trace.json"
    result_path = work / "result.json"
    ledger_path = work / "ledger.jsonl"
    input_path.write_bytes(raw)
    completed, elapsed = invoke(input_path, result_path, ledger_path)
    return input_path, result_path, ledger_path, completed, elapsed


def run_payload(
    directory: Path,
    payload: dict,
    tag: str,
) -> tuple[
    dict | None,
    Path,
    Path,
    Path,
    subprocess.CompletedProcess[str],
    bytes,
    float,
]:
    """Run one object fixture and parse a result when present."""
    raw = canonical_bytes(payload)
    input_path, result_path, ledger_path, completed, elapsed = run_raw(
        directory,
        raw,
        tag,
    )
    result = (
        json.loads(result_path.read_text(encoding="utf-8"))
        if result_path.exists()
        else None
    )
    return result, input_path, result_path, ledger_path, completed, raw, elapsed


def expected_crew_rows(payload: dict) -> list[dict]:
    """Recover the canonical route assignment from supplied support."""
    edges = {
        row["from_resource"]: row["to_resource"]
        for row in payload["transition_support"]
    }
    entries = sorted(
        row["resource_id"]
        for row in payload["resources"]
        if row["role"] == "entry"
    )
    routes = sorted([entry, edges[entry]] for entry in entries)
    crews = sorted(payload["crews"], key=lambda row: row["crew_ref"])
    return [
        {
            "crew_ref": crew["crew_ref"],
            "family_id": f"FAM-{letter}",
            "resources": route,
        }
        for crew, route, letter in zip(crews, routes, "ABCD")
    ]


def event_hours_are_contiguous(result: dict) -> bool:
    """Check that every job has stage zero and one in adjacent hours."""
    by_job: dict[str, list[dict]] = {}
    for event in result["events"]:
        by_job.setdefault(event["job_ref"], []).append(event)
    return all(
        sorted(row["stage"] for row in events) == [0, 1]
        and events[1]["hour"] - events[0]["hour"] == 1
        for events in by_job.values()
    )


def event_rows_are_sorted(events: list[dict]) -> bool:
    """Check the public event ordering key."""
    keys = [(row["hour"], row["resource_id"], row["job_ref"]) for row in events]
    return keys == sorted(keys)


def hourly_closes(payload: dict, result: dict) -> bool:
    """Check every recovered hourly resource union."""
    for row in payload["hourly"]:
        observed = {
            event["resource_id"]
            for event in result["events"]
            if event["hour"] == row["hour"]
        }
        if sorted(observed) != row["busy_resources"]:
            return False
    return True


def duplicate_key_raw(payload: dict) -> bytes:
    """Append one duplicate root key to a valid payload."""
    text = canonical_bytes(payload).decode("utf-8").rstrip("\n")
    return (text[:-1] + ',"run_id":"RUN-DUPLICATE"}\n').encode("utf-8")


@pytest.fixture(scope="session")
def baseline(tmp_path_factory: pytest.TempPathFactory):
    """Run the default scene once."""
    directory = tmp_path_factory.mktemp("baseline")
    return run_payload(directory, load_payload(), "scene_default")


def test_gate_recovery_and_counterfactual(tmp_path: Path):
    """CASE=CORE-01
    题面：「清晨换班时，逐事件日志已被覆盖，只剩三小时的资源占用、完成计数和几段跨小时转移。」。
    """
    payload = load_payload()
    result, _, _, _, completed, _, _ = run_payload(
        tmp_path,
        payload,
        "gate_recovery",
    )
    assert completed.returncode == 0
    assert completed.stdout == f"OK {payload['run_id']}\n"
    assert completed.stderr == ""
    assert result["crews"] == expected_crew_rows(payload)
    assert len(result["events"]) == 16
    assert hourly_closes(payload, result)
    assert [
        (
            row["strategy_id"],
            row["completed_count"],
            row["throughput_per_hour"],
            row["last_finish_hour"],
        )
        for row in result["simulation"]
    ] == [
        ("STR-A001", 8, 2.0, 2),
        ("STR-B002", 8, 4.0, 2),
        ("STR-C003", 2, 2.0, 1),
    ]


def test_gate_idempotent_ledger(tmp_path: Path):
    """CASE=LEDGER-01
    题面：「结果只认紧凑 JSON：键名升序、无多余空格、末尾恰好一个换行。复核账每行是 UTF-8 对象，键恰好为 `run_id`、`snapshot_id`、`input_digest`、`result_digest`、`previous_hash`、`entry_hash`；首行前链为六十四个零，后续前链取上一行 `entry_hash`，本行摘要按去掉 `entry_hash` 后的规范字节计算。同一运行号、同一输入重跑必须保持结果和账本字节不变；同号异输入返回 `LEDGER_CONFLICT`，新运行号只能追加同一现场的新链尾。」。
    """
    payload = load_payload()
    _, input_path, result_path, ledger_path, first, _, _ = run_payload(
        tmp_path,
        payload,
        "gate_ledger",
    )
    first_result = result_path.read_bytes()
    first_ledger = ledger_path.read_bytes()
    second, _ = invoke(input_path, result_path, ledger_path)
    assert first.returncode == 0
    assert second.returncode == 0
    assert result_path.read_bytes() == first_result
    assert ledger_path.read_bytes() == first_ledger
    assert len(first_ledger.splitlines()) == 1


def test_resource_role_split_is_enforced(tmp_path: Path):
    """CASE=RESOURCE-01
    题面：「`resources` 恰好八项，每项只有 `resource_id` 和 `role`；编号匹配 `RC-[A-Z0-9]{4}` 且唯一，角色为 `entry` 的恰好四项、为 `shared` 的恰好四项。`crews` 恰好四项，每项只有 `crew_ref` 和 `job_refs`，编号匹配 `CRW-[A-Z0-9]{3}`；每个 `job_refs` 恰好两项、按字典序排列并恰好划分全部作业。」。
    """
    payload = load_payload()
    next(
        row for row in payload["resources"] if row["role"] == "shared"
    )["role"] = "entry"
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "resource_shape",
    )
    assert completed.returncode == 3
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_crew_partition_is_exact(tmp_path: Path):
    """CASE=CREW-01
    题面：「`resources` 恰好八项，每项只有 `resource_id` 和 `role`；编号匹配 `RC-[A-Z0-9]{4}` 且唯一，角色为 `entry` 的恰好四项、为 `shared` 的恰好四项。`crews` 恰好四项，每项只有 `crew_ref` 和 `job_refs`，编号匹配 `CRW-[A-Z0-9]{3}`；每个 `job_refs` 恰好两项、按字典序排列并恰好划分全部作业。」。
    """
    payload = load_payload()
    payload["crews"][1]["job_refs"] = list(payload["crews"][0]["job_refs"])
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "crew_partition",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_hourly_shape_is_exact(tmp_path: Path):
    """CASE=HOURLY-01
    题面：「`hourly` 恰好三项，`hour` 依次为 `0`、`1`、`2`；每项只有 `hour`、`busy_resources`、`completed_count`，资源数组无重复且按字典序排列，计数为 `0` 到 `8` 的整数。」。
    """
    payload = load_payload()
    payload["hourly"].pop()
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "hourly_shape",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_route_recovery_uses_resource_totals(tmp_path: Path):
    """CASE=ROUTE-01
    题面：「八小时负载已不存在，只能把三小时 `busy_resources` 的并集逐项累计：每个资源应出现两次。」。
    """
    payload = load_payload()
    payload["hourly"][0]["busy_resources"].pop()
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "route_total",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED RECOVERY_INFEASIBLE\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_family_assignment_follows_sorted_routes(baseline):
    """CASE=FAMILY-01
    题面：「先用访问总数约束选出未分配资源，再依据已列转移把入口资源接到唯一共享资源；仍有多个合法路线时，将路线按字典序排好，依次配给按 `crew_ref` 排好的作业组。」。
    """
    result, _, _, _, _, _, _ = baseline
    routes = [row["resources"] for row in result["crews"]]
    assert routes == sorted(routes)
    assert [row["family_id"] for row in result["crews"]] == [
        "FAM-A",
        "FAM-B",
        "FAM-C",
        "FAM-D",
    ]


def test_each_job_has_two_contiguous_stages(baseline):
    """CASE=EVENT-01
    题面：「路线排序后，最小的作业组归 `FAM-A`，随后到 `FAM-D`。每项作业沿本组路线执行两个连续的一小时阶段，第一阶段的开始小时不得早于 `release_hour`；同一路线上的两项作业按 `job_ref` 先后执行。恢复出的四组事件必须恰好覆盖三小时负载、两项完成计数和三处转移核对，否则返回 `RECOVERY_INFEASIBLE`。」。
    """
    result, _, _, _, _, _, _ = baseline
    assert event_hours_are_contiguous(result)
    assert len(result["events"]) == 16


def test_events_are_sorted_by_hour_resource_and_job(baseline):
    """CASE=EVENT-02
    题面：「路线排序后，最小的作业组归 `FAM-A`，随后到 `FAM-D`。每项作业沿本组路线执行两个连续的一小时阶段，第一阶段的开始小时不得早于 `release_hour`；同一路线上的两项作业按 `job_ref` 先后执行。恢复出的四组事件必须恰好覆盖三小时负载、两项完成计数和三处转移核对，否则返回 `RECOVERY_INFEASIBLE`。」。
    """
    result, _, _, _, _, _, _ = baseline
    assert event_rows_are_sorted(result["events"])


def test_release_hour_is_respected(tmp_path: Path):
    """CASE=EVENT-03
    题面：「先用访问总数约束选出未分配资源，再依据已列转移把入口资源接到唯一共享资源；仍有多个合法路线时，将路线按字典序排好，依次配给按 `crew_ref` 排好的作业组。」。
    """
    payload = load_payload()
    payload["jobs"][0]["release_hour"] = 1
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "release_window",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED RECOVERY_INFEASIBLE\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_transition_support_must_close(tmp_path: Path):
    """CASE=SUPPORT-01
    题面：「`transition_support` 每项只有 `cut_hour`、`from_resource`、`to_resource`、`count`；`cut_hour` 为 `0` 或 `1`，两端资源必须登记，`count` 为正整数，同一四元组不重复。」。
    """
    payload = load_payload()
    payload["transition_support"].pop(0)
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "support_closure",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED RECOVERY_INFEASIBLE\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_completed_counts_must_close(tmp_path: Path):
    """CASE=SUPPORT-02
    题面：「`hourly` 恰好三项，`hour` 依次为 `0`、`1`、`2`；每项只有 `hour`、`busy_resources`、`completed_count`，资源数组无重复且按字典序排列，计数为 `0` 到 `8` 的整数。」。
    """
    payload = load_payload()
    payload["hourly"][2]["completed_count"] += 1
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "completion_count",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED RECOVERY_INFEASIBLE\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_counterfactual_uses_recovered_routes(baseline):
    """CASE=SIM-01
    题面：「`strategies` 有两至三项，每项只有 `strategy_id`、`start_hour`、`horizon_hours`、`job_refs`、`family_order`；策略号为 `STR-[A-Z0-9]{4}`，窗口起点为 `0` 到 `2`、长度为 `1` 到 `4`，作业数组非空且唯一，族顺序是 `FAM-A` 至 `FAM-D` 的排列。」。
    """
    result, _, _, _, _, _, _ = baseline
    assert result["simulation"][0]["completed_count"] == 8
    assert result["simulation"][1]["completed_count"] == 8
    assert result["simulation"][2]["completed_count"] == 2


def test_strategy_window_end_is_inclusive(baseline):
    """CASE=SIM-02
    题面：「`jobs` 恰好八项，每项只有 `job_ref`、`crew_ref`、`release_hour`；编号匹配 `JOB-[A-Z0-9]{5}` 且唯一，每组两项，`release_hour` 为 `0` 或 `1`。」。
    """
    result, _, _, _, _, _, _ = baseline
    third = result["simulation"][2]
    assert third["last_finish_hour"] == 1
    assert third["completed_count"] == 2


def test_throughput_uses_window_ratio_and_half_up(tmp_path: Path):
    """CASE=SIM-03
    题面：「`strategies` 有两至三项，每项只有 `strategy_id`、`start_hour`、`horizon_hours`、`job_refs`、`family_order`；策略号为 `STR-[A-Z0-9]{4}`，窗口起点为 `0` 到 `2`、长度为 `1` 到 `4`，作业数组非空且唯一，族顺序是 `FAM-A` 至 `FAM-D` 的排列。」。
    """
    payload = load_payload()
    strategy = copy.deepcopy(payload["strategies"][0])
    strategy["horizon_hours"] = 3
    strategy["job_refs"] = [row["job_ref"] for row in payload["jobs"][:2]]
    payload["strategies"] = [strategy, copy.deepcopy(payload["strategies"][1])]
    result, _, _, _, completed, _, _ = run_payload(
        tmp_path,
        payload,
        "throughput",
    )
    assert completed.returncode == 0
    assert result["simulation"][0]["completed_count"] == 2
    assert result["simulation"][0]["throughput_per_hour"] == pytest.approx(0.667)


def test_result_layers_have_exact_keys(baseline):
    """CASE=RESULT-01
    题面：「结果根键恰好为 `schema_version`、`snapshot_id`、`run_id`、`input_digest`、`crews`、`events`、`simulation`。`schema_version` 为整数 `1`，`input_digest` 是现场原始字节的小写 SHA-256。`crews` 按族号排列，每项只有 `crew_ref`、`family_id`、`resources`；`events` 按 `(hour, resource_id, job_ref)` 排列，每项只有 `job_ref`、`crew_ref`、`family_id`、`stage`、`hour`、`resource_id`；`simulation` 按策略号排列，每项只有 `strategy_id`、`completed_count`、`throughput_per_hour`、`last_finish_hour`。」。
    """
    result, _, _, _, _, _, _ = baseline
    assert set(result) == {
        "schema_version",
        "snapshot_id",
        "run_id",
        "input_digest",
        "crews",
        "events",
        "simulation",
    }
    assert all(
        set(row) == {"crew_ref", "family_id", "resources"}
        for row in result["crews"]
    )
    assert all(
        set(row)
        == {
            "job_ref",
            "crew_ref",
            "family_id",
            "stage",
            "hour",
            "resource_id",
        }
        for row in result["events"]
    )
    assert all(
        set(row)
        == {
            "strategy_id",
            "completed_count",
            "throughput_per_hour",
            "last_finish_hour",
        }
        for row in result["simulation"]
    )


def test_result_bytes_are_canonical_json(baseline):
    """CASE=RAW-01
    题面：「结果只认紧凑 JSON：键名升序、无多余空格、末尾恰好一个换行。」。
    """
    result, _, result_path, _, _, _, _ = baseline
    assert result_path.read_bytes() == canonical_bytes(result)
    assert result_path.read_bytes().count(b"\n") == 1


def test_input_digest_uses_original_bytes(baseline):
    """CASE=RAW-02
    题面：「结果根键恰好为 `schema_version`、`snapshot_id`、`run_id`、`input_digest`、`crews`、`events`、`simulation`。`schema_version` 为整数 `1`，`input_digest` 是现场原始字节的小写 SHA-256。`crews` 按族号排列，每项只有 `crew_ref`、`family_id`、`resources`；`events` 按 `(hour, resource_id, job_ref)` 排列，每项只有 `job_ref`、`crew_ref`、`family_id`、`stage`、`hour`、`resource_id`；`simulation` 按策略号排列，每项只有 `strategy_id`、`completed_count`、`throughput_per_hour`、`last_finish_hour`。」。
    """
    result, _, _, _, _, raw, _ = baseline
    assert result["input_digest"] == hashlib.sha256(raw).hexdigest()


def test_minimum_scene_completes(tmp_path: Path):
    """CASE=LIMIT-01
    题面：「`/app/data/trace.json` 是默认现场，同目录另有最小和最大快照；程序只使用镜像内自带依赖。」。
    """
    result, _, _, _, completed, _, _ = run_payload(
        tmp_path,
        load_payload("trace_min.json"),
        "minimum",
    )
    assert completed.returncode == 0
    assert len(result["events"]) == 16
    assert result["simulation"][0]["completed_count"] == 8


def test_maximum_scene_completes_within_public_limit(tmp_path: Path):
    """CASE=LIMIT-02
    题面：「`/app/data/trace.json` 是默认现场，同目录另有最小和最大快照；程序只使用镜像内自带依赖。」。
    """
    work = tmp_path / "maximum"
    work.mkdir()
    input_path = work / "trace.json"
    result_path = work / "result.json"
    ledger_path = work / "ledger.jsonl"
    input_path.write_bytes(canonical_bytes(load_payload("trace_max.json")))
    started = time.monotonic()
    completed = subprocess.run(
        ["python3", TASK_APP, str(input_path), str(result_path), str(ledger_path)],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    elapsed = time.monotonic() - started
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert completed.returncode == 0
    assert elapsed < 5
    assert len(result["crews"]) == 4
    assert len(result["events"]) == 16


def test_upper_bound_route_search_is_not_permutation_enumeration(tmp_path: Path):
    """CASE=LIMIT-03
    题面：「公开上限固定为四个作业组、八项作业、八个资源和三个时段，从读取到发布必须在五秒内完成。」。
    """
    payload = load_payload("trace_max.json")
    result, _, _, _, completed, _, elapsed = run_payload(
        tmp_path,
        payload,
        "non_enumeration",
    )
    candidate_space = factorial(8) * factorial(4)
    assert candidate_space == 967680
    assert completed.returncode == 0
    assert elapsed < 5
    assert len(result["events"]) == 16


def test_success_channel_is_exact(tmp_path: Path):
    """CASE=CHANNEL-01
    题面：「成功时标准输出只有 `OK <run_id>\\n`，标准错误为空。业务拒收退出码为 `3`，标准输出为空，标准错误只有一行 `REFUSED <代码>\\n`；公开完整输出为 `REFUSED BAD_INPUT\\n`、`REFUSED RECOVERY_INFEASIBLE\\n`、`REFUSED LEDGER_CONFLICT\\n`、`REFUSED LEDGER_CORRUPT\\n`。」。
    """
    payload = load_payload()
    _, _, _, _, completed, _, _ = run_payload(
        tmp_path,
        payload,
        "success_channel",
    )
    assert completed.returncode == 0
    assert completed.stdout == f"OK {payload['run_id']}\n"
    assert completed.stderr == ""


def test_refusal_channel_is_exact(tmp_path: Path):
    """CASE=CHANNEL-02
    题面：「业务拒收退出码为 `3`，标准输出为空，标准错误只有一行 `REFUSED <代码>\\n`；公开完整输出为 `REFUSED BAD_INPUT\\n`、`REFUSED RECOVERY_INFEASIBLE\\n`、`REFUSED LEDGER_CONFLICT\\n`、`REFUSED LEDGER_CORRUPT\\n`。」。
    """
    payload = load_payload()
    payload.pop("strategies")
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "channel_contract",
    )
    assert completed.returncode == 3
    assert completed.stdout == ""
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_absent_root_key_is_refused(tmp_path: Path):
    """CASE=INPUT-01
    题面：「班次表以 UTF-8 文本保存，记为 JSON；`schema` 固定为 `warehouse-shift-aggregate/v3`，根键恰好为 `schema`、`snapshot_id`、`run_id`、`resources`、`crews`、`jobs`、`hourly`、`transition_support`、`strategies`；身份号匹配 `[A-Za-z0-9][A-Za-z0-9._:-]{2,63}`。」。
    """
    payload = load_payload()
    payload.pop("resources")
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "root_absence_case",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_extra_nested_key_is_bad_input(tmp_path: Path):
    """CASE=INPUT-02
    题面：「所有记录和数组项必须是对象；数字不得写成字符串或布尔值。」。
    """
    payload = load_payload()
    payload["jobs"][0]["unexpected"] = 1
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "nested_shape",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_duplicate_json_key_is_bad_input(tmp_path: Path):
    """CASE=INPUT-03
    题面：「班次表以 UTF-8 文本保存，记为 JSON；`schema` 固定为 `warehouse-shift-aggregate/v3`，根键恰好为 `schema`、`snapshot_id`、`run_id`、`resources`、`crews`、`jobs`、`hourly`、`transition_support`、`strategies`；身份号匹配 `[A-Za-z0-9][A-Za-z0-9._:-]{2,63}`。」。
    """
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        duplicate_key_raw(load_payload()),
        "object_pairs",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_boolean_integer_is_bad_input(tmp_path: Path):
    """CASE=INPUT-04
    题面：「所有记录和数组项必须是对象；数字不得写成字符串或布尔值。」。
    """
    payload = load_payload()
    payload["jobs"][0]["release_hour"] = True
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "integer_shape",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_unknown_resource_is_recovery_infeasible(tmp_path: Path):
    """CASE=INPUT-05
    题面：「`jobs` 恰好八项，每项只有 `job_ref`、`crew_ref`、`release_hour`；编号匹配 `JOB-[A-Z0-9]{5}` 且唯一，每组两项，`release_hour` 为 `0` 或 `1`。」。
    """
    payload = load_payload()
    payload["hourly"][0]["busy_resources"].append("RC-ZZZZ")
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "unregistered_reference",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED RECOVERY_INFEASIBLE\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_validation_failure_preserves_old_outputs(tmp_path: Path):
    """CASE=RECOVERY-01
    题面：「路线排序后，最小的作业组归 `FAM-A`，随后到 `FAM-D`。每项作业沿本组路线执行两个连续的一小时阶段，第一阶段的开始小时不得早于 `release_hour`；同一路线上的两项作业按 `job_ref` 先后执行。恢复出的四组事件必须恰好覆盖三小时负载、两项完成计数和三处转移核对，否则返回 `RECOVERY_INFEASIBLE`。」。
    """
    _, input_path, result_path, ledger_path, first, raw, _ = run_payload(
        tmp_path,
        load_payload(),
        "preserve",
    )
    old_result = result_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    input_path.write_bytes(raw[:-8])
    failed, _ = invoke(input_path, result_path, ledger_path)
    assert first.returncode == 0
    assert failed.returncode == 3
    assert result_path.read_bytes() == old_result
    assert ledger_path.read_bytes() == old_ledger

    input_path.write_bytes(raw)
    unwritable_ledger = Path("/sys/ly-009-unwritable-ledger.jsonl")
    write_failed, _ = invoke(input_path, result_path, unwritable_ledger)
    assert write_failed.returncode == 3
    assert write_failed.stderr == "REFUSED BAD_INPUT\n"
    assert result_path.read_bytes() == old_result
    assert ledger_path.read_bytes() == old_ledger
    assert not unwritable_ledger.exists()


def test_same_run_with_changed_input_is_conflict(tmp_path: Path):
    """CASE=LEDGER-02
    题面：「同一运行号、同一输入重跑必须保持结果和账本字节不变；同号异输入返回 `LEDGER_CONFLICT`，新运行号只能追加同一现场的新链尾。」。
    """
    payload = load_payload()
    _, input_path, result_path, ledger_path, first, _, _ = run_payload(
        tmp_path,
        payload,
        "conflict",
    )
    old_result = result_path.read_bytes()
    old_ledger = ledger_path.read_bytes()
    changed = copy.deepcopy(payload)
    changed["strategies"][0]["horizon_hours"] = 3
    input_path.write_bytes(canonical_bytes(changed))
    failed, _ = invoke(input_path, result_path, ledger_path)
    states = ["released", "rejected"]

    def audit() -> dict[str, bool]:
        return {
            "released": first.returncode == 0,
            "rejected": failed.returncode == 3,
        }

    def surface() -> dict[str, bool]:
        return {
            "result": result_path.read_bytes() == old_result,
            "ledger": ledger_path.read_bytes() == old_ledger,
        }

    transition_audit = audit()
    transition_surface = surface()
    assert first.returncode == 0
    assert failed.returncode == 3
    assert failed.stderr == "REFUSED LEDGER_CONFLICT\n"
    assert states == ["released", "rejected"]
    assert all(transition_audit.values())
    assert all(transition_surface.values())
    assert result_path.read_bytes() == old_result
    assert ledger_path.read_bytes() == old_ledger


def test_broken_ledger_is_corrupt(tmp_path: Path):
    """CASE=LEDGER-03
    题面：「复核账每行是 UTF-8 对象，键恰好为 `run_id`、`snapshot_id`、`input_digest`、`result_digest`、`previous_hash`、`entry_hash`；首行前链为六十四个零，后续前链取上一行 `entry_hash`，本行摘要按去掉 `entry_hash` 后的规范字节计算。」。
    """
    payload = load_payload()
    _, input_path, result_path, ledger_path, first, _, _ = run_payload(
        tmp_path,
        payload,
        "broken",
    )
    broken = ledger_path.read_bytes().replace(
        ('"previous_hash":"' + "0" * 64 + '"').encode(),
        ('"previous_hash":"' + "f" * 64 + '"').encode(),
        1,
    )
    ledger_path.write_bytes(broken)
    old_result = result_path.read_bytes()
    failed, _ = invoke(input_path, result_path, ledger_path)
    assert first.returncode == 0
    assert failed.returncode == 3
    assert failed.stderr == "REFUSED LEDGER_CORRUPT\n"
    assert result_path.read_bytes() == old_result
    assert ledger_path.read_bytes() == broken


def test_duplicate_ledger_key_is_corrupt(tmp_path: Path):
    """CASE=LEDGER-04
    题面：「复核账每行是 UTF-8 对象，键恰好为 `run_id`、`snapshot_id`、`input_digest`、`result_digest`、`previous_hash`、`entry_hash`；首行前链为六十四个零，后续前链取上一行 `entry_hash`，本行摘要按去掉 `entry_hash` 后的规范字节计算。」。
    """
    work = tmp_path / "duplicate_ledger"
    work.mkdir()
    input_path = work / "trace.json"
    result_path = work / "result.json"
    ledger_path = work / "ledger.jsonl"
    input_path.write_bytes(canonical_bytes(load_payload()))
    ledger_path.write_bytes(b'{"run_id":"A","run_id":"B"}\n')
    completed, _ = invoke(input_path, result_path, ledger_path)
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED LEDGER_CORRUPT\n"
    assert not result_path.exists()


def test_job_count_above_upper_bound_is_refused(tmp_path: Path):
    """CASE=LIMIT-04
    题面：「公开上限固定为四个作业组、八项作业、八个资源和三个时段，从读取到发布必须在五秒内完成。」。
    """
    payload = load_payload()
    extra = copy.deepcopy(payload["jobs"][0])
    extra["job_ref"] = "JOB-ZZZZZ"
    extra["release_hour"] = 1
    payload["jobs"].append(extra)
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "job_bound",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()


def test_duplicate_transition_support_is_bad_input(tmp_path: Path):
    """CASE=SUPPORT-03
    题面：「`transition_support` 每项只有 `cut_hour`、`from_resource`、`to_resource`、`count`；`cut_hour` 为 `0` 或 `1`，两端资源必须登记，`count` 为正整数，同一四元组不重复。」。
    """
    payload = load_payload()
    payload["transition_support"].append(
        copy.deepcopy(payload["transition_support"][0])
    )
    _, result_path, ledger_path, completed, _ = run_raw(
        tmp_path,
        canonical_bytes(payload),
        "support_pairs",
    )
    assert completed.returncode == 3
    assert completed.stderr == "REFUSED BAD_INPUT\n"
    assert not result_path.exists()
    assert not ledger_path.exists()
