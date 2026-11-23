"""Benchmark configured vehicle cameras without signaling or WebRTC."""

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from rd_core.configuration import load_config
from rd_core.settings import VehicleSettings
from rd_vehicle.main import _create_camera


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/vehicle.lan.yaml")
    )
    parser.add_argument(
        "--backend",
        choices=("config", "opencv", "pyav"),
        default="config",
        help="use each configured backend, or override every physical camera",
    )
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args()
    if not 0 < args.seconds <= 120:
        parser.error("--seconds must be in (0, 120]")

    settings = load_config(
        VehicleSettings, "vehicle", path=args.config, environ={}
    ).value
    camera_settings = [
        item
        for item in settings.cameras
        if item.source is not None and item.source != "synthetic"
    ]
    if args.backend != "config":
        camera_settings = [
            replace(item, capture_backend=args.backend) for item in camera_settings
        ]
    if not camera_settings:
        parser.error("configuration contains no physical cameras")

    cameras = [_create_camera(item) for item in camera_settings]
    started = []
    wall_started = time.monotonic()
    cpu_started = time.process_time()
    try:
        for camera in cameras:
            camera.start_capture()
            started.append(camera)
        initial = {camera.camera_name: camera.get_frame_count() for camera in cameras}
        measured_at = time.monotonic()
        time.sleep(args.seconds)
        elapsed = time.monotonic() - measured_at
        results = []
        for camera, item in zip(cameras, camera_settings):
            frames = camera.get_frame_count() - initial[camera.camera_name]
            image = camera.get_snapshot()
            results.append(
                {
                    "name": camera.camera_name,
                    "source": item.source,
                    "backend": item.capture_backend,
                    "requested": {
                        "width": item.width,
                        "height": item.height,
                        "fps": item.fps,
                    },
                    "frames": frames,
                    "observed_fps": frames / elapsed,
                    "frame_shape": list(image.pixels.shape) if image else None,
                    "diagnostics": camera.diagnostics,
                }
            )
        report = {
            "elapsed_s": elapsed,
            "startup_and_measurement_s": time.monotonic() - wall_started,
            "process_cpu_percent_one_core": 100
            * (time.process_time() - cpu_started)
            / (time.monotonic() - wall_started),
            "cameras": results,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        for camera in reversed(started):
            camera.stop_capture()


if __name__ == "__main__":
    main()
