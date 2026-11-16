"""Exercise both real deployment topologies and calibrate cross-process joins."""

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_control_probe_import_is_light_and_resource_identity_is_real(monkeypatch):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import process_probe, sys; assert not {'numpy', 'cv2', 'PyQt6', 'aiortc', 'av'} & sys.modules.keys()",
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts")},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module("process_probe")
    sample = module.process_resources(os.getpid())
    assert sample["pid"] == os.getpid() and sample["start_ticks"] > 0
    assert sample["rss_mib"] >= sample["pss_mib"] > 0
    assert sample["cpu_s"] > 0 and sample["native_threads"] >= 1


def test_trace_join_uses_original_sample_and_rejects_missing_or_duplicate(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    module = importlib.import_module("process_probe")
    probe = importlib.import_module("media_probe").Probe()
    probe.mark(("front", 7), "paint", 10.2)
    record = {
        "frames": [[["front", 7], {"capture": 10.0}]],
        "control_executions": [[1234, 10.05]],
        "role": "control",
        "loop_lag_ms": [],
        "python_threads": [],
    }
    module.merge_trace(probe, record, {1234: 10.0})
    measured = probe.window(("front",), 9, 11)
    assert measured["views"]["front"]["latency_ms"]["capture_to_qt_paint"][
        "p50"
    ] == pytest.approx(200)
    assert measured["samples"]["control_sample_to_mock_submit_ms"][
        "p50"
    ] == pytest.approx(50)
    with pytest.raises(ValueError, match="duplicate"):
        module.merge_trace(probe, record, {1234: 10.0})
    with pytest.raises(ValueError, match="source time"):
        module.merge_trace(importlib.import_module("media_probe").Probe(), record, {})


@pytest.mark.parametrize("topology", ["combined", "split"])
def test_four_view_process_benchmark_measures_actual_hosts_and_reaps_children(
    tmp_path, topology
):
    output = tmp_path / "processes.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/benchmark_processes.py"),
            "--topology",
            topology,
            "--profile",
            "small",
            "--seconds",
            "0.5",
            "--warmup",
            "0.5",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["valid"] and not report["measurement_errors"]
    assert not report["physical_display_or_stop_verified"]
    assert not report["forced_cleanup"] and report["host_exitcode"] == 0
    expected = (
        {"harness", "combined"}
        if topology == "combined"
        else {"harness", "supervisor", "control", "media"}
    )
    assert set(report["process_roles"].values()) == expected
    assert len(report["child_outcomes"]) == (2 if topology == "split" else 0)
    for record in report["component_cleanup"]:
        assert (
            not record["forbidden_control_imports"] and not record["remaining_threads"]
        )
        assert record["camera_threads_closed"]
        assert not Path(f"/proc/{record['pid']}").exists()
    for phase in ("steady", "recovered"):
        data = report[phase]
        assert set(data["resources"]) == expected | {"total"}
        total = data["resources"]["total"]
        assert total["cpu_percent_one_core"] > 0
        assert total["rss_mib"]["p50"] >= total["pss_mib"]["p50"] > 0
        assert data["samples"]["control_sample_to_mock_submit_ms"]["count"] > 0
        assert data["samples"]["control_sample_to_ack_ms"]["count"] > 0
        for view in ("front", "back", "left", "right"):
            assert data["views"][view]["rates"]["paint"]["count"] > 0
            for measurement in data["views"][view]["latency_ms"].values():
                assert measurement["count"] > 0 and measurement["p50"] >= 0
        assert report["network"][phase]["selected_paths"]
        assert all(
            item["local"] == item["remote"] == "127.0.0.1"
            for item in report["network"][phase]["selected_paths"]
        )
