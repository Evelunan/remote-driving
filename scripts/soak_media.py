"""Exercise repeated local WSS/WebRTC/Qt reconnects with bounded observations.

No device or control connection is opened. RSS includes native codec/Qt memory;
retired peers are observed weakly. GC is forced only after all endpoints close.
"""

import argparse
import asyncio
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import secrets
import ssl
import statistics
import subprocess
import tempfile
import threading
import time
import weakref
from collections import Counter, OrderedDict
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import aioice.ice
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication
from rd_core.async_compat import ExceptionGroup, async_timeout
from rd_core.auth import AuthProvider
from rd_core.settings import (
    AuthSettings,
    ClientNetworkSettings,
    CredentialSettings,
    PrincipalSettings,
    SignalingSettings,
)
from rd_driving.infrastructure.video.frame_buffer import FrameBuffer
from rd_driving.infrastructure.video.webrtc_reciver import WebRTCReceiver
from rd_driving.ui.widgets.multi_video_widget import MultiVideoWidget
from rd_server.signaling.server import SignalingServer
from rd_vehicle.application.multi_camera_manager import MultiCameraManager
from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
from rd_vehicle.infrastructure.streams.webrtc_streamer import WebRTCStreamer
from simulate_control import temporary_tls

ROOT = Path(__file__).resolve().parents[1]
VIEWS = ("front", "back", "left", "right")


class Observations(FrameBuffer):
    """Keep counters and at most 128 scalar associations, never frame history."""

    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()
        self.associations = OrderedDict()
        self.received = Counter()
        self.painted = Counter()
        self.last_paint = {}
        self.errors = Counter()

    def put(self, name, pixels, *, source, captured_at=None):
        with self.lock:
            accepted = super().put(name, pixels, source=source, captured_at=captured_at)
            if accepted:
                self.received[name] += 1
                self.associations[name, self.get(name).id] = source.peer_id
                while len(self.associations) > 128:
                    self.associations.popitem(last=False)
            return accepted

    def observe(self, widget):
        for name, display in widget._displays.items():
            original = display.update_frame

            def update(
                pixels,
                frame_id,
                *,
                on_presented=None,
                captured_at=None,
                name=name,
                original=original,
            ):
                with self.lock:
                    source = self.associations.get((name, frame_id))

                def painted():
                    with self.lock:
                        if source is None:
                            self.errors["missing_paint_source"] += 1
                        self.painted[name] += 1
                        self.last_paint[name] = (time.monotonic(), source)
                    if on_presented is not None:
                        on_presented()

                original(
                    pixels, frame_id, on_presented=painted, captured_at=captured_at
                )

            display.update_frame = update

    def ready(self, views, since, source):
        with self.lock:
            return all(
                self.last_paint.get(view, (0, None))[0] > since
                and self.last_paint[view][1] == source
                for view in views
            )

    def counts(self):
        with self.lock:
            return {"received": dict(self.received), "painted": dict(self.painted)}


def resources():
    return {
        "monotonic_s": time.monotonic(),
        "cpu_s": time.process_time(),
        "rss_mib": int(Path("/proc/self/statm").read_text().split()[1])
        * os.sysconf("SC_PAGE_SIZE")
        / 1024**2,
        "file_descriptors": len(list(Path("/proc/self/fd").iterdir())),
        "native_threads": len(list(Path("/proc/self/task").iterdir())),
        "python_threads": sorted(thread.name for thread in threading.enumerate()),
        "gc_collections": [generation["collections"] for generation in gc.get_stats()],
    }


def trend(cycles):
    """Describe growth without inventing a universal safe RSS threshold."""
    points = [cycle["end"] for cycle in cycles]
    tail = points[len(points) // 2 :]
    result = {}
    for key in ("rss_mib", "file_descriptors", "native_threads", "asyncio_tasks"):
        values = [point[key] for point in points]
        result[key] = {
            "first": values[0],
            "last": values[-1],
            "min": min(values),
            "max": max(values),
        }
    x = [point["monotonic_s"] - tail[0]["monotonic_s"] for point in tail]
    y = [point["rss_mib"] for point in tail]
    x_mean, y_mean = statistics.mean(x), statistics.mean(y)
    denominator = sum((value - x_mean) ** 2 for value in x)
    result["last_half_rss_mib_per_minute"] = (
        sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y)) / denominator * 60
        if denominator
        else None
    )
    return result


def paths(publisher, receiver):
    nominated = []
    for endpoint in (publisher, receiver):
        for peer in endpoint.peers.values():
            for transceiver in peer.pc.getTransceivers():
                transport = transceiver.sender.transport.transport
                for pair in transport._connection._nominated.values():
                    local, remote = pair.local_candidate, pair.remote_candidate
                    if (local.host, remote.host, local.type, remote.type) != (
                        "127.0.0.1",
                        "127.0.0.1",
                        "host",
                        "host",
                    ):
                        raise RuntimeError("unexpected network path")
                    nominated.append([local.host, remote.host, local.type, remote.type])
    if not nominated:
        raise RuntimeError("no nominated local media path")
    return nominated


async def run(args, cache, stop, report):
    baseline_tasks = set(asyncio.all_tasks())
    report["before_start"] = resources()
    retired, tasks = [], []
    publisher = receiver = manager = service = None
    credentials = {
        name: secrets.token_urlsafe(32)
        for name in ("RD_SOAK_VEHICLE", "RD_SOAK_VIEWER")
    }
    previous = {key: os.environ.get(key) for key in credentials}
    os.environ.update(credentials)
    views = VIEWS[: args.views]

    def snapshot():
        result = resources()
        result.update(
            asyncio_tasks=len(asyncio.all_tasks()),
            task_kinds=dict(
                Counter(task.get_coro().__qualname__ for task in asyncio.all_tasks())
            ),
            retired_peers_alive=sum(ref() is not None for ref in retired),
            counts=cache.counts(),
        )
        return result

    async def until(predicate, timeout=20):
        async with async_timeout(timeout):
            while not predicate():
                if stop.is_set():
                    raise RuntimeError("soak interrupted")
                for task in tasks:
                    if task.done():
                        task.result()
                        raise RuntimeError("media component ended")
                await asyncio.sleep(0.02)

    async def monitor():
        while True:
            if len(report["samples"]) >= 20000:
                raise RuntimeError("resource sample limit exceeded")
            sample = snapshot()
            report["samples"].append(sample)
            if sample["rss_mib"] > args.max_rss_mib:
                raise RuntimeError("soak resident memory limit exceeded")
            await asyncio.sleep(1)

    with tempfile.TemporaryDirectory(prefix="rd-media-soak-") as temporary:
        try:
            tls = temporary_tls(Path(temporary))
            auth = AuthProvider(
                AuthSettings(
                    (
                        PrincipalSettings(
                            "soak-vehicle",
                            "vehicle",
                            "RD_SOAK_VEHICLE",
                            (42,),
                            ("soak",),
                        ),
                        PrincipalSettings(
                            "soak-viewer", "viewer", "RD_SOAK_VIEWER", (), ("soak",)
                        ),
                    )
                ),
                environ=credentials,
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(tls.cert, tls.key)
            service = SignalingServer(
                SignalingSettings(port=0), tls_context=context, auth=auth
            )
            await service.start()
            network = ClientNetworkSettings(
                signaling_url=f"wss://127.0.0.1:{service._server.sockets[0].getsockname()[1]}",
                signaling_ca_file=tls.cert,
                reconnect_delay_s=0.1,
            )
            publisher = WebRTCStreamer(
                "soak", network, CredentialSettings("RD_SOAK_VEHICLE"), session=42
            )
            receiver = WebRTCReceiver(
                "soak", network, CredentialSettings("RD_SOAK_VIEWER"), views=views
            )
            receiver.on_frame(
                lambda name, pixels, source: cache.put(name, pixels, source=source)
            )
            receiver.on_invalidate(
                lambda source: cache.invalidate_source(
                    source, generation=cache._generation
                )
            )
            manager = MultiCameraManager(publisher)
            for view in views:
                width, height = (
                    (320, 240)
                    if args.profile == "small"
                    else (1080, 720)
                    if view == "front"
                    else (640, 480)
                )
                manager.add_camera(
                    TestCamera(width=width, height=height, fps=30, name=view)
                )
            await manager.start()
            started = time.monotonic()
            tasks = [
                asyncio.create_task(job)
                for job in (
                    manager.wait(),
                    publisher.start(),
                    receiver.start(),
                    monitor(),
                )
            ]
            await until(
                lambda: (
                    publisher.my_id is not None
                    and cache.ready(views, started, publisher.my_id)
                )
            )
            report["first_all_views_ms"] = (time.monotonic() - started) * 1000
            warm = time.monotonic() + args.warmup
            await until(lambda: time.monotonic() >= warm, timeout=args.warmup + 5)
            for cycle in range(args.cycles):
                begin = snapshot()
                await until(
                    lambda begin=begin: (
                        time.monotonic() >= begin["monotonic_s"] + args.seconds
                    ),
                    timeout=args.seconds + 5,
                )
                end = snapshot()
                elapsed = end["monotonic_s"] - begin["monotonic_s"]
                fps = {
                    stage: {
                        view: (
                            end["counts"][stage].get(view, 0)
                            - begin["counts"][stage].get(view, 0)
                        )
                        / elapsed
                        for view in views
                    }
                    for stage in ("received", "painted")
                }
                if any(
                    value == 0 for stage in fps.values() for value in stage.values()
                ):
                    raise RuntimeError(
                        "a view stopped producing during a steady window"
                    )
                record = {
                    "cycle": cycle,
                    "begin": begin,
                    "end": end,
                    "fps": fps,
                    "cpu_percent_one_core": (end["cpu_s"] - begin["cpu_s"])
                    / elapsed
                    * 100,
                    "paths": paths(publisher, receiver),
                }
                report["cycles"].append(record)
                if cycle + 1 < args.cycles:
                    retired.extend(
                        weakref.ref(peer.pc)
                        for endpoint in (publisher, receiver)
                        for peer in endpoint.peers.values()
                    )
                    target = publisher if cycle % 2 == 0 else receiver
                    old_id, reconnect = target.my_id, time.monotonic()
                    await service.manager.peers[old_id].websocket.close(
                        1012, "local soak reconnect"
                    )
                    await until(
                        lambda target=target, old_id=old_id: (
                            target.my_id is not None and target.my_id != old_id
                        )
                    )
                    joined = time.monotonic()
                    await until(
                        lambda joined=joined: cache.ready(
                            views, joined, publisher.my_id
                        )
                    )
                    if any(
                        ref() is not None and ref().connectionState != "closed"
                        for ref in retired
                    ):
                        raise RuntimeError("retired peer remained open after reconnect")
                    record["reconnected_endpoint"] = (
                        "publisher" if target is publisher else "receiver"
                    )
                    record["reconnect_all_views_ms"] = (
                        time.monotonic() - reconnect
                    ) * 1000
                args.output.write_text(
                    json.dumps(report, indent=2, allow_nan=False) + "\n"
                )
                print(
                    json.dumps(
                        {
                            "cycle": cycle + 1,
                            "rss_mib": end["rss_mib"],
                            "retired_peers_alive": end["retired_peers_alive"],
                        }
                    ),
                    flush=True,
                )
        finally:
            for task in tasks:
                task.cancel()
            failures = [
                result
                for result in await asyncio.gather(*tasks, return_exceptions=True)
                if isinstance(result, Exception)
            ]
            for obj, operation in (
                (manager, "stop"),
                (publisher, "close"),
                (receiver, "close"),
                (service, "stop"),
            ):
                if obj is not None:
                    try:
                        await getattr(obj, operation)()
                    except Exception as error:  # noqa: BLE001 - finish all owned cleanup
                        failures.append(error)
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            report["cleanup"] = {
                "camera_threads_closed": manager is None
                or all(camera._thread is None for camera in manager.cameras),
                "endpoint_maps_empty": all(
                    not endpoint.peers
                    and not endpoint._retired
                    and not endpoint._peer_tasks
                    and not endpoint._cleanup_jobs
                    for endpoint in (publisher, receiver)
                    if endpoint
                ),
                "server_rooms_empty": service is None
                or not service.manager.rooms
                and not service.manager.peers,
                "unexpected_tasks": [
                    task.get_coro().__qualname__
                    for task in asyncio.all_tasks() - baseline_tasks
                    if not task.done()
                ],
            }
            report["closed_before_gc"] = snapshot()
            gc.collect()  # Diagnostic only, after endpoints close; never in steady phases.
            await asyncio.sleep(0.1)
            report["closed_after_gc"] = snapshot()
            if failures:
                raise ExceptionGroup("soak cleanup failed", failures)
            if (
                not all(
                    report["cleanup"][key]
                    for key in (
                        "camera_threads_closed",
                        "endpoint_maps_empty",
                        "server_rooms_empty",
                    )
                )
                or report["cleanup"]["unexpected_tasks"]
            ):
                raise RuntimeError("incomplete media cleanup")
            if report["closed_after_gc"]["retired_peers_alive"]:
                raise RuntimeError("retired peers remain reachable after final GC")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--views", type=int, choices=(1, 4), default=4)
    parser.add_argument(
        "--profile", choices=("small", "production"), default="production"
    )
    parser.add_argument("--cycles", type=int, default=60)
    parser.add_argument("--seconds", type=float, default=10)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--max-rss-mib", type=float, default=2048)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"valid": false, "status": "started"}\n')
    if not (
        2 <= args.cycles <= 1000
        and math.isfinite(args.seconds)
        and 0.5 <= args.seconds <= 300
        and math.isfinite(args.warmup)
        and 0 <= args.warmup <= 30
        and args.cycles * (args.seconds + 20) <= 14400
        and math.isfinite(args.max_rss_mib)
        and args.max_rss_mib >= 64
    ):
        parser.error(
            "cycles 2..1000, seconds 0.5..300, warmup 0..30; total budget at most 4h"
        )
    if not Path("/proc/self/statm").exists():
        parser.error("resource observations require Linux")
    if (
        importlib.metadata.version("aiortc") != "1.14.0"
        or importlib.metadata.version("aioice") != "0.10.2"
    ):
        parser.error(
            "review loopback/path diagnostics for the installed media versions"
        )
    report = {
        "schema": "rd-media-soak/1",
        "valid": False,
        "valid_meaning": "all requested windows and reconnects completed, all views rendered, and owned resources closed; RSS trend is observational, not a stability certification",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "single-process local WSS/WebRTC, synthetic camera, real decode/cache/Qt offscreen paint; no QUIC, control device, physical display or TURN",
        "python": platform.python_version(),
        "platform": platform.platform(),
        "versions": {
            name: importlib.metadata.version(name)
            for name in (
                "aiortc",
                "aioice",
                "av",
                "numpy",
                "opencv-python",
                "PyQt6",
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
            for path in sorted(
                [
                    *ROOT.glob("apps/*/src/**/*.py"),
                    *ROOT.glob("packages/*/src/**/*.py"),
                    Path(__file__),
                    Path(__file__).with_name("simulate_control.py"),
                ]
            )
        },
        "workload": {
            "views": args.views,
            "profile": args.profile,
            "cycles": args.cycles,
            "steady_seconds": args.seconds,
            "warmup_seconds": args.warmup,
            "fps": 30,
            "scene": "moving-flat",
            "seed": 42,
            "widget_size": [1280, 720],
            "ui_poll_ms": 16,
            "max_rss_mib": args.max_rss_mib,
        },
        "samples": [],
        "cycles": [],
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    cache, stop, outcome = Observations(), threading.Event(), Future()
    app = QApplication([])
    widget = MultiVideoWidget(cache)
    widget.resize(1280, 720)
    cache.observe(widget)
    widget.show()
    refresh = QTimer()
    refresh.setTimerType(Qt.TimerType.PreciseTimer)
    refresh.timeout.connect(widget.check_expiry)
    refresh.start(16)
    monitor = QTimer()
    monitor.timeout.connect(lambda: app.quit() if outcome.done() else None)
    monitor.start(50)

    def backend():
        try:

            async def bounded():
                async with async_timeout(
                    args.cycles * (args.seconds + 20) + args.warmup + 30
                ):
                    await run(args, cache, stop, report)

            with patch.object(
                aioice.ice, "get_host_addresses", lambda **kwargs: ["127.0.0.1"]
            ):
                asyncio.run(bounded())
            outcome.set_result(None)
        except BaseException as error:  # noqa: BLE001 - propagate worker failure
            outcome.set_exception(error)

    worker = threading.Thread(target=backend, name="soak-backend")
    worker.start()
    try:
        app.exec()
    finally:
        stop.set()
        refresh.stop()
        monitor.stop()
        cache.clear()
        widget.show_message_all("测试已结束")
        widget.close()
        worker.join(20)
        report["after_worker_exit"] = resources()
        report["measurement_errors"] = dict(cache.errors)
        try:
            if worker.is_alive():
                raise RuntimeError("soak backend did not exit")
            outcome.result()
            if cache.errors:
                raise RuntimeError("incomplete paint observations")
            report["trend"] = trend(report["cycles"])
            report["valid"] = True
        except BaseException as error:
            report["error_type"] = type(error).__name__
            raise
        finally:
            args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
