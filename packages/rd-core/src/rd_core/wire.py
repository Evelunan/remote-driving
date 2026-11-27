"""Version 2 control datagrams. A receipt is never a physical-state measurement."""

import math
import struct
from dataclasses import dataclass
from enum import IntEnum
from uuid import UUID

VERSION = 2
ALPN = "rd-control/2"
CONTROL_REVISION = "direct-control/4"
_HEADER = struct.Struct("!2sBBH16sIQ")


def parse_control_route(params: dict) -> tuple[int, UUID | None, str | None]:
    """Validate a relay's connection-scoped route change, including an offline peer."""
    if not isinstance(params, dict) or set(params) != {"revision", "epoch", "vehicle_principal"}:
        raise ValueError("invalid control route")
    revision = params["revision"]
    if type(revision) is not int or revision <= 0:
        raise ValueError("invalid route revision")
    value, principal = params["epoch"], params["vehicle_principal"]
    if value is None and principal is None:
        return revision, None, None
    if not isinstance(value, str) or not isinstance(principal, str) or not 1 <= len(principal) <= 128:
        raise ValueError("invalid control route identity")
    epoch = UUID(value)
    if epoch.int == 0:
        raise ValueError("empty control route epoch")
    return revision, epoch, principal


class Kind(IntEnum):
    CONTROL = 1
    EXECUTION = 2
    TELEMETRY = 3
    STOP_STATUS = 4
    WINDOW = 5
    VEHICLE_STATUS = 6


class ExecutionStatus(IntEnum):
    EXECUTED = 1  # Device API completed; does not prove physical motion/stop.
    REJECTED = 2
    NO_CONTROLLER = 3
    INVALIDATED = 4


def _uint(value, bits, field):
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ValueError(f"invalid {field}")


def _finite(*values):
    if any(
        type(value) not in (float, int) or not math.isfinite(value) for value in values
    ):
        raise ValueError("non-finite or non-numeric payload")


@dataclass(frozen=True)
class Window:
    token: bytes

    def validate(self):
        if type(self.token) is not bytes or len(self.token) != 16:
            raise ValueError("invalid vehicle window")


@dataclass(frozen=True)
class Control:
    throttle: float
    brake: float
    steering: float
    window: bytes

    def validate(self):
        Window(self.window).validate()
        _finite(self.throttle, self.brake, self.steering)
        if not (
            -1 <= self.throttle <= 1
            and 0 <= self.brake <= 1
            and -1 <= self.steering <= 1
        ):
            raise ValueError("control outside normalized range")


@dataclass(frozen=True)
class Execution:
    status: ExecutionStatus
    completed_at_ms: int

    def validate(self):
        if not isinstance(self.status, ExecutionStatus):
            raise ValueError("invalid execution status")  # noqa: TRY004 - uniform wire validation error
        _uint(self.completed_at_ms, 64, "execution time")


@dataclass(frozen=True)
class Telemetry:
    linear_mps: float
    angular_radps: float

    def validate(self):
        _finite(self.linear_mps, self.angular_radps)


@dataclass(frozen=True)
class VehicleStatus:
    """Chassis feedback; None means unavailable, never a measured zero.

    Vehicle and wheel speeds use m/s; steering angle uses degrees.
    Field order also defines validity-mask bits 0..7 on the wire.
    Gear and mode are raw chassis uint8 codes, without inferred mappings.
    """

    speed_mps: float | None = None
    steering_deg: float | None = None
    left_wheel_mps: float | None = None
    right_wheel_mps: float | None = None
    shift_level: int | None = None
    is_autodrive: bool | None = None
    drive_mode: int | None = None
    p_status: bool | None = None

    def validate(self):
        for index, (name, value) in enumerate(vars(self).items()):
            if value is None:
                continue
            if index < 4:
                _finite(value)
            elif name in ("is_autodrive", "p_status"):
                if type(value) is not bool:
                    raise ValueError("invalid chassis boolean")
            else:
                _uint(value, 8, name)

    def wire_fields(self):
        values = tuple(vars(self).values())
        mask = sum(1 << index for index, value in enumerate(values) if value is not None)
        return (mask, *(0 if value is None else value for value in values))


@dataclass(frozen=True)
class StopStatus:
    requested: bool
    confirmed: bool

    def validate(self):
        if type(self.requested) is not bool or type(self.confirmed) is not bool:
            raise ValueError("stop flags must be boolean")
        if self.confirmed and not self.requested:
            raise ValueError("stop cannot be confirmed without a request")


_LAYOUTS = {
    Kind.CONTROL: (Control, struct.Struct("!fff16s")),
    Kind.EXECUTION: (Execution, struct.Struct("!BQ")),
    Kind.WINDOW: (Window, struct.Struct("!16s")),
    Kind.TELEMETRY: (Telemetry, struct.Struct("!ff")),
    Kind.STOP_STATUS: (StopStatus, struct.Struct("!BB")),
    Kind.VEHICLE_STATUS: (VehicleStatus, struct.Struct("!BffffBBBB")),
}


@dataclass(frozen=True)
class Packet:
    """The epoch identifies the status stream for VehicleStatus and the paired route otherwise."""

    session: int
    epoch: UUID
    seq: int
    source_time_ms: int
    payload: Control | Execution | Telemetry | StopStatus | Window | VehicleStatus

    @property
    def kind(self):
        for kind, (model, _) in _LAYOUTS.items():
            if type(self.payload) is model:
                return kind
        raise ValueError("unknown payload type")

    def encode(self) -> bytes:
        _uint(self.session, 16, "session")
        _uint(self.seq, 32, "sequence")
        _uint(self.source_time_ms, 64, "source timestamp")
        if not isinstance(self.epoch, UUID) or self.epoch.int == 0:
            raise ValueError("packet epoch must be a nonzero UUID")
        kind = self.kind
        self.payload.validate()
        fields = tuple(vars(self.payload).values())
        if kind == Kind.VEHICLE_STATUS:
            fields = self.payload.wire_fields()
        try:
            return _HEADER.pack(
                b"RD",
                VERSION,
                kind,
                self.session,
                self.epoch.bytes,
                self.seq,
                self.source_time_ms,
            ) + _LAYOUTS[kind][1].pack(*fields)
        except (struct.error, OverflowError):
            raise ValueError("payload cannot be represented on the wire") from None

    @classmethod
    def decode(cls, data: bytes):
        if len(data) < _HEADER.size:
            raise ValueError("truncated header")
        magic, version, raw_kind, session, epoch, seq, timestamp = _HEADER.unpack_from(
            data
        )
        if magic != b"RD" or version != VERSION:
            raise ValueError("unsupported datagram version")
        try:
            kind = Kind(raw_kind)
        except ValueError:
            raise ValueError("unknown datagram type") from None
        model, layout = _LAYOUTS[kind]
        if len(data) != _HEADER.size + layout.size:
            raise ValueError("invalid datagram length")
        fields = layout.unpack_from(data, _HEADER.size)
        if kind == Kind.EXECUTION:
            fields = (ExecutionStatus(fields[0]), fields[1])
        elif kind == Kind.STOP_STATUS:
            if any(value not in (0, 1) for value in fields):
                raise ValueError("invalid stop flags")
            fields = tuple(bool(value) for value in fields)
        elif kind == Kind.VEHICLE_STATUS:
            mask, *values = fields
            _finite(*values[:4])
            for index, value in enumerate(values):
                if not mask & (1 << index):
                    if value != 0:
                        raise ValueError("nonzero unavailable chassis field")
                    values[index] = None
                elif index in (5, 7):
                    if value not in (0, 1):
                        raise ValueError("invalid chassis boolean")
                    values[index] = bool(value)
            fields = values
        payload = model(*fields)
        payload.validate()
        identifier = UUID(bytes=epoch)
        if identifier.int == 0:
            raise ValueError("invalid stream epoch")
        return cls(session, identifier, seq, timestamp, payload)


def conservative_age_ms(
    source_time_ms: int, now_ms: int, *, max_age_ms: int, clock_skew_ms: int
) -> int:
    """Upper age bound, assuming sender/receiver clock error <= clock_skew_ms.

    The source timestamp must remain unchanged when a cached sample is repeated.
    A local monotonic watchdog must still enforce expiry between messages.
    """
    _uint(source_time_ms, 64, "source timestamp")
    _uint(now_ms, 64, "current timestamp")
    if not 0 <= clock_skew_ms < max_age_ms:
        raise ValueError("invalid age budget")
    delta = now_ms - source_time_ms
    if delta < -clock_skew_ms:
        raise ValueError("timestamp too far in the future")
    age = max(0, delta + clock_skew_ms)
    if age >= max_age_ms:
        raise ValueError("sample expired")
    return age
