"""Prevent invalid or differently measured runs from becoming an A/B claim."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "media_comparison", ROOT / "scripts/compare_media.py"
)
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)
BASELINE = sorted((ROOT / "benchmarks/results").glob("media-b18-1view-r*.json"))


def copies(tmp_path, transform):
    paths = []
    for index, source in enumerate(BASELINE):
        report = json.loads(source.read_text())
        transform(report)
        path = tmp_path / f"after-{index}.json"
        path.write_text(json.dumps(report))
        paths.append(path)
    return paths


def test_same_measured_group_has_zero_change_and_retains_zero_metrics():
    report = comparison.compare(BASELINE, BASELINE)
    assert report["comparable"] and not report["changed_sources"]
    steady = report["cases"]["1"]["steady"]
    assert steady["process_cpu_percent_one_core"]["delta"] == 0
    latency = steady["views"]["front"]["latency_ms"]["capture_to_qt_paint"]["p95"]
    assert latency["delta"] == 0 and latency["before"]["runs"] == 3
    empty = steady["views"]["front"]["gaps_over_500ms"]
    assert empty["before"]["median"] == 0 and empty["change_percent"] is None
    assert report["cases"]["1"]["reconnect_new_control_epoch_ms"]["delta"] == 0


def test_reconnect_comparison_requires_consistent_control_evidence(tmp_path):
    def retained(report):
        del report["reconnect_new_control_epoch_ms"]
        report["control_retained_during_media_reconnect"] = True

    runs = copies(tmp_path, retained)
    case = comparison.compare(runs, runs)["cases"]["1"]
    assert case["control_retained_during_media_reconnect"] is True
    assert "reconnect_new_control_epoch_ms" not in case
    with pytest.raises(ValueError, match="control recovery"):
        comparison.compare(BASELINE, runs)
    sample = json.loads(runs[0].read_text())
    del sample["control_retained_during_media_reconnect"]
    runs[0].write_text(json.dumps(sample))
    with pytest.raises(ValueError, match="control recovery"):
        comparison.compare(runs, runs)


def test_missing_frame_latency_is_not_dropped_from_the_comparison():
    result = comparison.difference([None, 10, 11], [8, 9, 10])
    assert result["before"]["median"] is None
    assert result["before"]["per_run"] == [None, 10, 11]
    assert result["delta"] is result["change_percent"] is None
    assert (
        comparison.cpu_per_frame(
            {
                "process_cpu_percent_one_core": 5,
                "views": {"front": {"rates": {"paint": {"fps": 0}}}},
            }
        )
        is None
    )


@pytest.mark.parametrize("fault", ["invalid", "workload", "probe", "codec", "cleanup"])
def test_changed_conditions_and_invalid_runs_are_rejected(tmp_path, fault):
    def change(report):
        if fault == "invalid":
            report["valid"] = False
        elif fault == "workload":
            report["workload"]["capture_fps"] = 15
        elif fault == "probe":
            report["source_sha256"][comparison.PROBES[0]] = "different probe"
        elif fault == "codec":
            report["encoder_instances"][0]["thread_count"] = 32
        elif fault == "cleanup":
            report["camera_threads_closed"] = False

    after = copies(tmp_path, change)
    with pytest.raises(ValueError):
        comparison.compare(BASELINE, after)


def test_independent_repeated_runs_required():
    with pytest.raises(ValueError, match="duplicate"):
        comparison.compare([BASELINE[0], BASELINE[0], BASELINE[2]], BASELINE)
    with pytest.raises(ValueError, match="three runs"):
        comparison.compare(BASELINE[:2], BASELINE[:2])


def test_runtime_change_is_identified_without_changing_workload(tmp_path):
    def change(report):
        report["source_sha256"]["packages/rd-webrtc/src/rd_webrtc/video.py"] = (
            "candidate"
        )
        for phase in ("steady", "recovered"):
            report[phase]["views"]["front"]["latency_ms"]["capture_to_qt_paint"][
                "p95"
            ] *= 0.75

    report = comparison.compare(BASELINE, copies(tmp_path, change))
    assert report["changed_sources"] == ["packages/rd-webrtc/src/rd_webrtc/video.py"]
    latency = report["cases"]["1"]["steady"]["views"]["front"]["latency_ms"][
        "capture_to_qt_paint"
    ]["p95"]
    assert latency["change_percent"] == pytest.approx(-25)


def test_controlled_encoding_experiment_records_variation_and_rejects_other_changes(
    tmp_path,
):
    def change(report):
        report["workload"]["encoder_workers"] = 2
        report["encoding_threads_closed"] = True

    after = copies(tmp_path, change)
    with pytest.raises(ValueError, match="workload"):
        comparison.compare(BASELINE, after)
    report = comparison.compare(BASELINE, after, vary_encoding=True)
    assert report["cases"]["1"]["after_workload"]["encoder_workers"] == 2
    assert "quality equivalence is not established" in report["comparison_kind"]

    def different_source(report):
        change(report)
        report["source_sha256"]["new-code.py"] = "changed"

    with pytest.raises(ValueError, match="identical source"):
        comparison.compare(
            BASELINE, copies(tmp_path, different_source), vary_encoding=True
        )

    def different_input(report):
        change(report)
        report["workload"]["capture_fps"] = 15

    with pytest.raises(ValueError, match="workload"):
        comparison.compare(
            BASELINE, copies(tmp_path, different_input), vary_encoding=True
        )

    def leaked_workers(report):
        change(report)
        report["encoding_threads_closed"] = False

    with pytest.raises(ValueError, match="cleanup"):
        comparison.compare(
            BASELINE, copies(tmp_path, leaked_workers), vary_encoding=True
        )
