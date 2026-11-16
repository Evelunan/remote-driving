"""Wire compatibility and freshness regressions for all three current codecs."""

from pathlib import Path
import runpy

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=[
    "driving/infrastructure/protocol/codec.py",
    "vehicle-unit/infrastructure/protocol/codec.py",
    "servers/quic/services/codec.py",
])
def codec(request):
    return runpy.run_path(str(ROOT / request.param))["ControlCodec"]


def test_control_packet_matches_existing_wire_format(codec):
    packet = codec.encode(
        codec.MessageType.CONTROL, 42, throttle=1, brake=0,
        steering=-1, seq=65535, timestamp=1234,
    )
    assert packet == bytes.fromhex("03002affff000004d27f0081")
    assert codec.decode(packet) == {
        "type": 3, "session": 42, "seq": 65535, "timestamp": 1234,
        "throttle": 1.0, "brake": 0.0, "steering": -1.0,
    }


@pytest.mark.parametrize("new,old,accepted", [
    (0, None, True), (40000, None, True), (65535, None, True),
    (10, 10, False), (9, 10, False), (11, 10, True),
    (0, 65535, True), (65535, 0, False), (32768, 0, False),
])
def test_sequence_boundaries(codec, new, old, accepted):
    assert codec.is_newer(new, old) is accepted


@pytest.mark.parametrize("packet", [b"", b"\x03\x00\x2a", bytes(13)])
def test_truncated_and_oversized_packets_are_rejected(codec, packet):
    with pytest.raises(ValueError):
        codec.decode(packet)
