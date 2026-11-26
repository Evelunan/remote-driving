"""Exercise real local WSS/WebRTC/QUIC/Qt under bounded UDP impairments.

Synthetic cameras and a mock actuator only. Server restart cases create fresh
runtime/listener/registry instances on the same ports, not systemd processes.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import secrets
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import Future
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from network_shaper import Policy, Shaper
from rd_core.async_compat import ExceptionGroup, async_timeout

ROOT = Path(__file__).resolve().parents[1]
CASES = (
    "mild",
    "rpc-only",
    "control-blackhole",
    "media-blackhole",
    "window-budget",
    "signaling-restart",
    "relay-restart",
    "server-restart",
    "turn-relay",
    "turn-outage",
)


class RecoveryFailure(RuntimeError):
    def __init__(self, report):
        super().__init__("media/control recovery timed out")
        self.report = report


async def validate(args, cache, gate, interrupted):
    import aioice.ice
    from benchmark_media import network_delta, network_snapshot
    from rd_core.auth import AuthProvider
    from rd_core.samples import ControlInput
    from rd_core.settings import (
        AuthSettings,
        ClientNetworkSettings,
        CredentialSettings,
        DrivingSettings,
        PrincipalSettings,
        RelaySettings,
        SignalingSettings,
        VehicleSettings,
    )
    from rd_driving.app.control_client import DrivingControlClient
    from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
    from rd_quic.protocol import RpcProtocol
    from rd_server.relay.control_service import ControlRelayService
    from rd_server.runtime import ServerRuntime
    from rd_server.signaling.server import SignalingServer
    from rd_vehicle.application.control_client import VehicleControlClient
    from rd_vehicle.application.multi_camera_manager import MultiCameraManager
    from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
    from rd_vehicle.infrastructure.controller.mock_controller import MockController
    from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer
    from simulate_control import temporary_tls

    credentials = {
        name: secrets.token_urlsafe(32) for name in ("RD_NET_VEHICLE", "RD_NET_DRIVER")
    }
    previous = {name: os.environ.get(name) for name in credentials}
    os.environ.update(credentials)
    views = ("front", "back", "left", "right")[: args.views]
    tasks, runtimes, statuses, executions = [], [], [], []
    publisher = receiver = manager = vehicle = None
    shaper = Shaper(seed=args.seed)
    throttle = 0.0
    report = {"case": args.case, "physical_stop_verified": False}
    controller = None
    turn = None
    original_datagram = RpcProtocol.send_datagram
    datagrams = {"drop": False, "dropped": 0}

    def application_datagram(protocol, data):
        if datagrams["drop"]:
            datagrams["dropped"] += 1
            return
        return original_datagram(protocol, data)

    class Actuator(MockController):
        def __init__(self, motion):
            super().__init__(motion)
            self.events = []

        def send_command(self, throttle, brake, steering):
            super().send_command(throttle, brake, steering)
            if len(self.events) >= 10000:
                raise RuntimeError("network validation actuator observation limit")
            self.events.append((time.monotonic(), self.commands[-1]))

    async def until(predicate, timeout=15):
        async with async_timeout(timeout):
            while True:
                if interrupted.is_set():
                    raise RuntimeError("network validation interrupted")
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("network validation component exited")
                if shaper.errors:
                    raise RuntimeError(f"UDP injection errors: {shaper.errors}")
                if predicate():
                    return
                await asyncio.sleep(0.01)

    async def duration(seconds, invariant=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if invariant is not None and not invariant():
                raise RuntimeError("network validation invariant failed")
            await until(lambda: True)
            await asyncio.sleep(0.01)

    with tempfile.TemporaryDirectory(prefix="rd-network-") as temporary:
        try:
            if args.case.startswith("turn-"):
                from local_turn import LocalTurn

                turn = LocalTurn(temporary, args.turnserver, args.turn_library_dir)
                await turn.start()
            tls = temporary_tls(Path(temporary))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(tls.cert, tls.key)
            auth = AuthProvider(
                AuthSettings(
                    (
                        PrincipalSettings(
                            "net-vehicle",
                            "vehicle",
                            "RD_NET_VEHICLE",
                            (42,),
                            ("network",),
                        ),
                        PrincipalSettings(
                            "net-driver",
                            "driving",
                            "RD_NET_DRIVER",
                            (42,),
                            ("network",),
                        ),
                    )
                ),
                environ=credentials,
            )
            signaling_settings = SignalingSettings(port=0)
            relay_settings = RelaySettings(port=0, tls=tls)
            signaling = SignalingServer(
                signaling_settings, tls_context=context, auth=auth
            )
            relay = ControlRelayService(relay_settings, auth)
            separate = args.case in {"signaling-restart", "relay-restart"}
            active = (
                {
                    "signaling": ServerRuntime({"signaling": signaling}),
                    "relay": ServerRuntime({"relay": relay}),
                }
                if separate
                else {"both": ServerRuntime({"signaling": signaling, "relay": relay})}
            )
            with (
                shaper.install(),
                patch.object(RpcProtocol, "send_datagram", application_datagram),
                patch.object(
                    aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
                ),
                turn.force_relay(consent_interval=args.diagnostic_consent_interval)
                if turn is not None
                else nullcontext(),
            ):
                try:
                    for runtime in active.values():
                        runtimes.append(runtime)
                        await runtime.start()
                    signaling_settings = replace(
                        signaling_settings,
                        port=signaling._server.sockets[0].getsockname()[1],
                    )
                    relay_settings = replace(
                        relay_settings,
                        port=relay.listener._transport.get_extra_info("sockname")[1],
                    )
                    network = ClientNetworkSettings(
                        signaling_url=f"wss://127.0.0.1:{signaling_settings.port}",
                        signaling_ca_file=tls.cert,
                        quic_host="127.0.0.1",
                        quic_port=relay_settings.port,
                        quic_server_name="localhost",
                        quic_ca_file=tls.cert,
                        reconnect_delay_s=0.1,
                        ice_servers=(turn.ice_settings,) if turn is not None else (),
                    )
                    vehicle_settings = VehicleSettings(
                        controller="mock",
                        session=42,
                        room="network",
                        network=network,
                        credentials=CredentialSettings("RD_NET_VEHICLE"),
                    )
                    controller = Actuator(vehicle_settings.motion)
                    vehicle = VehicleControlClient(vehicle_settings, controller)
                    publisher = WebRTCStreamer(
                        "network", network, vehicle_settings.credentials, session=42
                    )
                    manager = MultiCameraManager(publisher)
                    for view in views:
                        manager.add_camera(TestCamera(320, 240, 30, view))
                    receiver = WebRTCReceiver(
                        "network",
                        network,
                        CredentialSettings("RD_NET_DRIVER"),
                        views=views,
                    )
                    receiver.on_frame(
                        lambda name, frame, source, captured_at: cache.put(
                            name, frame, source=source, captured_at=captured_at
                        ),
                        include_capture_time=True,
                    )

                    def invalidate(source):
                        cache.invalidate_source(source, generation=cache._generation)
                        gate.invalidate("publisher_left", source=source)

                    receiver.on_invalidate(invalidate)
                    inputs = asyncio.Queue(maxsize=1)
                    driver = DrivingControlClient(
                        DrivingSettings(
                            session=42,
                            room="network",
                            network=network,
                            credentials=CredentialSettings("RD_NET_DRIVER"),
                        ),
                        inputs,
                        video_gate=gate,
                        on_execution=lambda sample, result, ms: executions.append(
                            (time.monotonic(), ms)
                        ),
                        on_status=lambda state, detail: statuses.append(
                            (time.monotonic(), state, detail)
                        ),
                    )

                    async def samples():
                        while True:
                            if inputs.full():
                                inputs.get_nowait()
                            inputs.put_nowait(
                                ControlInput(
                                    throttle,
                                    0,
                                    0,
                                    time.monotonic(),
                                    time.time_ns() // 1_000_000,
                                )
                            )
                            await asyncio.sleep(0.01)

                    def picture(since):
                        return publisher.my_id is not None and cache.ready(
                            views, since, publisher.my_id
                        )

                    await manager.start()
                    started = time.monotonic()
                    tasks = [
                        asyncio.create_task(job)
                        for job in (
                            manager.wait(),
                            publisher.start(),
                            receiver.start(),
                            vehicle.run(),
                            driver.run(),
                            samples(),
                        )
                    ]
                    await until(lambda: vehicle.connection is not None)
                    await until(driver.request_start_control)
                    await until(lambda: driver.armed and picture(started))
                    report["initial_paths"] = (
                        await network_snapshot(publisher, receiver)
                    )["paths"]
                    if not report["initial_paths"]:
                        raise RuntimeError("no nominated media path")
                    if turn is not None:
                        turn.assert_paths(report["initial_paths"])
                    throttle = 0.25
                    await until(lambda: controller.commands[-1][0] > 0)
                    old_epoch, old_publisher = driver.epoch, publisher.my_id
                    driver_connection, vehicle_connection = driver.connection, vehicle.connection
                    before_counts = cache.counts()
                    before_network = await network_snapshot(publisher, receiver)
                    report["udp_before_fault"] = shaper.snapshot()
                    fault_event_index = len(controller.events)
                    started_fault = time.monotonic()
                    if args.case == "turn-relay":
                        await duration(
                            args.seconds,
                            lambda: driver.armed and vehicle.epoch == old_epoch,
                        )
                        if not picture(started_fault):
                            raise RuntimeError("a TURN view stopped presenting")
                        report["control_retained"] = True
                        report["sample_to_execution_ack_ms"] = [
                            ms for stamp, ms in executions if stamp >= started_fault
                        ]
                    elif args.case == "mild":
                        shaper.set(
                            "media",
                            Policy(
                                delay_ms=5, jitter_ms=3, loss=0.01, rate_bps=3_000_000
                            ),
                        )
                        shaper.set(
                            "control",
                            Policy(
                                delay_ms=2, jitter_ms=1, loss=0.01, rate_bps=256_000
                            ),
                        )
                        await duration(
                            args.seconds,
                            lambda: driver.armed and vehicle.epoch == old_epoch,
                        )
                        if not picture(started_fault):
                            raise RuntimeError(
                                "a view stopped presenting under mild impairment"
                            )
                        report["control_retained"] = True
                        report["sample_to_execution_ack_ms"] = [
                            ms for stamp, ms in executions if stamp >= started_fault
                        ]
                    else:
                        if args.case == "turn-outage":
                            await turn.stop(crash=True)
                        elif args.case == "rpc-only":
                            datagrams["drop"] = True
                        elif args.case in {"control-blackhole", "media-blackhole"}:
                            shaper.set(
                                args.case.split("-", 1)[0], Policy(blackhole=True)
                            )
                        elif args.case == "window-budget":
                            # Four UDP legs per window/command round trip.
                            delay_ms = relay_settings.max_control_age_ms // 4 + 20
                            shaper.set("control", Policy(delay_ms=delay_ms))
                            report["window_round_trip_floor_ms"] = delay_ms * 4
                        else:
                            key = (
                                "both"
                                if args.case == "server-restart"
                                else args.case.split("-", 1)[0]
                            )
                            await active[key].stop()
                        await until(
                            lambda: (
                                not driver.armed
                                and vehicle.epoch is None
                                and controller.commands[-1] == (0, 0)
                            ),
                            timeout=5,
                        )
                        stopped = time.monotonic()
                        first_zero_index = next(
                            i
                            for i in range(fault_event_index, len(controller.events))
                            if controller.events[i][1] == (0, 0)
                        )
                        first_zero = controller.events[first_zero_index][0]
                        report["fault_to_mock_zero_ms"] = (
                            first_zero - started_fault
                        ) * 1000
                        report["fault_to_revoke_observed_ms"] = (
                            stopped - started_fault
                        ) * 1000
                        await duration(
                            0.6,
                            lambda: (
                                not driver.armed and controller.commands[-1] == (0, 0)
                            ),
                        )
                        if args.case == "rpc-only":
                            if vehicle.connection is None:
                                raise RuntimeError(
                                    "RPC-only scenario lost its vehicle transport"
                                )
                            clock = await vehicle.connection.protocol.request(
                                "clock", {}
                            )
                            report["reliable_rpc_survived_datagram_loss"] = (
                                type(clock.get("server_time_ms")) is int
                            )
                            report["suppressed_application_datagrams"] = datagrams[
                                "dropped"
                            ]
                            if (
                                not report["reliable_rpc_survived_datagram_loss"]
                                or not datagrams["dropped"]
                            ):
                                raise RuntimeError(
                                    "RPC-only scenario did not exercise both paths"
                                )
                        if args.case == "media-blackhole":
                            report["control_transport_survived_video_loss"] = (
                                driver.connection is driver_connection
                                and vehicle.connection is vehicle_connection
                            )
                            if not report["control_transport_survived_video_loss"]:
                                raise RuntimeError(
                                    "media-only blackout lost the independent control transport"
                                )
                        report["fault_udp"] = shaper.snapshot()
                        recovery = time.monotonic()
                        datagrams["drop"] = False
                        shaper.set("media", Policy(), discard_pending=False)
                        shaper.set("control", Policy(), discard_pending=False)
                        if args.case == "turn-outage":
                            await turn.start()
                        if args.case.endswith("restart"):
                            services = {}
                            if args.case != "relay-restart":
                                signaling = SignalingServer(
                                    signaling_settings, tls_context=context, auth=auth
                                )
                                services["signaling"] = signaling
                            if args.case != "signaling-restart":
                                relay = ControlRelayService(relay_settings, auth)
                                services["relay"] = relay
                            replacement = ServerRuntime(services)
                            runtimes.append(replacement)
                            await replacement.start()
                        try:
                            await until(
                                lambda: (
                                    picture(recovery)
                                    and gate.snapshot().ready
                                    and driver.connection is not None
                                ),
                                timeout=45 if args.case == "turn-outage" else 15,
                            )
                        except TimeoutError:
                            report["failure"] = {
                                "phase": "media_recovery",
                                "gate_reason": gate.snapshot().reason,
                                "primary_capture_age_ms": (
                                    round((time.monotonic() - capture_time) * 1000)
                                    if (capture_time := cache.get("front").captured_at)
                                    is not None
                                    else None
                                ),
                                "elapsed_ms": (time.monotonic() - recovery) * 1000,
                                "driver_armed": driver.armed,
                                "mock_target": list(controller.commands[-1]),
                                "publisher_peers": len(publisher.peers),
                                "receiver_peers": len(receiver.peers),
                                "signaling_still_joined": publisher.my_id is not None
                                and receiver.my_id is not None,
                                "frame_counts": cache.counts(),
                            }
                            raise RecoveryFailure(report) from None
                        report["recovery_to_all_views_ms"] = (
                            time.monotonic() - recovery
                        ) * 1000
                        if (
                            args.case in {"signaling-restart", "server-restart"}
                            and publisher.my_id == old_publisher
                        ):
                            raise RuntimeError(
                                "server restart reused publisher identity"
                            )
                        held_event_index = len(controller.events)
                        await duration(
                            0.75,
                            lambda: (
                                not driver.armed and controller.commands[-1] == (0, 0)
                            ),
                        )
                        if any(
                            v or w for _, (v, w) in controller.events[held_event_index:]
                        ):
                            raise RuntimeError(
                                "held input restored motion before neutral"
                            )
                        report["held_input_did_not_rearm"] = True
                        throttle = 0.0
                        await until(lambda: vehicle.connection is not None)
                        await until(driver.request_start_control)
                        await until(lambda: driver.armed and driver.epoch != old_epoch)
                        report["recovery_to_new_epoch_ms"] = (
                            time.monotonic() - recovery
                        ) * 1000
                        report["new_epoch_after_fresh_neutral"] = True
                        if any(
                            v or w
                            for _, (v, w) in controller.events[first_zero_index + 1 :]
                        ):
                            raise RuntimeError(
                                "motion resumed between failsafe and fresh-neutral recovery"
                            )
                        report["zero_target_until_neutral_recovery"] = True
                        throttle = 0.25
                        await until(lambda: controller.commands[-1][0] > 0)
                        await duration(args.seconds, lambda: driver.armed)
                    report["frame_counts_before"] = before_counts
                    report["frame_counts_after"] = cache.counts()
                    after_network = await network_snapshot(publisher, receiver)
                    report["final_paths"] = after_network["paths"]
                    if turn is not None:
                        turn.assert_paths(report["final_paths"])
                    report["media_network"] = network_delta(
                        before_network, after_network
                    )
                    report["udp_after_scenario"] = shaper.snapshot()
                    report["settings"] = {
                        "control": asdict(vehicle_settings.control),
                        "video": asdict(vehicle_settings.video),
                        "relay_clock_skew_ms": relay_settings.max_clock_skew_ms,
                    }
                    report["statuses"] = statuses
                finally:
                    # Restore the path before the protocols' bounded shutdown;
                    # all injection handles are discarded and then reclaimed.
                    datagrams["drop"] = False
                    shaper.set("control", Policy())
                    shaper.set("media", Policy())
                    for task in tasks:
                        task.cancel()
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    failures = [
                        value for value in results if isinstance(value, Exception)
                    ]
                    for resource, method in (
                        (manager, "stop"),
                        (publisher, "close"),
                        (receiver, "close"),
                    ):
                        if resource is not None:
                            try:
                                await getattr(resource, method)()
                            except Exception as error:  # noqa: BLE001 - collect cleanup failures, then raise them
                                failures.append(error)
                    for runtime in reversed(runtimes):
                        try:
                            await runtime.stop()
                        except Exception as error:  # noqa: BLE001 - keep cleaning other owned runtimes
                            failures.append(error)
                    # Check natural ownership cleanup before the harness fallback.
                    report["udp_cleanup"] = shaper.snapshot()
                    shaper.close()
                    if failures:
                        raise ExceptionGroup(
                            "network validation cleanup failed", failures
                        )
        finally:
            if turn is not None:
                await turn.close()
                report["turn_fixture"] = turn.report()
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    report["camera_threads_closed"] = all(
        camera._thread is None for camera in manager.cameras
    )
    report["device_threads_closed"] = not any(
        thread.is_alive() for thread in vehicle.supervisor._threads
    )
    report["actuator_closed"] = controller.closed
    report["media_tasks_closed"] = (
        not receiver._peer_tasks and not receiver.peers and not publisher.peers
    )
    if not all(
        report[name]
        for name in (
            "camera_threads_closed",
            "device_threads_closed",
            "actuator_closed",
            "media_tasks_closed",
        )
    ):
        raise RuntimeError("network validation component resource leak")
    if (
        any(
            not item["closed"] or item["pending_packets"] or item["queued_bytes"]
            for item in report["udp_cleanup"]["endpoints"]
        )
        or shaper.errors
    ):
        raise RuntimeError("network validation UDP endpoint leak/error")
    return report


def main():
    # This in-process harness runs Qt, real UDP sockets and packet shaping together.
    # Use the selector loop for the Windows UDP worker during the simulation.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--views", type=int, choices=(1, 4), default=4)
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--turnserver",
        type=Path,
        default=os.environ.get("RD_TEST_TURNSERVER") or shutil.which("turnserver"),
    )
    parser.add_argument(
        "--turn-library-dir",
        type=Path,
        default=os.environ.get("RD_TEST_TURN_LIBRARY_DIR"),
    )
    parser.add_argument(
        "--diagnostic-consent-interval",
        type=float,
        help="test-only ICE consent polling interval; omitted uses upstream defaults",
    )
    args = parser.parse_args()
    if not 0.25 <= args.seconds <= 30:
        parser.error("seconds must be 0.25..30")
    if args.case == "mild" and args.seconds < 2:
        parser.error("mild impairment needs at least 2 seconds to observe RTP recovery")
    if args.case.startswith("turn-") and args.turnserver is None:
        parser.error("TURN cases require --turnserver or RD_TEST_TURNSERVER")
    if args.diagnostic_consent_interval is not None and (
        not args.case.startswith("turn-")
        or not 0.1 <= args.diagnostic_consent_interval <= 5
    ):
        parser.error(
            "diagnostic consent interval requires a TURN case and must be 0.1..5 seconds"
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"passed": false, "status": "started"}\n')
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QApplication
    from rd_core.video_safety import VideoGate
    from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
    from soak_media import Observations

    app = QApplication([])
    cache, gate = Observations(), VideoGate()
    widget = MultiVideoWidget(cache, video_gate=gate)
    cache.observe(widget)
    widget.resize(1280, 720)
    widget.show()
    stop, outcome = threading.Event(), Future()
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)

    def tick():
        widget.check_expiry()
        if outcome.done():
            app.quit()

    timer.timeout.connect(tick)
    timer.start(16)

    def backend():
        try:

            async def bounded():
                async with async_timeout(
                    args.seconds + (100 if args.case.startswith("turn-") else 60)
                ):
                    return await validate(args, cache, gate, stop)

            outcome.set_result(asyncio.run(bounded()))
        except BaseException as error:  # noqa: BLE001 - forward thread failure to the main thread
            outcome.set_exception(error)

    worker = threading.Thread(target=backend, name="network-validation-backend")
    worker.start()
    try:
        app.exec()
    finally:
        stop.set()
        timer.stop()
        widget.close()
        worker.join(20)
    if worker.is_alive():
        raise RuntimeError("network validation worker did not terminate")
    try:
        report = outcome.result()
    except RecoveryFailure as error:
        report = error.report
    sources = [
        *ROOT.glob("apps/*/src/**/*.py"),
        *ROOT.glob("packages/*/src/**/*.py"),
        *ROOT.glob("scripts/*.py"),
    ]
    report.update(
        schema="rd-network-validation/1",
        passed=not cache.errors and "failure" not in report,
        scope="single-host real WSS/WebRTC/QUIC and Qt offscreen; UDP send-side impairment before real loopback sockets; synthetic cameras, mock actuator; TURN only in explicit turn cases; no physical-device certification",
        injection_scope="per UDP endpoint send queue; TCP/WSS is not shaped; seeded packet randomness does not make adaptive codec traffic deterministic",
        python=platform.python_version(),
        platform=platform.platform(),
        versions={
            name: importlib.metadata.version(name)
            for name in ("aiortc", "aioice", "aioquic", "av", "PyQt6")
        },
        workload={
            "views": args.views,
            "camera_size": [320, 240],
            "fps": 30,
            "seconds": args.seconds,
            "seed": args.seed,
            "diagnostic_consent_interval": args.diagnostic_consent_interval,
        },
        git_commit=subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        git_dirty=bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ),
        source_sha256={
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(sources)
        },
        presentation_errors=dict(cache.errors),
    )
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {"passed": report["passed"], "case": args.case, "output": str(args.output)}
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
