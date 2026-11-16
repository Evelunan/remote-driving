"""Reject topology comparisons when their underlying conditions differ."""

import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMBINED = sorted(
    (ROOT / "benchmarks/results").glob("process-b25-combined-4view-r*.json")
)
SPLIT = sorted((ROOT / "benchmarks/results").glob("process-b25-split-4view-r*.json"))


@pytest.fixture
def comparison(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("compare_processes")


def test_recorded_topologies_have_identical_input_and_three_independent_runs(
    comparison,
):
    report = comparison.compare(COMBINED, SPLIT)
    assert report["comparable"]
    assert len(report["inputs"]["combined"]) == len(report["inputs"]["split"]) == 3
    assert (
        report["steady"]["samples"]["control_sample_to_ack_ms"]["p95"]["before"]["runs"]
        == 3
    )
    assert report["steady"]["resources"]["pss_mib"]["p95"]["before"]["median"] > 0


@pytest.mark.parametrize(
    "fault", ["workload", "source", "encoder", "cleanup", "invalid"]
)
def test_different_conditions_cannot_produce_a_performance_claim(
    comparison, tmp_path, fault
):
    paths = []
    for index, source in enumerate(SPLIT):
        report = json.loads(source.read_text())
        if fault == "workload":
            report["workload"]["capture_fps"] = 15
        elif fault == "source":
            report["source_sha256"]["scripts/process_probe.py"] = "modified"
        elif fault == "encoder":
            report["encoder_instances"][0]["thread_count"] = 16
        elif fault == "cleanup":
            report["component_cleanup"][0]["remaining_threads"] = ["rd-device"]
        else:
            report["valid"] = False
        path = tmp_path / f"changed-{index}.json"
        path.write_text(json.dumps(report))
        paths.append(path)
    with pytest.raises(ValueError):
        comparison.compare(COMBINED, paths)


def test_missing_or_duplicate_runs_are_rejected(comparison):
    with pytest.raises(ValueError, match="three runs"):
        comparison.compare(COMBINED[:2], SPLIT)
    with pytest.raises(ValueError, match="duplicate"):
        comparison.compare(COMBINED, [SPLIT[0], SPLIT[0], SPLIT[2]])
