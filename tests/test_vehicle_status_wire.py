from dataclasses import replace
from uuid import UUID

import pytest
from rd_core.wire import CONTROL_REVISION, Kind, Packet, VehicleStatus

BASE = Packet(1234, UUID("00112233-4455-6677-8899-aabbccddeeff"), 7, 1000,
              VehicleStatus(3.5, -12.5, 3.0, 4.0, 2, True, 5, False))


def test_chassis_status_has_fixed_units_order_and_roundtrip():
    encoded = BASE.encode()
    assert BASE.kind == Kind.VEHICLE_STATUS
    assert encoded[34:].hex() == "ff40600000c1480000404000004080000002010500"
    assert Packet.decode(encoded) == BASE
    assert CONTROL_REVISION == "direct-control/4"


@pytest.mark.parametrize("payload", [
    VehicleStatus(),
    VehicleStatus(speed_mps=0, is_autodrive=False, p_status=False),
    VehicleStatus(steering_deg=-27, shift_level=255, drive_mode=0),
])
def test_missing_fields_survive_distinct_from_zero_and_false(payload):
    packet = replace(BASE, payload=payload)
    assert Packet.decode(packet.encode()) == packet


@pytest.mark.parametrize("changes", [
    {"speed_mps": float("nan")}, {"steering_deg": float("inf")},
    {"left_wheel_mps": True}, {"right_wheel_mps": 1e100},
    {"shift_level": -1}, {"shift_level": 256}, {"drive_mode": True},
    {"is_autodrive": 1}, {"p_status": "false"},
])
def test_invalid_local_status_is_rejected(changes):
    with pytest.raises(ValueError):
        replace(BASE, payload=replace(BASE.payload, **changes)).encode()


@pytest.mark.parametrize("offset,value", [(52, 2), (54, 255), (34, 0)])
def test_bad_boolean_or_mask_is_rejected(offset, value):
    encoded = bytearray(BASE.encode())
    encoded[offset] = value
    with pytest.raises(ValueError):
        Packet.decode(encoded)


def test_invalid_size_and_nonfinite_wire_value_are_rejected():
    encoded = BASE.encode()
    for broken in (encoded[:-1], encoded + b"\0",
                   encoded[:35] + bytes.fromhex("7fc00000") + encoded[39:]):
        with pytest.raises(ValueError):
            Packet.decode(broken)
