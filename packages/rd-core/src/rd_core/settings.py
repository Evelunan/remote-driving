"""Role schemas. Units are part of field names; credentials are environment references."""

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

Role = Literal["driving", "vehicle", "viewer"]
Environment = Literal["development", "production"]


def _positive(**values):
    for name, value in values.items():
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")


def _port(port):
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")


def _env_name(value):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("invalid credential environment variable name")


@dataclass(frozen=True)
class LoggingSettings:
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    directory: Path | None = None


@dataclass(frozen=True)
class TLSSettings:
    cert: Path | None = None
    key: Path | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PrincipalSettings:
    id: str = ""
    role: Role = "viewer"
    token_env: str = "RD_TOKEN"
    sessions: tuple[int, ...] = ()
    rooms: tuple[str, ...] = ()
    expires_at: str | None = None

    def __post_init__(self):
        _env_name(self.token_env)
        if not self.id or len(self.id) > 128:
            raise ValueError("principal id must have 1..128 characters")
        if len(set(self.sessions)) != len(self.sessions) or any(
            not 0 <= item <= 65535 for item in self.sessions
        ):
            raise ValueError("principal sessions must be unique uint16 values")
        if len(set(self.rooms)) != len(self.rooms) or any(
            not room or len(room) > 128 for room in self.rooms
        ):
            raise ValueError(
                "principal rooms must be unique, nonempty, and at most 128 characters"
            )
        if self.expires_at is not None:
            try:
                parsed = datetime.fromisoformat(self.expires_at)
                if parsed.utcoffset() is None:
                    raise ValueError
            except ValueError:
                raise ValueError(
                    "expires_at must be an ISO timestamp with timezone"
                ) from None


@dataclass(frozen=True)
class AuthSettings:
    principals: tuple[PrincipalSettings, ...] = ()

    def __post_init__(self):
        if len({item.id for item in self.principals}) != len(self.principals):
            raise ValueError("principal ids must be unique")


@dataclass(frozen=True)
class SignalingSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    max_peers_per_room: int = 2
    max_message_bytes: int = 65536
    ping_interval_s: float = 20.0
    ping_timeout_s: float = 20.0
    send_timeout_s: float = 3.0
    close_timeout_s: float = 3.0
    tls: TLSSettings = field(default_factory=TLSSettings)

    def __post_init__(self):
        _port(self.port)
        _positive(
            max_peers_per_room=self.max_peers_per_room,
            max_message_bytes=self.max_message_bytes,
            ping_interval_s=self.ping_interval_s,
            ping_timeout_s=self.ping_timeout_s,
            send_timeout_s=self.send_timeout_s,
            close_timeout_s=self.close_timeout_s,
        )


@dataclass(frozen=True)
class RelaySettings:
    host: str = "127.0.0.1"
    port: int = 4433
    tls: TLSSettings = field(default_factory=TLSSettings)
    idle_timeout_s: float = 10.0
    register_timeout_s: float = 5.0
    lease_ttl_s: float = 1.0
    max_control_age_ms: int = 300
    max_clock_skew_ms: int = 50
    max_datagram_bytes: int = 1200
    max_rpc_bytes: int = 16384

    def __post_init__(self):
        _port(self.port)
        _positive(
            idle_timeout_s=self.idle_timeout_s,
            register_timeout_s=self.register_timeout_s,
            lease_ttl_s=self.lease_ttl_s,
            max_control_age_ms=self.max_control_age_ms,
            max_datagram_bytes=self.max_datagram_bytes,
            max_rpc_bytes=self.max_rpc_bytes,
        )
        if not 0 <= self.max_clock_skew_ms < self.max_control_age_ms:
            raise ValueError("max_clock_skew_ms must be below max_control_age_ms")
        if self.max_datagram_bytes > 65536 or self.max_rpc_bytes > 1_048_576:
            raise ValueError("protocol buffer limits are too large")
        if self.register_timeout_s > 60:
            raise ValueError("register_timeout_s must not exceed 60")
        if self.lease_ttl_s * 1000 <= self.max_control_age_ms:
            raise ValueError("lease_ttl_s must exceed max_control_age_ms / 1000")


@dataclass(frozen=True)
class ServerSettings:
    environment: Environment = "development"
    services: tuple[Literal["signaling", "relay"], ...] = ("signaling", "relay")
    allow_legacy_development: bool = False
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    signaling: SignalingSettings = field(default_factory=SignalingSettings)
    relay: RelaySettings = field(default_factory=RelaySettings)

    def validate(self):
        if not self.services or len(set(self.services)) != len(self.services):
            raise ValueError("services must be nonempty and unique")
        if self.allow_legacy_development:
            if self.environment != "development" or any(
                getattr(self, name).host not in {"127.0.0.1", "::1"}
                for name in self.services
            ):
                raise ValueError(
                    "legacy protocol is restricted to development on loopback"
                )
        elif not self.auth.principals:
            raise ValueError("authenticated services require auth.principals")
        for name in self.services:
            tls = getattr(self, name).tls
            if bool(tls.cert) != bool(tls.key):
                raise ValueError(f"{name}.tls cert and key must be supplied together")
            if (
                name == "relay" or self.environment == "production"
            ) and tls.cert is None:
                raise ValueError(f"{name}.tls cert and key are required")
            if (
                not self.allow_legacy_development
                and getattr(self, name).host not in {"127.0.0.1", "::1"}
                and tls.cert is None
            ):
                raise ValueError(
                    f"{name}.tls is required for authenticated non-loopback listeners"
                )


@dataclass(frozen=True)
class CredentialSettings:
    token_env: str = "RD_TOKEN"

    def __post_init__(self):
        _env_name(self.token_env)


@dataclass(frozen=True)
class IceSettings:
    urls: tuple[str, ...] = ()
    username_env: str | None = None
    credential_env: str | None = None

    def __post_init__(self):
        if not self.urls or any(
            not value.startswith(("stun:", "stuns:", "turn:", "turns:")) or "@" in value
            for value in self.urls
        ):
            raise ValueError("ICE urls require stun/stuns/turn/turns scheme")
        for value in (self.username_env, self.credential_env):
            if value is not None:
                _env_name(value)
        if any(value.startswith(("turn:", "turns:")) for value in self.urls) and (
            not self.username_env or not self.credential_env
        ):
            raise ValueError("TURN requires username_env and credential_env")


@dataclass(frozen=True)
class ClientNetworkSettings:
    signaling_url: str = "wss://localhost:8765"
    signaling_ca_file: Path | None = None
    signaling_ping_interval_s: float = 20.0
    signaling_ping_timeout_s: float = 20.0
    signaling_send_timeout_s: float = 3.0
    signaling_close_timeout_s: float = 3.0
    signaling_max_message_bytes: int = 65536
    quic_host: str = "localhost"
    quic_port: int = 4433
    quic_server_name: str = "localhost"
    quic_ca_file: Path | None = None
    idle_timeout_s: float = 10.0
    reconnect_delay_s: float = 1.0
    register_timeout_s: float = 5.0
    ice_servers: tuple[IceSettings, ...] = ()

    def __post_init__(self):
        _port(self.quic_port)
        parsed = urlsplit(self.signaling_url)
        if (
            parsed.scheme not in {"ws", "wss"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(
                "signaling_url must be a WebSocket URL without credentials"
            )
        if not self.quic_host or not self.quic_server_name:
            raise ValueError("QUIC host and certificate server name are required")
        _positive(
            idle_timeout_s=self.idle_timeout_s,
            reconnect_delay_s=self.reconnect_delay_s,
            register_timeout_s=self.register_timeout_s,
            signaling_ping_interval_s=self.signaling_ping_interval_s,
            signaling_ping_timeout_s=self.signaling_ping_timeout_s,
            signaling_send_timeout_s=self.signaling_send_timeout_s,
            signaling_close_timeout_s=self.signaling_close_timeout_s,
            signaling_max_message_bytes=self.signaling_max_message_bytes,
        )
        if self.signaling_max_message_bytes > 1_048_576:
            raise ValueError("signaling_max_message_bytes must not exceed 1 MiB")
        if self.register_timeout_s > 60:
            raise ValueError("register_timeout_s must not exceed 60")


@dataclass(frozen=True)
class ControlSettings:
    send_hz: float = 50.0
    input_timeout_ms: int = 200
    command_timeout_ms: int = 300
    max_clock_skew_ms: int = 50

    def __post_init__(self):
        _positive(
            send_hz=self.send_hz,
            input_timeout_ms=self.input_timeout_ms,
            command_timeout_ms=self.command_timeout_ms,
        )
        if self.input_timeout_ms <= 1000 / self.send_hz:
            raise ValueError("input_timeout_ms must exceed one send period")
        if (
            self.input_timeout_ms >= self.command_timeout_ms
            or not 0 <= self.max_clock_skew_ms < self.command_timeout_ms
        ):
            raise ValueError(
                "input timeout and clock skew must be below command timeout"
            )


@dataclass(frozen=True)
class MotionSettings:
    max_linear_mps: float = 5.0
    max_angular_radps: float = 2.0

    def __post_init__(self):
        _positive(
            max_linear_mps=self.max_linear_mps, max_angular_radps=self.max_angular_radps
        )


@dataclass(frozen=True)
class CameraSettings:
    name: str = "front"
    source: str | int = "synthetic"
    width: int = 640
    height: int = 480
    fps: float = 30.0

    def __post_init__(self):
        if self.name not in {"front", "back", "left", "right"}:
            raise ValueError("camera name must be front, back, left or right")
        if (
            isinstance(self.source, int)
            and self.source < 0
            or isinstance(self.source, str)
            and not self.source
        ):
            raise ValueError("camera source must be nonempty or a nonnegative index")
        _positive(width=self.width, height=self.height, fps=self.fps)
        if self.fps > 240:
            raise ValueError("camera FPS must not exceed 240")
        if self.width % 2 or self.height % 2:
            raise ValueError("camera dimensions must be even")


@dataclass(frozen=True)
class SerialSettings:
    port: str = "/dev/ttyUSB0"
    baud_rate: int = 115200
    read_timeout_s: float = 0.02
    write_timeout_s: float = 0.02
    template_hex: str = "CD 0A 01 00 00 00 00 00 00 00 00 00 00"

    def __post_init__(self):
        _positive(
            baud_rate=self.baud_rate,
            read_timeout_s=self.read_timeout_s,
            write_timeout_s=self.write_timeout_s,
        )
        if not self.port:
            raise ValueError("serial port is required")
        try:
            template = bytes.fromhex(self.template_hex)
        except ValueError:
            raise ValueError("serial template_hex must encode 13 bytes") from None
        if len(template) != 13:
            raise ValueError("serial template_hex must encode 13 bytes")


@dataclass(frozen=True)
class ProcessSettings:
    heartbeat_interval_s: float = 0.1
    heartbeat_timeout_s: float = 1.0
    startup_timeout_s: float = 15.0
    shutdown_timeout_s: float = 5.0
    kill_timeout_s: float = 1.0

    def __post_init__(self):
        _positive(**vars(self))
        if not (
            self.heartbeat_interval_s
            < self.heartbeat_timeout_s
            <= self.startup_timeout_s
        ):
            raise ValueError(
                "process heartbeat interval < timeout <= startup timeout required"
            )


@dataclass(frozen=True)
class VideoSafetySettings:
    primary_view: Literal["front", "back", "left", "right"] = "front"
    stale_frame_ms: int = 500
    recovery_ms: int = 500

    def __post_init__(self):
        _positive(stale_frame_ms=self.stale_frame_ms, recovery_ms=self.recovery_ms)


@dataclass(frozen=True)
class VehicleSettings:
    environment: Environment = "development"
    session: int = 1234
    room: str = "1234"
    controller: Literal["none", "mock", "ros2", "serial"] = "none"
    components: tuple[Literal["control", "media"], ...] = ("control", "media")
    processes: ProcessSettings = field(default_factory=ProcessSettings)
    video: VideoSafetySettings = field(default_factory=VideoSafetySettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    credentials: CredentialSettings = field(default_factory=CredentialSettings)
    network: ClientNetworkSettings = field(default_factory=ClientNetworkSettings)
    control: ControlSettings = field(default_factory=ControlSettings)
    motion: MotionSettings = field(default_factory=MotionSettings)
    serial: SerialSettings = field(default_factory=SerialSettings)
    cameras: tuple[CameraSettings, ...] = field(
        default_factory=lambda: (CameraSettings(),)
    )

    def validate(self):
        validate_client(self)
        if not self.components or len(set(self.components)) != len(self.components):
            raise ValueError("components must be nonempty and unique")
        if self.components == ("control",) and self.controller == "none":
            raise ValueError("control-only mode requires an enabled controller")
        if not self.cameras or len({camera.name for camera in self.cameras}) != len(
            self.cameras
        ):
            raise ValueError("camera names must be nonempty and unique")
        if "media" in self.components and self.video.primary_view not in {
            camera.name for camera in self.cameras
        }:
            raise ValueError("primary video view must have a configured camera")


@dataclass(frozen=True)
class USBSettings:
    vendor_id: int = 0x2F24
    product_id: int = 0x0009

    def __post_init__(self):
        if not all(0 <= value <= 65535 for value in (self.vendor_id, self.product_id)):
            raise ValueError("USB identifiers must be uint16 values")


@dataclass(frozen=True)
class UISettings:
    theme: Literal["modern_glass", "dark_tech"] = "modern_glass"
    width: int = 1200
    height: int = 750
    frame_interval_ms: int = 16
    stats_interval_ms: int = 1000
    debug: bool = False

    def __post_init__(self):
        _positive(
            width=self.width,
            height=self.height,
            frame_interval_ms=self.frame_interval_ms,
            stats_interval_ms=self.stats_interval_ms,
        )


@dataclass(frozen=True)
class DrivingSettings:
    environment: Environment = "development"
    session: int = 1234
    room: str = "1234"
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    credentials: CredentialSettings = field(default_factory=CredentialSettings)
    network: ClientNetworkSettings = field(default_factory=ClientNetworkSettings)
    control: ControlSettings = field(default_factory=ControlSettings)
    usb: USBSettings = field(default_factory=USBSettings)
    ui: UISettings = field(default_factory=UISettings)
    video: VideoSafetySettings = field(default_factory=VideoSafetySettings)

    def validate(self):
        validate_client(self)


def validate_client(settings):
    if (
        not 0 <= settings.session <= 65535
        or not settings.room
        or len(settings.room) > 128
    ):
        raise ValueError("session must be uint16 and room must have 1..128 characters")
    if (
        settings.environment == "production"
        and not settings.network.signaling_url.startswith("wss://")
    ):
        raise ValueError("production signaling requires WSS")
