"""Deterministic four-camera cache workload, with exact NumPy copy accounting."""

import argparse
import hashlib
import importlib.util
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "benchmarks/baselines/frame_buffer_before_b11.py"
CURRENT = ROOT / "apps/driving/src/rd_driving/infrastructure/video/frame_buffer.py"
SHAPES = {
    "front": (720, 1080, 3),
    "back": (480, 640, 3),
    "left": (480, 640, 3),
    "right": (480, 640, 3),
}


class CountedArray(np.ndarray):
    copies = 0
    copied_bytes = 0

    def copy(self, order="C"):
        CountedArray.copies += 1
        CountedArray.copied_bytes += self.nbytes
        return super().copy(order=order)


def implementation(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.FrameBuffer


def measure(factory, seconds, capture_fps, poll_hz):
    rng = np.random.default_rng(42)
    inputs = {
        name: rng.integers(0, 256, shape, dtype=np.uint8).view(CountedArray)
        for name, shape in SHAPES.items()
    }
    cache = factory()
    last_ids = {name: -1 for name in SHAPES}
    next_capture, published, displayed = 0, 0, 0
    CountedArray.copies = CountedArray.copied_bytes = 0
    cpu_start, wall_start = time.process_time(), time.perf_counter()
    # Logical scheduling avoids sleep/jitter; this measures cache work, not a
    # real-time video pipeline or display/GPU/end-to-end latency.
    for tick in range(round(seconds * poll_hz)):
        if tick / poll_hz >= next_capture / capture_fps:
            for name, frame in inputs.items():
                frame[0, 0, 0] = next_capture % 256
                cache.put(name, frame)
                published += 1
            next_capture += 1
        for name in SHAPES:
            item = cache.get(name)
            if (
                item is not None
                and item.frame is not None
                and item.id != last_ids[name]
            ):
                last_ids[name] = item.id
                displayed += 1
    return {
        "wall_s": time.perf_counter() - wall_start,
        "cpu_s": time.process_time() - cpu_start,
        "copies": CountedArray.copies,
        "copied_bytes": CountedArray.copied_bytes,
        "published_frames": published,
        "displayed_unique_frames": displayed,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=("baseline", "current", "compare"), default="compare"
    )
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--capture-fps", type=int, default=30)
    parser.add_argument("--poll-hz", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (
        not 0 < args.seconds <= 60
        or not 0 < args.capture_fps <= args.poll_hz <= 1000
        or round(args.seconds * args.poll_hz) < 1
        or not 1 <= args.repeats <= 20
    ):
        parser.error("invalid benchmark workload")
    versions = {"baseline": BASELINE, "current": CURRENT}
    if args.mode != "compare":
        versions = {args.mode: versions[args.mode]}
    report = {
        "scope": "frame cache only; no decoder, network, Qt or GPU",
        "baseline_revision": "9ec4447",
        "python": platform.python_version(),
        "numpy": np.__version__,
        "platform": platform.platform(),
        "logical_seconds": args.seconds,
        "capture_fps": args.capture_fps,
        "poll_hz": args.poll_hz,
        "shapes": SHAPES,
        "results": {},
    }
    for label, path in versions.items():
        factory = implementation(path, "benchmark_frame_buffer_" + label)
        measure(factory, min(0.2, args.seconds), args.capture_fps, args.poll_hz)
        samples = [
            measure(factory, args.seconds, args.capture_fps, args.poll_hz)
            for _ in range(args.repeats)
        ]
        report["results"][label] = {
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "runs": samples,
            "median_wall_s": statistics.median(item["wall_s"] for item in samples),
            "median_cpu_s": statistics.median(item["cpu_s"] for item in samples),
        }
    if args.mode == "compare":
        before, after = (report["results"][name] for name in ("baseline", "current"))
        assert (
            before["runs"][0]["displayed_unique_frames"]
            == after["runs"][0]["displayed_unique_frames"]
        )
        report["comparison"] = {
            "cpu_reduction_fraction": 1
            - after["median_cpu_s"] / before["median_cpu_s"],
            "wall_reduction_fraction": 1
            - after["median_wall_s"] / before["median_wall_s"],
            "copy_bytes_reduction_fraction": 1
            - after["runs"][0]["copied_bytes"] / before["runs"][0]["copied_bytes"],
            "same_unique_frame_count": True,
        }
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n")
    print(output)


if __name__ == "__main__":
    main()
