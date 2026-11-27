"""Calibrate benchmark correlation and statistics before trusting its reports."""

import importlib.util
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from aiortc.codecs.vpx import Vp8Decoder, Vp8Encoder, VpxPayloadDescriptor
from aiortc.jitterbuffer import JitterFrame
from av import VideoFrame

spec = importlib.util.spec_from_file_location(
    "media_probe_under_test",
    Path(__file__).resolve().parents[1] / "scripts/media_probe.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_watermark_survives_real_lossy_codec_and_detects_corruption():
    encoder, decoder = Vp8Encoder(), Vp8Decoder()
    for index, view in enumerate(probe.VIEWS):
        sequence = (1, 256, 65535, 2**32 - 1)[index]
        pixels = np.random.default_rng(42).integers(
            0, 256, (240, 320, 3), dtype=np.uint8
        )
        probe.stamp(pixels, view, sequence)
        original = VideoFrame.from_ndarray(pixels, format="bgr24")
        original.pts, original.time_base = index * 3000, Fraction(1, 90000)
        payloads, timestamp = encoder.encode(original, True)
        encoded = b"".join(
            VpxPayloadDescriptor.parse(payload)[1] for payload in payloads
        )
        (decoded,) = decoder.decode(JitterFrame(encoded, timestamp))
        received = decoded.to_ndarray(format="bgr24")
        assert probe.identify(received) == (view, sequence)
        # Flip a complete payload cell: CRC must reject a plausible-looking id.
        cell = probe.CELL
        received[4 * cell : 5 * cell, 4 * cell : 5 * cell] = (
            255 - received[4 * cell : 5 * cell, 4 * cell : 5 * cell]
        )
        with pytest.raises(ValueError, match="invalid media watermark"):
            probe.identify(received)


def test_rates_keep_zero_intervals_and_normalize_partial_seconds():
    result = probe.rates([10, 10.5, 12.1, 12.4, 12.5], 10, 12.5)
    assert result == {"count": 4, "fps": 1.6, "per_second_fps": [2, 0, 4]}
    assert probe.distribution([0, 0, 10])["p50"] == 0
    assert probe.distribution([])["p99"] is None


def test_frame_correlated_latency_and_total_freeze_are_not_dropped():
    measurement = probe.Probe(limit=2)
    for sequence in (1, 2):
        for stage, timestamp in {
            "capture": 10 + sequence,
            "encoder_input": 10.01 + sequence,
            "encode_start": 10.02 + sequence,
            "encode_end": 10.04 + sequence,
            "paint": 10.1 + sequence,
        }.items():
            measurement.mark(("front", sequence), stage, timestamp)
    report = measurement.window(("front", "back"), 10, 14)
    front, absent = report["views"]["front"], report["views"]["back"]
    assert front["latency_ms"]["capture_to_qt_paint"]["p50"] == pytest.approx(100)
    assert front["latency_ms"]["encode_including_color_conversion"][
        "p99"
    ] == pytest.approx(20)
    assert absent["rates"]["paint"]["per_second_fps"] == [0, 0, 0, 0]
    assert absent["presentation_gap_ms"]["max"] == 4000
    assert absent["gaps_over_500ms"] == 1
    measurement.mark(("front", 3), "capture", 13)
    assert measurement.errors == {"record_limit_exceeded": 1}


def test_hooks_restore_runtime_even_when_measurement_fails(monkeypatch):
    import aioice.ice
    import aiortc.rtcrtpsender
    from rd_webrtc.video import VideoFrameTrack

    original = (
        aioice.ice.get_host_addresses,
        aiortc.rtcrtpsender.get_encoder,
        VideoFrameTrack.recv_if_ready,
    )
    with pytest.raises(RuntimeError, match="injected"), probe.Probe().hooks():
        assert VideoFrameTrack.recv_if_ready is not original[2]
        raise RuntimeError("injected benchmark failure")
    assert original == (
        aioice.ice.get_host_addresses,
        aiortc.rtcrtpsender.get_encoder,
        VideoFrameTrack.recv_if_ready,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda name: "unknown")
    with pytest.raises(RuntimeError, match="review benchmark hooks"):
        probe.Probe().hooks()
    assert VideoFrameTrack.recv_if_ready is original[2]


@pytest.mark.parametrize(
    "workers,profile", [(None, "upstream"), (1, "upstream"), (2, "differentiated")]
)
def test_four_view_measurement_traces_real_pipeline_and_cleans_up(
    tmp_path, workers, profile
):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "measurement.json"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/benchmark_media.py"),
            "--views",
            "4",
            "--profile",
            "small",
            "--seconds",
            "0.5",
            "--warmup",
            "0",
            "--output",
            str(output),
            "--encoding-profile",
            profile,
            *(["--encoder-workers", str(workers)] if workers is not None else []),
        ],
        cwd=tmp_path,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    assert report["valid"] and not report["measurement_errors"]
    assert report["camera_threads_closed"] and report["control_threads_closed"]
    assert report["encoding_threads_closed"]
    if workers is not None:
        assert report["encoding_pool"]["workers"] == workers
        assert report["encoding_pool"]["completed"] > 0
    if profile == "differentiated":
        assert all(
            item["implementation"] == "BudgetedVp8Encoder" and item["thread_count"] == 1
            for item in report["encoder_instances"]
        )
    assert report["control_retained_during_media_reconnect"]
    assert report["reconnect_all_views_painted_ms"] > 0
    for phase in ("steady", "recovered"):
        data = report[phase]
        assert data["samples"]["control_sample_to_mock_submit_ms"]["count"] > 0
        for view in probe.VIEWS:
            measured = data["views"][view]
            assert measured["rates"]["paint"]["count"] > 0
            assert measured["latency_ms"]["capture_to_qt_paint"]["p50"] > 0
            for distribution in measured["latency_ms"].values():
                assert distribution["count"] > 0 and distribution["p50"] >= 0
        assert data["network"]["selected_paths"]
        assert all(
            p["local"] == p["remote"] == "127.0.0.1"
            for p in data["network"]["selected_paths"]
        )
