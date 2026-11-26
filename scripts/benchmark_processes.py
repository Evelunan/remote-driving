"""Compare combined and production split vehicle hosts on one Linux machine.

The driver/Qt/server harness is identical in both modes. Vehicle children run
the real component entry with synthetic cameras and a mock actuator. No frames
or measurements travel on the production supervisor's heartbeat channels.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import multiprocessing
import os
import platform
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from process_probe import merge_trace, process_resources, process_tree, vehicle_host

from rd_core.async_compat import ExceptionGroup, async_timeout

ROOT = Path(__file__).resolve().parents[1]


async def run_case(args, probe, cache, gate, stop):
    from benchmark_media import network_delta, network_snapshot
    from media_probe import VIEWS, distribution
    from rd_core.auth import AuthProvider
    from rd_core.samples import ControlInput
    from rd_core.settings import (
        AuthSettings,
        CameraSettings,
        ClientNetworkSettings,
        CredentialSettings,
        DrivingSettings,
        LoggingSettings,
        PrincipalSettings,
        RelaySettings,
        SignalingSettings,
        VehicleSettings,
    )
    from rd_driving.app.control_client import DrivingControlClient
    from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
    from rd_server.relay.control_service import ControlRelayService
    from rd_server.runtime import ServerRuntime
    from rd_server.signaling.server import SignalingServer
    from simulate_control import temporary_tls
    from types import SimpleNamespace

    views = VIEWS[: args.views]
    sample_times, observations, phases, tasks = {}, [], {}, []
    report = {"topology": args.topology}
    throttle = 0.0
    context = multiprocessing.get_context("spawn")
    process = parent = receiver = runtime = None
    monitoring = None
    credentials = {
        name: secrets.token_urlsafe(32)
        for name in ("RD_PROC_VEHICLE", "RD_PROC_DRIVER")
    }
    previous = {name: os.environ.get(name) for name in credentials}
    os.environ.update(credentials)

    def snapshot():
        return {
            "timestamp": time.monotonic(),
            "processes": {
                str(pid): process_resources(pid)
                for pid in sorted({os.getpid()} | process_tree(process.pid))
            },
        }

    async def until(predicate, timeout=20):
        async with async_timeout(timeout):
            while not predicate():
                if stop.is_set():
                    raise RuntimeError("process benchmark interrupted")
                if process is not None and process.exitcode is not None:
                    raise RuntimeError(
                        f"vehicle host exited early ({process.exitcode})"
                    )
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("benchmark component exited early")
                await asyncio.sleep(0.01)

    def publisher_id():
        ids = [
            key
            for key, peer in signaling.manager.peers.items()
            if peer.principal is not None and peer.principal.role == "vehicle"
        ]
        if len(ids) > 1:
            raise RuntimeError("unexpected duplicate publisher")
        return ids[0] if ids else None

    def painted(since, peer_id):
        with probe.lock:
            return peer_id is not None and set(views) <= {
                name
                for (name, _), stages in probe.frames.items()
                if stages.get("paint", 0) >= since
                and stages.get("source_peer_id") == peer_id
            }

    with tempfile.TemporaryDirectory(prefix="rd-process-benchmark-") as directory:
        path = Path(directory)
        try:
            tls = temporary_tls(path)
            auth = AuthProvider(
                AuthSettings(
                    (
                        PrincipalSettings(
                            "process-vehicle",
                            "vehicle",
                            "RD_PROC_VEHICLE",
                            (42,),
                            ("process-benchmark",),
                        ),
                        PrincipalSettings(
                            "process-driver",
                            "driving",
                            "RD_PROC_DRIVER",
                            (42,),
                            ("process-benchmark",),
                        ),
                    )
                ),
                environ=credentials,
            )
            tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls_context.load_cert_chain(tls.cert, tls.key)
            signaling = SignalingServer(
                SignalingSettings(port=0), tls_context=tls_context, auth=auth
            )
            relay = ControlRelayService(RelaySettings(port=0, tls=tls), auth)
            runtime = ServerRuntime({"signaling": signaling, "relay": relay})
            await runtime.start()
            network = ClientNetworkSettings(
                signaling_url=f"wss://127.0.0.1:{signaling._server.sockets[0].getsockname()[1]}",
                signaling_ca_file=tls.cert,
                quic_host="127.0.0.1",
                quic_port=relay.listener._transport.get_extra_info("sockname")[1],
                quic_server_name="localhost",
                quic_ca_file=tls.cert,
                reconnect_delay_s=0.1,
            )
            cameras = []
            for view in views:
                width, height = (
                    (320, 240)
                    if args.profile == "small"
                    else (1080, 720)
                    if view == "front"
                    else (640, 480)
                )
                cameras.append(CameraSettings(view, "synthetic", width, height, 30))
            vehicle = VehicleSettings(
                controller="mock",
                session=42,
                room="process-benchmark",
                network=network,
                credentials=CredentialSettings("RD_PROC_VEHICLE"),
                cameras=tuple(cameras),
                logging=LoggingSettings("WARNING", path),
            )
            receiver = WebRTCReceiver(
                "process-benchmark",
                network,
                CredentialSettings("RD_PROC_DRIVER"),
                views=views,
            )
            receiver.on_frame(
                lambda name, pixels, source, captured_at: cache.put(
                    name, pixels, source=source, captured_at=captured_at
                ),
                include_capture_time=True,
            )

            def invalidated(source):
                cache.invalidate_source(source, generation=cache._generation)
                gate.invalidate("publisher_left", source=source)

            receiver.on_invalidate(invalidated)
            samples = asyncio.Queue(maxsize=1)
            driver = DrivingControlClient(
                DrivingSettings(
                    session=42,
                    room="process-benchmark",
                    network=network,
                    credentials=CredentialSettings("RD_PROC_DRIVER"),
                ),
                samples,
                video_gate=gate,
                on_execution=lambda sample, result, elapsed: probe.sample(
                    "control_sample_to_ack_ms", elapsed
                ),
            )

            async def inputs():
                while True:
                    now, wall = time.monotonic(), time.time_ns() // 1_000_000
                    if wall in sample_times or len(sample_times) >= 20000:
                        raise RuntimeError(
                            "ambiguous input timestamp or observation limit"
                        )
                    sample_times[wall] = now
                    if samples.full():
                        samples.get_nowait()
                    samples.put_nowait(ControlInput(throttle, 0, 0, now, wall))
                    await asyncio.sleep(0.01)

            async def monitor():
                next_resources = 0
                while True:
                    before = time.monotonic()
                    await asyncio.sleep(0.01)
                    now = time.monotonic()
                    probe.sample(
                        "harness_loop_lag_ms", max(0, now - before - 0.01) * 1000
                    )
                    probe.sample("control_armed", int(driver.armed))
                    if now >= next_resources:
                        # Sampling starts after both components are ready. A
                        # vanished process during a phase must fail the run.
                        observations.append(snapshot())
                        next_resources = now + 0.2

            parent, child = socket.socketpair()
            process = context.Process(
                target=vehicle_host,
                args=(vehicle, args.topology, child),
                name="benchmark-vehicle-host",
            )
            started = time.monotonic()
            try:
                process.start()
            finally:
                child.close()
            tasks = [
                asyncio.create_task(job)
                for job in (receiver.start(), driver.run(), inputs())
            ]
            await until(lambda: vehicle.session in relay.registry.vehicles)
            await until(driver.request_start_control)
            await until(lambda: painted(started, publisher_id()) and driver.armed)
            report["startup_ready_ms"] = (time.monotonic() - started) * 1000
            monitoring = asyncio.create_task(monitor())
            tasks.append(monitoring)
            throttle = 0.25

            async def phase(name):
                end_warmup = time.monotonic() + args.warmup
                await until(lambda: time.monotonic() >= end_warmup)
                before_network = await network_snapshot(
                    SimpleNamespace(peers={}), receiver
                )
                before = snapshot()
                await until(
                    lambda: time.monotonic() >= before["timestamp"] + args.seconds,
                    args.seconds + 5,
                )
                after = snapshot()
                after_network = await network_snapshot(
                    SimpleNamespace(peers={}), receiver
                )
                phases[name] = (before, after)
                report.setdefault("network", {})[name] = network_delta(
                    before_network, after_network
                )

            await phase("steady")
            throttle = 0.0
            old_peer, old_epoch = publisher_id(), driver.epoch
            reconnect = time.monotonic()
            await signaling.manager.peers[old_peer].websocket.close(
                1012, "process benchmark reconnect"
            )
            await until(
                lambda: (
                    publisher_id() != old_peer and painted(reconnect, publisher_id())
                )
            )
            report["reconnect_all_views_painted_ms"] = (
                time.monotonic() - reconnect
            ) * 1000
            await until(driver.request_start_control)
            await until(lambda: driver.armed and driver.epoch != old_epoch)
            report["reconnect_new_control_epoch_ms"] = (
                time.monotonic() - reconnect
            ) * 1000
            throttle = 0.25
            await phase("recovered")
        finally:
            failures = []
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures.extend(value for value in results if isinstance(value, Exception))
            shutdown = time.monotonic()
            forced = False
            descendants = {}
            if process is not None and process.pid is not None and process.is_alive():
                try:
                    for pid in process_tree(process.pid):
                        descendants[pid] = process_resources(pid)["start_ticks"]
                except FileNotFoundError:
                    pass  # A component may already be exiting after a failure.
            if parent is not None:
                parent.close()  # EOF to the harness host, which stops its own children.
            if process is not None and process.pid is not None:
                # Wait past the production supervisor's bounded shutdown. On
                # error, kill only known owned descendants, never by name.
                deadline = shutdown + 12
                while process.is_alive() and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                if process.is_alive():
                    forced = True
                    for pid, identity in descendants.items():
                        try:
                            if process_resources(pid)["start_ticks"] == identity:
                                os.kill(pid, 9)
                        except (ProcessLookupError, FileNotFoundError):
                            pass
                    if process.is_alive():
                        process.kill()
                process.join(timeout=2)
                report["host_exitcode"] = process.exitcode
                if process.exitcode != 0 or forced:
                    failures.append(RuntimeError("vehicle host cleanup failed"))
                process.close()
            report["shutdown_ms"] = (time.monotonic() - shutdown) * 1000
            report["forced_cleanup"] = forced
            for resource, method in ((receiver, "close"), (runtime, "stop")):
                if resource is not None:
                    try:
                        await getattr(resource, method)()
                    except Exception as error:
                        failures.append(error)
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            if failures:
                raise ExceptionGroup("process benchmark cleanup failed", failures)

        records = [
            json.loads(file.read_text()) for file in sorted(path.glob("probe-*.json"))
        ]
        expected = {"combined"} if args.topology == "combined" else {"control", "media"}
        if {record["role"] for record in records} != expected:
            raise RuntimeError("missing vehicle component report")
        host = json.loads((path / "host.json").read_text())
        if args.topology == "split" and (
            len(host["outcomes"]) != 2
            or any(item["exitcode"] != 0 or item["forced"] for item in host["outcomes"])
        ):
            raise RuntimeError("vehicle child shutdown failed")
        roles = {str(os.getpid()): "harness", str(host["pid"]): "supervisor"}
        for record in records:
            roles[str(record["pid"])] = record["role"]
            if (
                record["forbidden_control_imports"]
                or record["remaining_threads"]
                or not record["camera_threads_closed"]
            ):
                raise RuntimeError(
                    f"vehicle {record['role']} isolation/cleanup failed: "
                    f"imports={record['forbidden_control_imports']}, "
                    f"threads={record['remaining_threads']}, "
                    f"cameras_closed={record['camera_threads_closed']}"
                )
            merge_trace(probe, record, sample_times)
        with probe.lock:
            for stages in probe.frames.values():
                if (
                    "callback" in stages
                    and not {
                        "capture",
                        "encoder_input",
                        "encode_start",
                        "encode_end",
                        "decode_start",
                        "decode_end",
                        "track_recv",
                    }
                    <= stages.keys()
                ):
                    probe.error("incomplete_received_frame_trace")
        report["process_roles"] = roles
        report["child_outcomes"] = host["outcomes"]
        report["component_cleanup"] = [
            {
                key: record[key]
                for key in (
                    "role",
                    "pid",
                    "forbidden_control_imports",
                    "remaining_threads",
                    "camera_threads_closed",
                )
            }
            for record in records
        ]
        report["encoder_instances"] = [
            codec for record in records for codec in record.get("codecs", [])
        ]
        report["resource_observations"] = observations
        for name, (before, after) in phases.items():
            begin, end = before["timestamp"], after["timestamp"]
            result = probe.window(views, begin, end)
            phase_observations = [
                before,
                *(item for item in observations if begin < item["timestamp"] < end),
                after,
            ]
            if any(set(item["processes"]) != set(roles) for item in phase_observations):
                raise RuntimeError("process coverage changed during measurement")
            resources = {}
            for pid, role in roles.items():
                rows = [item["processes"][pid] for item in phase_observations]
                if len({row["start_ticks"] for row in rows}) != 1:
                    raise RuntimeError("PID reused during measurement")
                resources[role] = {
                    "cpu_percent_one_core": (rows[-1]["cpu_s"] - rows[0]["cpu_s"])
                    / (end - begin)
                    * 100,
                    **{
                        key: distribution([row[key] for row in rows])
                        for key in (
                            "rss_mib",
                            "pss_mib",
                            "native_threads",
                            "file_descriptors",
                        )
                    },
                }
            resources["total"] = {
                "cpu_percent_one_core": sum(
                    value["cpu_percent_one_core"] for value in resources.values()
                ),
                **{
                    key: distribution(
                        [
                            sum(row[key] for row in item["processes"].values())
                            for item in phase_observations
                        ]
                    )
                    for key in (
                        "rss_mib",
                        "pss_mib",
                        "native_threads",
                        "file_descriptors",
                    )
                },
            }
            result["resources"] = resources
            result["resource_boundaries"] = {"before": before, "after": after}
            if result["samples"].get("control_armed", {}).get("p50") != 1:
                probe.error("control_unavailable_during_phase")
            with probe.lock:
                if any(
                    value != 1
                    for timestamp, value in probe.series.get("control_armed", [])
                    if begin <= timestamp < end
                ):
                    probe.error("control_disarmed_during_phase")
            for view in views:
                if result["views"][view]["rates"]["paint"]["count"] == 0:
                    probe.error("empty_view_window")
            for metric in (
                "control_sample_to_ack_ms",
                "control_sample_to_mock_submit_ms",
            ):
                if not result["samples"].get(metric, {}).get("count"):
                    probe.error("empty_control_window")
            report[name] = result
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology", choices=("combined", "split"), required=True)
    parser.add_argument("--views", type=int, choices=(1, 4), default=4)
    parser.add_argument(
        "--profile", choices=("small", "production"), default="production"
    )
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not (
        math.isfinite(args.seconds)
        and 0.5 <= args.seconds <= 60
        and math.isfinite(args.warmup)
        and 0 <= args.warmup <= 10
    ):
        parser.error("seconds must be 0.5..60; warmup must be 0..10")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"valid": false, "status": "started"}\n')
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from media_probe import Probe, measured_cache, observe_display
    from PyQt6.QtCore import Qt, QTimer
    from PyQt6.QtWidgets import QApplication
    from rd_core.video_safety import VideoGate
    from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget

    app = QApplication([])
    probe, gate, stop, outcome = Probe(), VideoGate(), threading.Event(), Future()
    cache = measured_cache(probe)
    widget = MultiVideoWidget(cache, video_gate=gate)
    widget.resize(1280, 720)
    observe_display(widget, cache, probe)
    widget.show()
    refresh = QTimer()
    refresh.setTimerType(Qt.TimerType.PreciseTimer)
    refresh.timeout.connect(widget.check_expiry)
    refresh.start(16)
    last_tick = time.monotonic()

    def tick():
        nonlocal last_tick
        now = time.monotonic()
        probe.sample("gui_timer_lag_ms", max(0, now - last_tick - 0.01) * 1000)
        last_tick = now
        if outcome.done():
            app.quit()

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(10)

    def backend():
        try:
            with probe.hooks():

                async def bounded():
                    async with async_timeout(2 * (args.seconds + args.warmup) + 60):
                        return await run_case(args, probe, cache, gate, stop)

                result = asyncio.run(bounded())
            outcome.set_result(result)
        except BaseException as error:
            outcome.set_exception(error)

    worker = threading.Thread(target=backend, name="process-benchmark-backend")
    worker.start()
    try:
        app.exec()
    finally:
        stop.set()
        refresh.stop()
        timer.stop()
        widget.close()
        worker.join(20)
    if worker.is_alive():
        raise RuntimeError("process benchmark backend did not terminate")
    result = outcome.result()
    sources = [
        *ROOT.glob("apps/*/src/**/*.py"),
        *ROOT.glob("packages/*/src/**/*.py"),
        *ROOT.glob("scripts/*.py"),
    ]
    report = {
        "schema": "rd-process-benchmark/1",
        "valid": not probe.errors,
        "scope": "same-host loopback WSS/WebRTC/QUIC; synthetic read completion to Qt offscreen paint; mock actuator",
        "resource_scope": "harness plus complete vehicle subtree; RSS sums double-count shared pages, PSS apportions them; multiprocessing resource tracker excluded",
        "physical_display_or_stop_verified": False,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_model": next(
            (
                line.split(":", 1)[1].strip()
                for line in Path("/proc/cpuinfo").read_text().splitlines()
                if line.startswith("model name")
            ),
            platform.processor(),
        ),
        "cpu_count": os.cpu_count(),
        "qt_platform": app.platformName(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in (
                "aiortc",
                "av",
                "numpy",
                "opencv-python",
                "PyQt6",
                "aioquic",
                "websockets",
            )
        },
        "workload": {
            "views": args.views,
            "profile": args.profile,
            "seconds_per_phase": args.seconds,
            "warmup_seconds": args.warmup,
            "capture_fps": 30,
            "scene": "moving-texture",
            "seed": 42,
            "ui_poll_ms": 16,
            "widget_size": [1280, 720],
            "encoder_workers": None,
        },
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(sources)
        },
        "measurement_errors": probe.errors,
        **result,
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "valid": report["valid"],
                "output": str(args.output),
                "topology": args.topology,
            }
        )
    )
    if not report["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
