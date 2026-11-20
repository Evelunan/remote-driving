"""Full WebRTC presentation and control recovery, without synthetic video permits."""

import json
import os
import shutil
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
        "window-budget",
        "signaling-restart",
        "relay-restart",
        "server-restart",
        "turn-relay",
        "turn-outage",
    ],
)
def test_network_faults_preserve_recovery_contract_and_close_resources(tmp_path, case):
    output = tmp_path / "result.json"
    turn_options = []
    if case.startswith("turn-"):
        executable = os.environ.get("RD_TEST_TURNSERVER") or shutil.which("turnserver")
        if executable is None:
            if os.environ.get("RD_TEST_REQUIRE_TURN") == "1":
                pytest.fail("coturn is required for this acceptance run")
            pytest.skip("install coturn or set RD_TEST_TURNSERVER for TURN acceptance")
        turn_options = [
            "--turnserver",
            executable,
            "--diagnostic-consent-interval",
            "0.2",
        ]
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
            *turn_options,
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=110 if turn_options else 70,
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
    if turn_options:
        assert report["workload"]["diagnostic_consent_interval"] == 0.2
        fixture = report["turn_fixture"]
        assert not fixture["running"] and fixture["binary_sha256"]
        assert not any(exit["forced_cleanup"] for exit in fixture["exits"])
        assert fixture["exits"][-1]["exitcode"] in (0, -15)
        assert not fixture["exits"][-1]["intentional_crash"]
        assert len(fixture["exits"]) == (2 if case == "turn-outage" else 1)
        if case == "turn-outage":
            assert fixture["exits"][0]["intentional_crash"]
            assert fixture["exits"][0]["exitcode"] == -9
        for key in ("initial_paths", "final_paths"):
            assert all(
                path["local_type"] == path["remote_type"] == "relay"
                for path in report[key]
            )
    if case in {"mild", "turn-relay"}:
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
        if case == "window-budget":
            assert report["window_round_trip_floor_ms"] > 300
        if case == "rpc-only":
            assert report["reliable_rpc_survived_datagram_loss"]
            assert report["suppressed_application_datagrams"] > 0
