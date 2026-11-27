from dataclasses import replace
from uuid import UUID

import pytest
from rd_core.configuration import load_config
from rd_core.settings import DrivingFeedbackSettings, DrivingSettings
from rd_core.wire import (
    CONTROL_REVISION,
    Kind,
    Packet,
    VehicleStatus,
    VehicleStatusReply,
    VehicleStatusRequest,
)

BASE = Packet(1234, UUID("00112233-4455-6677-8899-aabbccddeeff"), 7, 1000,
              VehicleStatus(3.5, -12.5, 3.0, 4.0, 2, True, 5, False))
REQUEST_ID = bytes.fromhex("112233445566778899aabbccddeeff00")


def test_chassis_status_has_fixed_units_order_and_roundtrip():
    encoded = BASE.encode()
    assert BASE.kind == Kind.VEHICLE_STATUS
    assert encoded[34:].hex() == "ff40600000c1480000404000004080000002010500"
    assert Packet.decode(encoded) == BASE
    assert CONTROL_REVISION == "direct-control/5"


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


def test_status_request_and_reply_roundtrip_with_existing_field_layout():
    request = replace(BASE, source_time_ms=0, payload=VehicleStatusRequest(REQUEST_ID))
    status = VehicleStatus(speed_mps=0, p_status=False)
    reply = replace(BASE, payload=VehicleStatusReply(REQUEST_ID, 10_000, status))
    encoded = reply.encode()

    assert request.kind == Kind.VEHICLE_STATUS_REQUEST == 7
    assert request.encode()[34:] == REQUEST_ID
    assert Packet.decode(request.encode()) == request
    assert reply.kind == Kind.VEHICLE_STATUS_REPLY == 8
    assert encoded[34:52] == REQUEST_ID + b"\x27\x10"
    assert encoded[52:] == replace(BASE, payload=status).encode()[34:]
    assert Packet.decode(encoded) == reply


@pytest.mark.parametrize("request_id", [b"", bytes(16), bytes(17), bytearray(REQUEST_ID)])
def test_request_and_reply_reject_invalid_request_ids(request_id):
    for payload in (VehicleStatusRequest(request_id), VehicleStatusReply(request_id, 0, VehicleStatus())):
        with pytest.raises(ValueError, match="request ID"):
            replace(BASE, payload=payload).encode()


@pytest.mark.parametrize("age", [-1, 10_001, 65_536, 1.0, True])
def test_reply_rejects_invalid_sample_ages(age):
    with pytest.raises(ValueError, match="sample age"):
        replace(BASE, payload=VehicleStatusReply(REQUEST_ID, age, VehicleStatus())).encode()


def test_decode_rejects_invalid_request_id_reply_age_and_status_fields():
    request = replace(BASE, source_time_ms=0, payload=VehicleStatusRequest(REQUEST_ID)).encode()
    reply = replace(BASE, payload=VehicleStatusReply(REQUEST_ID, 0, BASE.payload)).encode()
    for invalid in (
        request[:34] + bytes(16),
        reply[:34] + bytes(16) + reply[50:],
        reply[:50] + b"\x27\x11" + reply[52:],
        reply[:52] + b"\0" + reply[53:],
        reply[:-1] + b"\x02",
    ):
        with pytest.raises(ValueError):
            Packet.decode(invalid)


def test_driving_feedback_settings_load_from_yaml_and_environment(tmp_path):
    path = tmp_path / "driving.yaml"
    path.write_text("feedback:\n  request_hz: 15.5\n  max_age_ms: 350\n", encoding="utf-8")
    config = load_config(
        DrivingSettings, "driving", path=path, environ={"RD_DRIVING__FEEDBACK__MAX_AGE_MS": "400"}
    )

    assert DrivingSettings().feedback == DrivingFeedbackSettings(10, 300)
    assert config.value.feedback == DrivingFeedbackSettings(15.5, 400)
    assert config.sources["feedback.request_hz"] == "file"
    assert config.sources["feedback.max_age_ms"] == "environment"


@pytest.mark.parametrize("changes", [
    {"request_hz": 0}, {"request_hz": 50.1}, {"request_hz": float("inf")},
    {"max_age_ms": 0}, {"max_age_ms": 10_001}, {"max_age_ms": 300.5},
])
def test_driving_feedback_settings_reject_invalid_rates_and_ages(changes):
    with pytest.raises(ValueError):
        replace(DrivingFeedbackSettings(), **changes)
