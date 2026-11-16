from dataclasses import replace
from uuid import UUID

import pytest
from rd_core.wire import (
    Control,
    Execution,
    ExecutionStatus,
    Packet,
    StopStatus,
    Telemetry,
    conservative_age_ms,
)

EPOCH = UUID("00112233-4455-6677-8899-aabbccddeeff")
BASE = Packet(1234, EPOCH, 0xFFFFFFFE, 1234567890123, Control(0.5, 0.25, -1))
GOLDEN = bytes.fromhex(
    "5244020104d200112233445566778899aabbccddeefffffffffe0000011f71fb04cb3f0000003e800000bf800000"
)


def test_control_has_a_fixed_network_order_vector_and_roundtrips():
    assert BASE.encode() == GOLDEN
    assert Packet.decode(GOLDEN) == BASE


@pytest.mark.parametrize(
    "payload",
    [
        Execution(ExecutionStatus.EXECUTED, 1234567890456),
        Telemetry(2.5, -0.25),
        StopStatus(True, False),
    ],
)
def test_receipt_measurement_and_stop_request_have_distinct_wire_types(payload):
    packet = replace(BASE, payload=payload)
    assert Packet.decode(packet.encode()) == packet
    assert packet.kind != BASE.kind


@pytest.mark.parametrize(
    "data",
    [GOLDEN[:length] for length in (0, 2, 33, 34, 45)]
    + [
        GOLDEN + b"\0",
        b"XX" + GOLDEN[2:],
        GOLDEN[:2] + b"\1" + GOLDEN[3:],
        GOLDEN[:3] + b"\xff" + GOLDEN[4:],
        GOLDEN[:6] + b"\0" * 16 + GOLDEN[22:],
        GOLDEN[:34] + bytes.fromhex("7fc00000") + GOLDEN[38:],
    ],
)
def test_invalid_version_size_epoch_and_nan_are_rejected(data):
    with pytest.raises(ValueError):
        Packet.decode(data)


@pytest.mark.parametrize(
    "changes",
    [
        {"session": True},
        {"session": 65536},
        {"seq": -1},
        {"seq": 2**32},
        {"source_time_ms": -1},
        {"epoch": UUID(int=0)},
        {"payload": Control(0, -1, 0)},
        {"payload": Control(0, 0, float("inf"))},
        {"payload": Telemetry(1e100, 0)},
        {"payload": StopStatus(False, True)},
    ],
)
def test_encoder_rejects_invalid_local_values(changes):
    with pytest.raises(ValueError):
        replace(BASE, **changes).encode()


def test_sample_age_reserves_clock_error_and_rejects_future_and_deadline():
    assert conservative_age_ms(1000, 1100, max_age_ms=300, clock_skew_ms=50) == 150
    assert conservative_age_ms(1050, 1000, max_age_ms=300, clock_skew_ms=50) == 0
    for source in (1051, 750):
        with pytest.raises(ValueError):
            conservative_age_ms(source, 1000, max_age_ms=300, clock_skew_ms=50)
    # Repeating the original sample consumes its budget; a new sequence is not age.
    with pytest.raises(ValueError):
        conservative_age_ms(1000, 1250, max_age_ms=300, clock_skew_ms=50)
