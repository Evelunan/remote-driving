from types import SimpleNamespace

import pytest
from rd_webrtc import video_timing as timing


def test_clock_matches_each_views_rebased_pts_through_wrap_and_probe_loss(monkeypatch):
    now = 100.04
    monkeypatch.setattr(
        timing,
        "time",
        SimpleNamespace(monotonic=lambda: now, monotonic_ns=lambda: round(now * 1e9)),
    )
    clock = timing.ReceiverTiming()
    sent = round(100.0 * 1e9)
    clock.pending = sent
    clock.next_probe = 101
    clock.channel = SimpleNamespace(
        readyState="open", bufferedAmount=0, send=lambda _: None
    )
    message = (
        timing.PROBE.pack(sent)
        + timing.ANCHOR.pack(0, 900)
        + timing.ANCHOR.pack(1, 40_000)
    )
    clock._reply(message)

    def capture(name, origin, pts):
        receiver = SimpleNamespace(track=SimpleNamespace(id=name))
        receiver._RTCRtpReceiver__timestamp_mapper = SimpleNamespace(_origin=origin)
        return clock.capture_time(receiver, pts)

    # Both tracks lost their first frame. Only front has wrapped since.
    assert capture("front", -10_000, 1_900) == pytest.approx(99.92)
    assert capture("back", 10_000, 12_000) == pytest.approx(99.82)
    assert capture("left", 0, 0) is None
    now += 6
    assert capture("back", 10_000, 12_000) is None
    clock._reply(message)  # delayed reply to the old probe cannot restore timing
    assert capture("back", 10_000, 12_000) is None
