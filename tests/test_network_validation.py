"""Full WebRTC presentation and control recovery, without synthetic video permits."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "case",
    [
        "mild",
        "rpc-only",
        "control-blackhole",
        "media-blackhole",
        "clock-budget",
        "signaling-restart",
        "relay-restart",
        "server-restart",
    ],
)
def test_network_faults_preserve_recovery_contract_and_close_resources(tmp_path, case):
    output = tmp_path / "result.json"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/validate_network.py"),
            "--case",
            case,
            "--seconds",
            "2" if case == "mild" else "0.5",
            "--output",
            str(output),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=70,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["passed"] and not report["presentation_errors"]
    assert not report["physical_stop_verified"]
    assert report["camera_threads_closed"] and report["device_threads_closed"]
    assert report["actuator_closed"] and report["media_tasks_closed"]
    for endpoint in report["udp_cleanup"]["endpoints"]:
        assert (
            endpoint["closed"]
            and endpoint["pending_packets"] == endpoint["queued_bytes"] == 0
        )
    assert not report["udp_cleanup"]["errors"]
    for view in ("front", "back", "left", "right"):
        assert (
            report["frame_counts_after"]["painted"][view]
            > report["frame_counts_before"]["painted"][view]
        )
    for key in ("initial_paths", "final_paths"):
        assert report[key]
        assert all(
            path["local"] == path["remote"] == "127.0.0.1" for path in report[key]
        )
    if case == "mild":
        assert report["control_retained"] and report["sample_to_execution_ack_ms"]
    else:
        assert (
            report["held_input_did_not_rearm"]
            and report["new_epoch_after_fresh_neutral"]
        )
        assert report["zero_target_until_neutral_recovery"]
        assert 0 <= report["fault_to_mock_zero_ms"] < 5000
        assert report["recovery_to_new_epoch_ms"] > report["recovery_to_all_views_ms"]
        if case.endswith("blackhole"):
            category = case.split("-", 1)[0]
            assert (
                sum(
                    endpoint["stats"].get("blackhole_drops", 0)
                    for endpoint in report["fault_udp"]["endpoints"]
                    if endpoint["category"] == category
                )
                > 0
            )
        if case == "media-blackhole":
            assert report["control_transport_survived_video_loss"]
        if case == "clock-budget":
            assert report["clock_budget_rejections"]
        if case == "rpc-only":
            assert report["reliable_rpc_survived_datagram_loss"]
            assert report["suppressed_application_datagrams"] > 0
