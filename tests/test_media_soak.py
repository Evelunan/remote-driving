"""Verify the soak observes real reconnects and cannot hide failed runs."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/soak_media.py"


def test_resource_trend_includes_growth_and_zero_values():
    # Import only in this isolated test process to avoid changing the GUI backend
    # or loading Qt into the normal pytest host.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import importlib.util
spec = importlib.util.spec_from_file_location('soak', {str(SCRIPT)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
cycles = [{{'end': {{'monotonic_s': i * 60, 'rss_mib': i * 2,
    'file_descriptors': 0, 'native_threads': 1, 'asyncio_tasks': 1}}}} for i in range(4)]
trend = module.trend(cycles)
assert trend['last_half_rss_mib_per_minute'] == 2
assert trend['rss_mib'] == {{'first': 0, 'last': 6, 'min': 0, 'max': 6}}
assert trend['file_descriptors']['min'] == 0
""",
        ],
        cwd=SCRIPT.parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_four_view_soak_reconnects_both_endpoints_and_reclaims_peers(tmp_path):
    output = tmp_path / "soak.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cycles",
            "3",
            "--seconds",
            "0.5",
            "--warmup",
            "0",
            "--profile",
            "small",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["valid"] and not report["measurement_errors"]
    assert [cycle.get("reconnected_endpoint") for cycle in report["cycles"]] == [
        "publisher",
        "receiver",
        None,
    ]
    assert not report["cleanup"]["unexpected_tasks"]
    assert all(
        report["cleanup"][key]
        for key in (
            "camera_threads_closed",
            "endpoint_maps_empty",
            "server_rooms_empty",
        )
    )
    assert report["closed_after_gc"]["retired_peers_alive"] == 0
    assert report["after_worker_exit"]["python_threads"] == ["MainThread"]
    for cycle in report["cycles"]:
        for stage in ("received", "painted"):
            assert set(cycle["fps"][stage]) == {"front", "back", "left", "right"}
            assert all(value > 0 for value in cycle["fps"][stage].values())
        assert all(
            path == ["127.0.0.1", "127.0.0.1", "host", "host"]
            for path in cycle["paths"]
        )


@pytest.mark.parametrize("args", [["--cycles", "1"], ["--max-rss-mib", "nan"]])
def test_rejected_workload_cannot_leave_previous_success_report(tmp_path, args):
    output = tmp_path / "soak.json"
    output.write_text('{"valid": true}')
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(output), *args],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert json.loads(output.read_text())["valid"] is False


def test_allocator_diagnostic_runs_only_after_successful_soak(tmp_path):
    import platform

    if platform.libc_ver()[0] != "glibc":
        pytest.skip("optional glibc diagnostic")
    output, underlying = tmp_path / "memory.json", tmp_path / "soak.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT.with_name("diagnose_media_memory.py")),
            "--cycles",
            "3",
            "--seconds",
            "0.5",
            "--profile",
            "small",
            "--soak-output",
            str(underlying),
            "--output",
            str(output),
        ],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["valid"] and json.loads(underlying.read_text())["valid"]
    assert report["malloc_trim_return"] in (0, 1)
    assert report["after_trim"]["python_threads"] == ["MainThread"]
    assert (
        report["after_trim"]["monotonic_s"]
        >= report["after_final_gc"]["monotonic_s"]
        >= report["after_soak_return"]["monotonic_s"]
    )
    # Memory fragmentation and allocator caches vary: do not require a gain.
    assert isinstance(report["reclaimed_mib"], (int, float))
