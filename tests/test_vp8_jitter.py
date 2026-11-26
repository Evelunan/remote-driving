from types import SimpleNamespace

from aiortc.rtp import RtpPacket
from rd_webrtc.vp8_jitter import Vp8JitterBuffer, enable_low_latency_vp8


def packet(seq, ts, *, start=False, end=False, pt=97, ssrc=10):
    packet = RtpPacket(
        payload_type=pt,
        marker=int(end),
        sequence_number=seq,
        timestamp=ts,
        ssrc=ssrc,
        payload=bytes([0x10 if start else 0]) + b"data",
    )
    packet._data = packet.payload[1:]
    return packet


def test_release_waits_for_missing_packet_and_handles_sequence_wrap():
    jitter = Vp8JitterBuffer(8, {97})
    assert jitter.add(packet(65534, 100, start=True)) == (False, None)
    assert jitter.add(packet(0, 100, end=True)) == (False, None)
    assert jitter.add(packet(1, 200, start=True)) == (False, None)
    pli, frame = jitter.add(packet(65535, 100))
    assert not pli
    assert (frame.data, frame.timestamp) == (b"data" * 3, 100)
    assert jitter.add(packet(65535, 100)) == (False, None)
    pli, frame = jitter.add(packet(2, 200, end=True))
    assert not pli and frame.timestamp == 200


def test_partial_startup_and_unmarked_frames_are_discarded_before_valid_frame():
    jitter = Vp8JitterBuffer(8, {97})
    assert jitter.add(packet(4, 100, end=True)) == (True, None)
    assert jitter.add(packet(5, 200, start=True)) == (False, None)
    pli, frame = jitter.add(packet(6, 300, start=True, end=True))
    assert pli and frame.timestamp == 300 and frame.data == b"data"


def test_overflow_requests_keyframe_and_next_complete_frame_is_released():
    jitter = Vp8JitterBuffer(4, {97})
    assert jitter.add(packet(5, 100, start=True)) == (False, None)
    assert jitter.add(packet(7, 100, end=True)) == (False, None)
    pli, frame = jitter.add(packet(9, 200, start=True, end=True))
    assert pli and frame.timestamp == 200


def test_other_codec_keeps_timestamp_based_release():
    jitter = Vp8JitterBuffer(8, {97})
    assert jitter.add(packet(4, 100, start=True, end=True, pt=99)) == (False, None)
    pli, frame = jitter.add(packet(5, 200, start=True, end=True, pt=99))
    assert not pli and frame.timestamp == 100


def test_vp8_does_not_release_a_frame_for_a_different_arriving_codec():
    jitter = Vp8JitterBuffer(8, {97})
    assert jitter.add(packet(4, 100, start=True, end=True, pt=99)) == (False, None)
    assert jitter.add(packet(5, 200, start=True, end=True)) == (False, None)


def test_h264_first_negotiation_does_not_replace_existing_buffer():
    receiver = SimpleNamespace(_RTCRtpReceiver__jitter_buffer=object())
    upstream = receiver._RTCRtpReceiver__jitter_buffer
    transceiver = SimpleNamespace(
        receiver=receiver,
        _codecs=[
            SimpleNamespace(mimeType="video/H264", payloadType=99),
            SimpleNamespace(mimeType="video/VP8", payloadType=97),
        ],
    )
    enable_low_latency_vp8(transceiver)
    assert receiver._RTCRtpReceiver__jitter_buffer is upstream
