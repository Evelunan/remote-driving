"""Reproduce v2 registration, target execution, input expiry and neutral recovery.

Only binds loopback, generates temporary credentials/TLS and uses MockController.
No USB, ROS, serial adapter, camera, GUI or external endpoint is opened.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import platform
import secrets
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from rd_core.auth import AuthProvider
from rd_core.samples import ControlInput
from rd_core.settings import (
    AuthSettings,
    ClientNetworkSettings,
    CredentialSettings,
    DrivingSettings,
    MotionSettings,
    PrincipalSettings,
    RelaySettings,
    TLSSettings,
    VehicleSettings,
)
from rd_driving.app.control_client import DrivingControlClient
from rd_server.relay.control_service import ControlRelayService
from rd_server.runtime import ServerRuntime
from rd_vehicle.application.control_client import VehicleControlClient
from rd_vehicle.infrastructure.controller.mock_controller import MockController


def temporary_tls(directory):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "cert.pem", directory / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return TLSSettings(cert_path, key_path)


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.005)


async def simulate(directory, motion_seconds):
    credentials = {
        "RD_SIM_VEHICLE_TOKEN": secrets.token_urlsafe(32),
        "RD_SIM_DRIVER_TOKEN": secrets.token_urlsafe(32),
    }
    previous = {name: os.environ.get(name) for name in credentials}
    os.environ.update(credentials)
    tls = temporary_tls(directory)
    auth = AuthProvider(
        AuthSettings(
            (
                PrincipalSettings(
                    "sim-vehicle", "vehicle", "RD_SIM_VEHICLE_TOKEN", (42,), ()
                ),
                PrincipalSettings(
                    "sim-driver", "driving", "RD_SIM_DRIVER_TOKEN", (42,), ()
                ),
            )
        ),
        environ=credentials,
    )
    relay = ControlRelayService(RelaySettings(port=0, tls=tls), auth)
    runtime = ServerRuntime({"relay": relay})
    await runtime.start()
    network = ClientNetworkSettings(
        quic_host="127.0.0.1",
        quic_port=relay.listener._transport.get_extra_info("sockname")[1],
        quic_server_name="localhost",
        quic_ca_file=tls.cert,
        reconnect_delay_s=0.1,
    )
    vehicle_settings = VehicleSettings(
        session=42,
        controller="mock",
        network=network,
        credentials=CredentialSettings("RD_SIM_VEHICLE_TOKEN"),
        motion=MotionSettings(1.2, 0.4),
    )
    driving_settings = DrivingSettings(
        session=42,
        network=network,
        credentials=CredentialSettings("RD_SIM_DRIVER_TOKEN"),
    )
    controller = MockController(vehicle_settings.motion)
    vehicle = VehicleControlClient(vehicle_settings, controller)
    samples = asyncio.Queue(maxsize=1)
    elapsed = []
    driver = DrivingControlClient(
        driving_settings,
        samples,
        on_execution=lambda sample, result, ms: elapsed.append(ms),
    )
    current = {"enabled": True, "throttle": 0.0, "steering": 0.0}

    async def input_source():
        while True:
            if current["enabled"]:
                sample = ControlInput(
                    current["throttle"],
                    0,
                    current["steering"],
                    time.monotonic(),
                    time.time_ns() // 1_000_000,
                )
                if samples.full():
                    samples.get_nowait()
                samples.put_nowait(sample)
            await asyncio.sleep(0.01)

    tasks = [
        asyncio.create_task(vehicle.run()),
        asyncio.create_task(input_source()),
        asyncio.create_task(driver.run()),
    ]
    try:
        await wait_for(lambda: driver.armed)
        first_epoch = driver.epoch
        current.update(throttle=0.5, steering=0.25)
        await wait_for(lambda: (0.6, 0.1) in controller.commands)
        await asyncio.sleep(motion_seconds)
        current["enabled"] = False
        lost_at = time.monotonic()
        await wait_for(
            lambda: vehicle.epoch is None and controller.commands[-1] == (0, 0)
        )
        stop_delay_ms = (time.monotonic() - lost_at) * 1000
        current.update(enabled=True, throttle=0.0, steering=0.0)
        await wait_for(lambda: driver.armed and driver.epoch != first_epoch)
        report = {
            "scope": "loopback TLS QUIC and in-memory actuator; no hardware",
            "python": platform.python_version(),
            "platform": platform.platform(),
            "motion_seconds": motion_seconds,
            "settings": {
                "send_hz": driving_settings.control.send_hz,
                "input_timeout_ms": driving_settings.control.input_timeout_ms,
                "command_timeout_ms": vehicle_settings.control.command_timeout_ms,
                "clock_skew_ms": vehicle_settings.control.max_clock_skew_ms,
                "max_linear_mps": vehicle_settings.motion.max_linear_mps,
                "max_angular_radps": vehicle_settings.motion.max_angular_radps,
            },
            "execution_acks": len(elapsed),
            "capture_to_ack_ms_median": statistics.median(elapsed),
            "capture_to_ack_ms_p95": sorted(elapsed)[int((len(elapsed) - 1) * 0.95)],
            "input_expiry_to_mock_zero_target_ms": stop_delay_ms,
            "new_epoch_after_neutral_recovery": True,
            "physical_stop_verified": False,
        }
    finally:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await runtime.stop()
        if not all(isinstance(result, asyncio.CancelledError) for result in results):
            raise RuntimeError("a simulation task failed")
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    report["device_threads_closed"] = not any(
        thread.is_alive() for thread in vehicle.supervisor._threads
    )
    if not report["device_threads_closed"]:
        raise RuntimeError("actuator threads did not close")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--motion-seconds", type=float, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0.1 <= args.motion_seconds <= 60:
        parser.error("--motion-seconds must be between 0.1 and 60")
    with tempfile.TemporaryDirectory(prefix="rd-control-sim-") as directory:
        report = asyncio.run(simulate(Path(directory), args.motion_seconds))
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
