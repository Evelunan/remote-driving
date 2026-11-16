"""Spawn-safe diagnostics for benchmark_processes; never imported by applications.

Keep module imports free of NumPy/Qt/media so a control-only child retains the
production dependency boundary. Scalar traces are written only after shutdown.
"""

import asyncio
import json
import os
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def process_resources(pid):
    root = Path(f"/proc/{pid}")
    # comm can contain spaces/parentheses. Fields after the final ')' start at 3.
    fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
    memory = dict(
        (line.split(":", 1)[0], float(line.split()[1]) / 1024)
        for line in (root / "smaps_rollup").read_text().splitlines()
        if line.startswith(("Rss:", "Pss:"))
    )
    return {
        "pid": pid,
        "start_ticks": int(fields[19]),
        "cpu_s": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
        "rss_mib": memory["Rss"],
        "pss_mib": memory["Pss"],
        "native_threads": int(fields[17]),
        "file_descriptors": len(list((root / "fd").iterdir())),
    }


def process_tree(pid):
    result = {pid}
    for task in Path(f"/proc/{pid}/task").iterdir():
        try:
            children = (task / "children").read_text().split()
        except FileNotFoundError:
            # Codec threads can finish between listing task/ and reading the
            # per-thread children file. Only disappearance of the process itself
            # is a coverage failure; Linux reparents children to a live thread.
            if not Path(f"/proc/{pid}").exists():
                raise
            continue
        for child in children:
            result.update(process_tree(int(child)))
    return result


def media_instruments(stack, cameras):
    import numpy as np
    from media_probe import Probe, stamp
    from rd_vehicle.infrastructure.cameras.buffer import CaptureBuffer
    from rd_vehicle.infrastructure.cameras.test_camera import TestCamera
    from rd_webrtc.opencv import cv2

    probe = Probe()
    stack.enter_context(probe.hooks())

    class Buffer(CaptureBuffer):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def publish_owned(self, pixels, *, captured_at=None):
            probe.mark((self.name, self.sequence + 1), "capture", captured_at)
            super().publish_owned(pixels, captured_at=captured_at)

    class Camera(TestCamera):
        def __init__(self, settings):
            super().__init__(
                settings.width, settings.height, settings.fps, settings.name
            )
            coarse = np.random.default_rng(self._seed).integers(
                0, 256, (48, 64, 3), dtype=np.uint8
            )
            self.texture = cv2.resize(coarse, (self.width, self.height))
            self.buffer = Buffer(self.camera_name)
            cameras.append(self)

        def _read(self):
            sequence = self.get_frame_count() + 1
            pixels = np.roll(self.texture, sequence * 3, axis=1)
            stamp(pixels, self.camera_name, sequence)
            return pixels

    return probe, Camera


async def recorded_run(settings, stop):
    from rd_vehicle.main import run

    label = "combined" if len(settings.components) == 2 else settings.components[0]
    directory = settings.logging.directory
    events, loop_lag, python_threads, cameras = [], [], [], []
    probe = None

    async def monitor():
        while True:
            before = time.monotonic()
            await asyncio.sleep(0.01)
            now = time.monotonic()
            if len(loop_lag) >= 20000:
                raise RuntimeError("process probe observation limit exceeded")
            loop_lag.append((now, max(0, now - before - 0.01) * 1000))
            if not python_threads or now - python_threads[-1][0] >= 0.2:
                python_threads.append((now, threading.active_count()))

    with ExitStack() as stack:
        factory = None
        if "media" in settings.components:
            probe, factory = media_instruments(stack, cameras)
        if "control" in settings.components:
            from rd_vehicle.application.control_client import VehicleControlClient

            original = VehicleControlClient._post_result

            def result(client, command, executed, detail):
                if executed:
                    if len(events) >= 20000:
                        raise RuntimeError("control probe observation limit exceeded")
                    events.append((command.source_timestamp, time.monotonic()))
                return original(client, command, executed, detail)

            stack.enter_context(
                patch.object(VehicleControlClient, "_post_result", result)
            )
        monitor_task = asyncio.create_task(monitor())
        try:
            await run(settings, stop, camera_factory=factory)
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            value = {
                "pid": os.getpid(),
                "role": label,
                "loop_lag_ms": loop_lag,
                "python_threads": python_threads,
                "control_executions": events,
                "forbidden_control_imports": sorted(
                    {"cv2", "av", "aiortc", "PyQt6", "numpy"} & sys.modules.keys()
                )
                if label == "control"
                else [],
                "camera_threads_closed": all(
                    camera._thread is None for camera in cameras
                ),
                "remaining_threads": [
                    thread.name
                    for thread in threading.enumerate()
                    if thread.name.startswith(
                        (
                            "capture-",
                            "rd-device",
                            "rd-watchdog",
                            "rd-safety-hook",
                            "rd-encode",
                        )
                    )
                ],
            }
            if probe is not None:
                with probe.lock:
                    value.update(
                        frames=[
                            [list(key), stages] for key, stages in probe.frames.items()
                        ],
                        series=probe.series,
                        codecs=probe.codecs,
                        errors=probe.errors,
                    )
            (directory / f"probe-{label}.json").write_text(
                json.dumps(value, allow_nan=False) + "\n"
            )


def vehicle_host(settings, topology, channel):
    """Same clean vehicle host in both modes; parent EOF always requests exit."""
    from rd_core.logging import configure_logging
    from rd_vehicle.processes import VehicleProcesses

    async def lifetime():
        stop = asyncio.Event()
        channel.setblocking(False)

        async def parent_lifetime():
            try:
                await asyncio.get_running_loop().sock_recv(channel, 1)
            finally:
                stop.set()

        watcher = asyncio.create_task(parent_lifetime())
        group = VehicleProcesses(settings, runner=recorded_run)
        try:
            if topology == "split":
                await group.run(stop)
            else:
                await recorded_run(settings, stop)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            (settings.logging.directory / "host.json").write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "outcomes": group.outcomes,
                    }
                )
                + "\n"
            )

    configure_logging("WARNING")
    try:
        asyncio.run(lifetime())
    finally:
        channel.close()


def merge_trace(probe, record, sample_times):
    """Join by watermark and original input timestamp on the SAME Linux host."""
    for key, stages in record.get("frames", []):
        for stage, timestamp in stages.items():
            if stage in probe.frames.get(tuple(key), {}):
                raise ValueError("duplicate process trace stage")
            probe.mark(tuple(key), stage, timestamp)
    for name, samples in record.get("series", {}).items():
        for timestamp, value in samples:
            probe.sample(name, value, timestamp)
    for name, count in record.get("errors", {}).items():
        probe.errors[name] = probe.errors.get(name, 0) + count
    for source, timestamp in record["control_executions"]:
        sampled = sample_times.get(source)
        if sampled is None or timestamp < sampled:
            raise ValueError("missing/invalid source time for executed control")
        probe.sample(
            "control_sample_to_mock_submit_ms", (timestamp - sampled) * 1000, timestamp
        )
    for key in ("loop_lag_ms", "python_threads"):
        for timestamp, value in record[key]:
            probe.sample(f"vehicle_{record['role']}_{key}", value, timestamp)
