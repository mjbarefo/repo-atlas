"""Validate the current ATLAS map against its intentional self-audit snapshot."""

from __future__ import annotations

import json
from pathlib import Path

from atlas_analyzer.analysis.analyzer import analyze_repository
from atlas_analyzer.audit import audit_map

ROOT = Path(__file__).resolve().parents[1]
EXPECTATIONS = ROOT / "analyzer" / "tests" / "fixtures" / "self_audit_expectations.json"


def main() -> int:
    expected_payload = json.loads(EXPECTATIONS.read_text())
    report = audit_map(analyze_repository(ROOT))
    actual = {
        (
            finding.code,
            tuple(item.root for item in finding.node_ids),
        )
        for finding in report.findings
    }
    expected = {
        (
            finding["code"],
            tuple(finding["node_ids"]),
        )
        for finding in expected_payload["expected_findings"]
    }
    if report.ruleset != expected_payload["ruleset"]:
        print(
            f"Self-audit ruleset changed: expected {expected_payload['ruleset']}, "
            f"received {report.ruleset}"
        )
        return 1
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        if missing:
            print(f"Missing expected self-audit findings: {missing}")
        if unexpected:
            print(f"Unexpected self-audit findings: {unexpected}")
        return 1
    print(
        f"Self-audit verified: {report.summary.warnings} warning(s), "
        f"{report.summary.notices} notice(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
