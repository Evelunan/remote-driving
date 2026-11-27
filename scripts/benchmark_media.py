"""Measure the real local WSS/WebRTC/QUIC/Qt pipeline with synthetic devices.

Run one case per process. Same-host monotonic frame correlation is not a
cross-host, camera-exposure or physical-display latency measurement.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from media_probe import VIEWS, Probe, measured_cache, observe_display, stamp
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication
from rd_core.async_compat import ExceptionGroup, async_timeout
from rd_core.auth import AuthProvider
from rd_core.samples import ControlInput
from rd_core.settings import (
    AuthSettings,
    CameraEncodingSettings,
    ClientNetworkSettings,
    CredentialSettings,
    DrivingSettings,
    PrincipalSettings,
    RelaySettings,
    SignalingSettings,
    VehicleSettings,
    VideoSafetySettings,
)
from rd_core.video_safety import VideoGate
from rd_driving.app.control_client import DrivingControlClient
from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
from rd_server.relay.control_service import ControlRelayService
from rd_server.runtime import ServerRuntime
from rd_server.signaling.server import SignalingServer
from rd_vehicle.application.control_client import VehicleControlClient
from rd_vehicle.application.multi_camera_manager import MultiCameraManager
from rd_vehicle.infrastructure.cameras.buffer import CaptureBuffer
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.controller.mock_controller import MockController
from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer
from rd_webrtc.opencv import cv2
from simulate_control import temporary_tls

ROOT = Path(__file__).resolve().parents[1]


def resource_sample(probe):
    # Linux resident pages include native arrays/codecs, unlike tracemalloc.
    pages = int(Path("/proc/self/statm").read_text().split()[1])
    probe.sample("rss_mib", pages * os.sysconf("SC_PAGE_SIZE") / 1024**2)
    probe.sample("python_threads", threading.active_count())
    probe.sample("native_threads", len(list(Path("/proc/self/task").iterdir())))


async def network_snapshot(publisher, receiver):
    result = {"timestamp": time.monotonic(), "streams": [], "paths": []}
    for role, endpoint in (("publisher", publisher), ("receiver", receiver)):
        for peer_id, peer in tuple(endpoint.peers.items()):
            names = {
                str(id(t.sender.track)): t.sender.track.id
                for t in peer.pc.getTransceivers()
                if t.sender.track is not None
            }
            for stats in (await peer.pc.getStats()).values():
                row = asdict(stats)
                row["role"], row["peer"] = role, peer_id
                if row.get("trackId") in names:
                    row["view"] = names[row["trackId"]]
                for key, value in tuple(row.items()):
                    if hasattr(value, "isoformat"):
                        row[key] = value.isoformat()
                result["streams"].append(row)
            # aiortc doesn't expose candidate-pair stats. This diagnostic is
            # version-pinned and records the actual nominated pair.
            transports = {
                t.sender.transport.transport for t in peer.pc.getTransceivers()
            }
            for transport in transports:
                for pair in transport._connection._nominated.values():
                    local, remote = pair.local_candidate, pair.remote_candidate
                    if local.host != "127.0.0.1" or remote.host != "127.0.0.1":
                        raise RuntimeError("benchmark traffic escaped loopback")
                    result["paths"].append(
                        {
                            "role": role,
                            "local": local.host,
                            "remote": remote.host,
                            "local_type": local.type,
                            "remote_type": remote.type,
                        }
                    )
    return result


def network_delta(before, after):
    elapsed = after["timestamp"] - before["timestamp"]
    initial = {(row["role"], row["id"]): row for row in before["streams"]}
    streams = []
    for row in after["streams"]:
        old = initial.get((row["role"], row["id"]))
        if old is None:
            continue
        if row["type"] == "outbound-rtp" and row["role"] == "publisher":
            streams.append(
                {
                    "id": row["id"],
                    "view": row.get("view"),
                    "rtp_payload_kbps": (row["bytesSent"] - old["bytesSent"])
                    * 8
                    / elapsed
                    / 1000,
                    "packets_sent": row["packetsSent"] - old["packetsSent"],
                }
            )
        elif row["type"] == "inbound-rtp" and row["role"] == "receiver":
            streams.append(
                {
                    "id": row["id"],
                    "packets_received": row["packetsReceived"] - old["packetsReceived"],
                    "packets_lost_delta": row["packetsLost"] - old["packetsLost"],
                    "jitter_ms_at_end": row["jitter"] / 90,
                }
            )
        elif row["type"] == "remote-inbound-rtp" and row["role"] == "publisher":
            streams.append(
                {
                    "id": row["id"],
                    "rtcp_rtt_ms_at_end": row["roundTripTime"] * 1000
                    if row["roundTripTime"] is not None
                    else None,
                }
            )
    return {"seconds": elapsed, "streams": streams, "selected_paths": after["paths"]}


async def run_case(args, probe, cache, gate, stop):
    views = VIEWS[: args.views]
    sample_times = OrderedDict()
    current = {"throttle": 0.0}

    class Camera(TestCamera):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            coarse = np.random.default_rng(self._seed).integers(
                0, 256, (48, 64, 3), dtype=np.uint8
            )
            self.texture = cv2.resize(
                coarse, (self.width, self.height), interpolation=cv2.INTER_LINEAR
            )
            self.diagnostics["scene"] = args.scene

        def _read(self):
            pixels = (
                np.roll(self.texture, (self.get_frame_count() + 1) * 3, axis=1)
                if args.scene == "moving-texture"
                else super()._read()
            )
            stamp(pixels, self.camera_name, self.get_frame_count() + 1)
            return pixels

    class Buffer(CaptureBuffer):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def publish_owned(self, pixels, *, captured_at=None):
            probe.mark((self.name, self.sequence + 1), "capture", captured_at)
            super().publish_owned(pixels, captured_at=captured_at)

    class Vehicle(VehicleControlClient):
        def _post_result(self, command, executed, detail):
            with probe.lock:
                sampled_at = sample_times.get(command.source_timestamp)
            if executed and sampled_at is not None:
                probe.sample(
                    "control_sample_to_mock_submit_ms",
                    (time.monotonic() - sampled_at) * 1000,
                )
            super()._post_result(command, executed, detail)

    async def until(predicate, timeout=15):
        async with async_timeout(timeout):
            while not predicate():
                if stop.is_set():
                    raise RuntimeError("benchmark interrupted")
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("benchmark component exited")
                await asyncio.sleep(0.01)

    def all_painted_after(timestamp, source_peer_id=None):
        with probe.lock:
            painted = {
                name
                for (name, _), values in probe.frames.items()
                if values.get("paint", 0) >= timestamp
                and (
                    source_peer_id is None
                    or values.get("source_peer_id") == source_peer_id
                )
            }
        return set(views) <= painted

    credentials = {
        name: secrets.token_urlsafe(32)
        for name in ("RD_BENCH_VEHICLE", "RD_BENCH_DRIVER")
    }
    previous = {key: os.environ.get(key) for key in credentials}
    os.environ.update(credentials)
    tasks, report, stats = [], {}, []
    manager = publisher = receiver = runtime = vehicle = None
    with tempfile.TemporaryDirectory(prefix="rd-media-benchmark-") as directory:
        try:
            tls = temporary_tls(Path(directory))
            auth = AuthProvider(
                AuthSettings(
                    (
                        PrincipalSettings(
                            "bench-vehicle",
                            "vehicle",
                            "RD_BENCH_VEHICLE",
                            (42,),
                            ("benchmark",),
                        ),
                        PrincipalSettings(
                            "bench-driver",
                            "driving",
                            "RD_BENCH_DRIVER",
                            (42,),
                            ("benchmark",),
                        ),
                    )
                ),
                environ=credentials,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(tls.cert, tls.key)
            signaling = SignalingServer(
                SignalingSettings(port=0), tls_context=context, auth=auth
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
            publisher = WebRTCStreamer(
                "benchmark",
                network,
                CredentialSettings("RD_BENCH_VEHICLE"),
                session=42,
                encoder_workers=args.encoder_workers,
            )
            receiver = WebRTCReceiver(
                "benchmark",
                network,
                CredentialSettings("RD_BENCH_DRIVER"),
                views=views,
                low_latency_vp8=args.low_latency_vp8,
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
            manager = MultiCameraManager(publisher, video_policy=VideoSafetySettings())
            for view in views:
                width, height = (
                    (320, 240)
                    if args.profile == "small"
                    else (1080, 720)
                    if view == "front"
                    else (640, 480)
                )
                camera = Camera(width=width, height=height, fps=30, name=view)
                camera.buffer = Buffer(view)
                policy = None
                if args.encoding_profile == "differentiated":
                    policy = (
                        CameraEncodingSettings(1_000_000, 250_000, 1_500_000, 1)
                        if view == "front"
                        else CameraEncodingSettings(500_000, 250_000, 600_000, 1)
                    )
                manager.add_camera(camera, encoding=policy)
            vehicle_settings = VehicleSettings(
                controller="mock",
                session=42,
                network=network,
                credentials=CredentialSettings("RD_BENCH_VEHICLE"),
            )
            vehicle = Vehicle(vehicle_settings, MockController(vehicle_settings.motion))
            samples = asyncio.Queue(maxsize=1)

            def record_control_ready(state, _detail):
                if state == "active":
                    report.setdefault(
                        "initial_control_ready_ms", (time.monotonic() - started) * 1000
                    )

            driver = DrivingControlClient(
                DrivingSettings(
                    session=42,
                    network=network,
                    credentials=CredentialSettings("RD_BENCH_DRIVER"),
                ),
                samples,
                on_status=record_control_ready,
                on_execution=lambda sample, result, elapsed: probe.sample(
                    "control_sample_to_ack_ms", elapsed
                ),
            )

            async def input_source():
                while True:
                    now, wall = time.monotonic(), time.time_ns() // 1_000_000
                    with probe.lock:
                        sample_times[wall] = now
                        while len(sample_times) > 4096:
                            sample_times.popitem(last=False)
                    if samples.full():
                        samples.get_nowait()
                    samples.put_nowait(
                        ControlInput(current["throttle"], 0, 0, now, wall)
                    )
                    await asyncio.sleep(0.01)

            async def monitor():
                deadline, next_resources, next_stats = time.monotonic(), 0, 0
                previous_cpu, previous_wall = time.process_time(), time.monotonic()
                while True:
                    deadline += 0.01
                    await asyncio.sleep(max(0, deadline - time.monotonic()))
                    now = time.monotonic()
                    probe.sample(
                        "backend_event_loop_lag_ms", max(0, now - deadline) * 1000
                    )
                    deadline = now  # Observe lag without catch-up bursts.
                    if now >= next_resources:
                        resource_sample(probe)
                        cpu = time.process_time()
                        probe.sample(
                            "process_cpu_percent_one_core",
                            (cpu - previous_cpu) / (now - previous_wall) * 100,
                        )
                        previous_cpu, previous_wall, next_resources = (
                            cpu,
                            now,
                            now + 0.1,
                        )
                    if now >= next_stats:
                        stats.append(await network_snapshot(publisher, receiver))
                        next_stats = now + 1

            await manager.start()
            started = time.monotonic()
            tasks = [
                asyncio.create_task(coroutine)
                for coroutine in (
                    manager.wait(),
                    publisher.start(),
                    receiver.start(),
                    vehicle.run(),
                    driver.run(),
                    input_source(),
                    monitor(),
                )
            ]
            await until(lambda: vehicle.connection is not None)
            await until(driver.request_start_control)
            await until(lambda: all_painted_after(started))
            with probe.lock:
                report["first_paint_ms"] = {
                    view: (
                        min(
                            data["paint"]
                            for (name, _), data in probe.frames.items()
                            if name == view and "paint" in data
                        )
                        - started
                    )
                    * 1000
                    for view in views
                }
            await until(lambda: driver.armed)
            current["throttle"] = 0.25

            async def phase(name):
                warmup_end = time.monotonic() + args.warmup
                await until(lambda: time.monotonic() >= warmup_end)
                before = await network_snapshot(publisher, receiver)
                begin, cpu = time.monotonic(), time.process_time()
                await until(
                    lambda: time.monotonic() >= begin + args.seconds,
                    timeout=args.seconds + 5,
                )
                end, cpu_elapsed = time.monotonic(), time.process_time() - cpu
                after = await network_snapshot(publisher, receiver)
                result = probe.window(views, begin, end)
                result["process_cpu_percent_one_core"] = (
                    cpu_elapsed / (end - begin) * 100
                )
                result["network"] = network_delta(before, after)
                report[name] = result

            await phase("steady")
            old_id, old_epoch = publisher.my_id, driver.epoch
            reconnect = time.monotonic()
            await signaling.manager.peers[old_id].websocket.close(
                1012, "benchmark reconnect"
            )
            await until(
                lambda: (
                    publisher.my_id is not None
                    and publisher.my_id != old_id
                    and all_painted_after(reconnect, publisher.my_id)
                )
            )
            report["reconnect_all_views_painted_ms"] = (
                time.monotonic() - reconnect
            ) * 1000
            if not driver.armed or driver.epoch != old_epoch:
                raise RuntimeError("media reconnect interrupted the control lease")
            report["control_retained_during_media_reconnect"] = True
            await phase("recovered")
            report["cameras"] = [camera.diagnostics for camera in manager.cameras]
            report["network_samples"] = stats
            if publisher.encoding_pool is not None:
                report["encoding_pool"] = {
                    "workers": publisher.encoding_pool.workers,
                    "completed": publisher.encoding_pool.completed,
                    "expired_after_encode": publisher.encoding_pool.expired,
                }
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [value for value in results if isinstance(value, Exception)]
            for resource, operation in (
                (manager, "stop"),
                (publisher, "close"),
                (receiver, "close"),
                (runtime, "stop"),
            ):
                if resource is not None:
                    try:
                        await getattr(resource, operation)()
                    except Exception as error:  # noqa: BLE001 - finish all owned cleanup before reporting
                        failures.append(error)
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            if failures:
                raise ExceptionGroup("benchmark component failed", failures)
    report["camera_threads_closed"] = all(
        camera._thread is None for camera in manager.cameras
    )
    report["control_threads_closed"] = not any(
        thread.is_alive() for thread in vehicle.supervisor._threads
    )
    report["encoding_threads_closed"] = not any(
        thread.is_alive() and thread.name.startswith("rd-encode")
        for thread in threading.enumerate()
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--views", type=int, choices=(1, 4), default=4)
    parser.add_argument(
        "--profile", choices=("small", "production"), default="production"
    )
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--label", default="baseline")
    parser.add_argument(
        "--scene", choices=("moving-texture", "moving-flat"), default="moving-texture"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--encoder-workers", type=int)
    parser.add_argument("--low-latency-vp8", action="store_true")
    parser.add_argument(
        "--encoding-profile", choices=("upstream", "differentiated"), default="upstream"
    )
    args = parser.parse_args()
    if args.encoder_workers is not None and not 1 <= args.encoder_workers <= 32:
        parser.error("encoder-workers must be 1..32")
    if args.encoding_profile != "upstream" and args.encoder_workers is None:
        parser.error("per-view encoding profile requires encoder-workers")
    if not (
        math.isfinite(args.seconds)
        and 0.5 <= args.seconds <= 60
        and math.isfinite(args.warmup)
        and 0 <= args.warmup <= 10
    ):
        parser.error("seconds must be 0.5..60 and warmup 0..10")
    if not Path("/proc/self/statm").exists():
        parser.error("resource measurements currently require Linux")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"valid": false, "status": "started"}\n')
    probe, gate, stop, outcome = Probe(), VideoGate(), threading.Event(), Future()
    cache = measured_cache(probe)
    app = QApplication([])
    widget = MultiVideoWidget(cache, video_gate=gate)
    widget.resize(1280, 720)
    observe_display(widget, cache, probe)
    widget.show()
    refresh = QTimer()
    refresh.setTimerType(Qt.TimerType.PreciseTimer)
    refresh.timeout.connect(widget.check_expiry)
    refresh.start(16)
    last_tick = time.monotonic()

    def gui_tick():
        nonlocal last_tick
        now = time.monotonic()
        probe.sample("gui_timer_lag_ms", max(0, now - last_tick - 0.01) * 1000)
        last_tick = now
        if outcome.done():
            app.quit()

    monitor = QTimer()
    monitor.setTimerType(Qt.TimerType.PreciseTimer)
    monitor.timeout.connect(gui_tick)
    monitor.start(10)

    def backend():
        try:
            with probe.hooks():

                async def bounded_run():
                    async with async_timeout(2 * (args.seconds + args.warmup) + 45):
                        return await run_case(args, probe, cache, gate, stop)

                result = asyncio.run(bounded_run())
            outcome.set_result(result)
        except BaseException as error:  # noqa: BLE001 - propagate worker failure to the main thread
            outcome.set_exception(error)

    worker = threading.Thread(target=backend, name="benchmark-backend")
    worker.start()
    try:
        app.exec()
    finally:
        stop.set()
        refresh.stop()
        monitor.stop()
        widget.close()
        worker.join(20)
    if worker.is_alive():
        raise RuntimeError("benchmark backend did not terminate")
    result = outcome.result()
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
    source_paths = [
        *ROOT.glob("apps/*/src/**/*.py"),
        *ROOT.glob("packages/*/src/**/*.py"),
        Path(__file__),
        Path(__file__).with_name("media_probe.py"),
    ]
    report = {
        "schema": "rd-media-benchmark/1",
        "label": args.label,
        "scope": "single-process loopback software pipeline; synthetic camera read completion to Qt offscreen paint; mock actuator",
        "topology": "GUI main thread; one backend asyncio thread for server/media/control; native capture/codec/actuator threads",
        "physical_display_or_stop_verified": False,
        "gpu_metrics": None,
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
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "git_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(source_paths)
        },
        "workload": {
            "views": args.views,
            "profile": args.profile,
            "seconds_per_phase": args.seconds,
            "warmup_seconds": args.warmup,
            "capture_fps": 30,
            "ui_poll_ms": 16,
            "widget_size": [1280, 720],
            "subscribers": 1,
            "seed": 42,
            "scene": args.scene,
            "encoder_workers": args.encoder_workers,
            "encoding_profile": args.encoding_profile,
            "low_latency_vp8": args.low_latency_vp8,
        },
        "encoder_instances": probe.codecs,
        "measurement_errors": probe.errors,
        "probe_frame_records": probe.high_water,
        "valid": not probe.errors,
        **result,
    }
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "valid": report["valid"],
                "output": str(args.output),
                "views": args.views,
                "first_paint_ms": result["first_paint_ms"],
                "cpu_percent": result["steady"]["process_cpu_percent_one_core"],
            },
            indent=2,
        )
    )
    if not report["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
