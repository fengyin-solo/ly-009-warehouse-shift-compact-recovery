"""Attach stable behavior identifiers to pytest completion output."""

from __future__ import annotations

import re


CASE_PATTERN = re.compile(r"CASE=([A-Z][A-Z0-9_-]*-[0-9]{2,})")
_REPORTS: dict[str, tuple[object, object]] = {}


def pytest_runtest_logreport(report) -> None:
    """Remember the final call report for each test item."""
    if report.when == "call" or report.failed:
        _REPORTS[report.nodeid] = (report, report)


def pytest_sessionfinish(session, exitstatus) -> None:
    """Print one stable OBSERVED_CASE line for every collected test."""
    del exitstatus
    for item in session.items:
        report_pair = _REPORTS.get(item.nodeid)
        report = report_pair[0] if report_pair else None
        docstring = getattr(item.obj, "__doc__", "") or ""
        case_ids = CASE_PATTERN.findall(docstring) or ["UNMAPPED-00"]
        verdict = "PASSED" if report is not None and report.passed else "FAILED"
        for case_id in case_ids:
            print(f"\nOBSERVED_CASE {case_id} {verdict} {item.nodeid}")
