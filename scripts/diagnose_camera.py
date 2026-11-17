"""Save camera frames before WebRTC/Qt and compare USB capture modes.

Run from the project root after stopping rd-vehicle:
    .venv/bin/python scripts/diagnose_camera.py
    .venv/bin/python scripts/diagnose_camera.py --compare

Outputs PNG samples, capture.log and report.json under artifacts/camera-diagnostic/.
No server, credentials, GUI or control connection is required. A black region is
only a measurement: a dark scene can produce the same result as a broken frame.
AUTO leaves FourCC unchanged; the driver may retain a previously selected mode.
"""

import argparse
import json
import math
import platform
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def fourcc_name(value):
    code = int(value)
    return "".join(chr((code >> (8 * i)) & 255) for i in range(4)).strip("\0")


def image_metrics(frame):
    import numpy as np

    # Near-black pixels across >=99% of a row; do not classify scene content
    # as corruption or reject legitimate dark frames based on this heuristic.
    black = np.max(frame, axis=2) <= 8
    rows = np.mean(black, axis=1) >= 0.99
    nonblack_rows = np.flatnonzero(~rows)
    trailing = (
        len(rows) - int(nonblack_rows[-1]) - 1 if len(nonblack_rows) else len(rows)
    )
    return {
        "shape": list(frame.shape),
        "near_black_fraction": float(black.mean()),
        "bottom_quarter_near_black_fraction": float(
            black[-max(1, len(rows) // 4) :].mean()
        ),
        "trailing_near_black_rows": trailing,
    }


def capture(job):
    import numpy as np
    from rd_webrtc.opencv import cv2

    output = Path(job["output"])
    report = {"requested": job, "frames": [], "saved_images": [], "read_failures": 0}
    cap = None
    try:
        synthetic = job["source"] == "synthetic"
        if synthetic:
            report["negotiated"] = {"backend": "synthetic", "fourcc": "BGR"}
        else:
            backend = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY
            cap = cv2.VideoCapture(job["source"], backend)
            if not cap.isOpened():
                raise RuntimeError(
                    "无法打开摄像头；检查设备路径、权限，以及车端是否已退出"
                )
            properties = [("buffersize", cv2.CAP_PROP_BUFFERSIZE, 1)]
            if job["fourcc"] != "AUTO":
                properties.append(
                    (
                        "fourcc",
                        cv2.CAP_PROP_FOURCC,
                        cv2.VideoWriter.fourcc(*job["fourcc"]),
                    )
                )
            properties.extend(
                [
                    ("width", cv2.CAP_PROP_FRAME_WIDTH, job["width"]),
                    ("height", cv2.CAP_PROP_FRAME_HEIGHT, job["height"]),
                    ("fps", cv2.CAP_PROP_FPS, job["fps"]),
                ]
            )
            # Same ordering as USBCamera._open; retain rejected settings.
            report["set_accepted"] = {
                name: bool(cap.set(prop, value)) for name, prop, value in properties
            }
            report["negotiated"] = {
                "backend": cap.getBackendName(),
                "width": cap.get(cv2.CAP_PROP_FRAME_WIDTH),
                "height": cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
                "fps": cap.get(cv2.CAP_PROP_FPS),
                "fourcc": fourcc_name(cap.get(cv2.CAP_PROP_FOURCC)),
            }
            report["mismatches"] = {
                name: {"requested": job[name], "reported": report["negotiated"][name]}
                for name in ("width", "height", "fps", "fourcc")
                if not (name == "fourcc" and job[name] == "AUTO")
                and report["negotiated"][name] != job[name]
            }
        print("实际采集参数:", report["negotiated"], flush=True)
        write_json(output / "report.json", report)
        started = time.monotonic()
        measure_at = started + job["warmup"]
        end = measure_at + job["seconds"]
        interval = job["seconds"] / job["samples"]
        next_sample = measure_at
        while time.monotonic() < end:
            before = time.monotonic()
            if synthetic:
                frame = np.empty((job["height"], job["width"], 3), dtype=np.uint8)
                for index, color in enumerate(
                    ((30, 60, 220), (40, 220, 60), (220, 60, 40), (90, 180, 240))
                ):
                    frame[
                        index * job["height"] // 4 : (index + 1) * job["height"] // 4
                    ] = color
            else:
                ok, frame = cap.retrieve() if cap.grab() else (False, None)
                if not ok:
                    frame = None
            now = time.monotonic()
            if now >= measure_at:
                if frame is None or not frame.size:
                    report["read_failures"] += 1
                else:
                    metrics = image_metrics(frame)
                    metrics.update(
                        elapsed_s=now - measure_at, read_ms=(now - before) * 1000
                    )
                    report["frames"].append(metrics)
                    if (
                        now >= next_sample
                        and len(report["saved_images"]) < job["samples"]
                    ):
                        filename = f"frame-{len(report['saved_images']) + 1:02d}.png"
                        if not cv2.imwrite(str(output / filename), frame):
                            raise RuntimeError("保存 PNG 失败")
                        report["saved_images"].append(filename)
                        next_sample = now + interval
                        write_json(output / "report.json", report)
            time.sleep(max(0, before + 1 / job["fps"] - time.monotonic()))
        measured = time.monotonic() - measure_at
        report["observed_fps"] = len(report["frames"]) / measured
        if not report["frames"]:
            raise RuntimeError("采集期间没有有效图像")
        report["status"] = "captured"
    except Exception as error:  # noqa: BLE001 - persist failures across the worker boundary
        report["status"] = "error"
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], file=sys.stderr, flush=True)
    finally:
        # Save diagnostics even if the device blocks during release().
        write_json(output / "report.json", report)
        if cap is not None:
            cap.release()
    return 0 if report["status"] == "captured" else 1


def device_info(source, output):
    if platform.system() != "Linux" or source == "synthetic":
        return
    path = f"/dev/video{source}" if isinstance(source, int) else source
    commands = [
        ("v4l2-formats.txt", ["v4l2-ctl", "-d", path, "--list-formats-ext"]),
        ("v4l2-device.txt", ["v4l2-ctl", "-d", path, "--all"]),
    ]
    for filename, command in commands:
        try:
            if not shutil.which(command[0]):
                text = "v4l2-ctl 未安装；跳过设备信息采集。\n"
            else:
                result = subprocess.run(
                    command, check=False, capture_output=True, text=True, timeout=5
                )
                text = result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired) as error:
            text = str(error)
        (output / filename).write_text(text, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "configs/vehicle.single-camera.yaml"
    )
    parser.add_argument(
        "--camera", choices=("front", "back", "left", "right"), default="front"
    )
    parser.add_argument(
        "--source", help="覆盖设备路径/索引；synthetic 可无摄像头验证脚本"
    )
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=float)
    parser.add_argument("--fourcc", choices=("MJPG", "YUYV", "AUTO"), default="MJPG")
    parser.add_argument(
        "--compare",
        action="store_true",
        help="追加 MJPG 低帧率、YUYV 低帧率及不设置 FourCC 的测试",
    )
    parser.add_argument("--seconds", type=float, default=5, help="每组采集秒数，默认 5")
    parser.add_argument("--warmup", type=float, default=1, help="预热秒数，默认 1")
    parser.add_argument(
        "--samples", type=int, default=5, help="每组最多保存的 PNG 数量"
    )
    parser.add_argument(
        "--timeout", type=float, default=20, help="每组硬超时秒数，默认 20"
    )
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/camera-diagnostic"
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return capture(json.loads(args.worker.read_text(encoding="utf-8")))

    from rd_core.configuration import ConfigError, load_config
    from rd_core.settings import VehicleSettings

    try:
        # Read this file only; never resolve authentication tokens or start services.
        settings = load_config(
            VehicleSettings, "vehicle", path=args.config, environ={}
        ).value
        camera = next((c for c in settings.cameras if c.name == args.camera), None)
        if camera is None:
            parser.error(f"配置中没有 {args.camera} 摄像头")
        source = camera.source if args.source is None else args.source
        if source is None:
            parser.error("该视角没有独立输入；请选择实际采集视角或指定 --source")
        if isinstance(source, str) and source.isdecimal():
            source = int(source)
        width = camera.width if args.width is None else args.width
        height = camera.height if args.height is None else args.height
        fps = camera.fps if args.fps is None else args.fps
        if min(width, height) <= 0 or width % 2 or height % 2:
            parser.error("宽高必须为正偶数")
        if not math.isfinite(fps) or not 0 < fps <= 240:
            parser.error("FPS 必须在 (0, 240] 内")
        if not math.isfinite(args.seconds) or not 0 < args.seconds <= 120:
            parser.error("--seconds 必须在 (0, 120] 内")
        if not math.isfinite(args.warmup) or not 0 <= args.warmup <= 30:
            parser.error("--warmup 必须在 [0, 30] 内")
        if not 1 <= args.samples <= 100:
            parser.error("--samples 必须在 [1, 100] 内")
        if (
            not math.isfinite(args.timeout)
            or args.timeout <= args.seconds + args.warmup
        ):
            parser.error("--timeout 必须大于采集与预热时间之和")
    except ConfigError as error:
        parser.error(str(error))

    output = args.output.resolve() / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    output.mkdir(parents=True)
    print(f"输出目录: {output}", flush=True)
    print(
        "请先停止车端释放摄像头。此测试不连接服务器；黑色区域统计不代表已确认坏帧。",
        flush=True,
    )
    device_info(source, output)
    modes = [(args.fourcc, fps)]
    if args.compare:
        modes += [("MJPG", min(fps, 15)), ("YUYV", min(fps, 15)), ("AUTO", fps)]
    summaries = []
    for number, (fourcc, rate) in enumerate(dict.fromkeys(modes), 1):
        folder = output / f"{number:02d}-{fourcc}-{rate:g}fps"
        folder.mkdir()
        job = {
            "source": source,
            "width": width,
            "height": height,
            "fps": rate,
            "fourcc": fourcc,
            "seconds": args.seconds,
            "warmup": args.warmup,
            "samples": args.samples,
            "output": str(folder),
        }
        job_path = folder / "request.json"
        write_json(job_path, job)
        print(f"测试 {fourcc} {width}x{height} @ {rate:g} FPS ...", flush=True)
        with (folder / "capture.log").open("w", encoding="utf-8") as log:
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(job_path),
                    ],
                    check=False,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=args.timeout,
                )
                status = "completed" if result.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                status = "timeout"
        report_path = folder / "report.json"
        report = (
            json.loads(report_path.read_text(encoding="utf-8"))
            if report_path.exists()
            else {}
        )
        report["process_status"] = status
        write_json(report_path, report)
        summary = {
            "directory": folder.name,
            "status": status,
            "negotiated": report.get("negotiated"),
            "observed_fps": report.get("observed_fps"),
            "saved_images": report.get("saved_images", []),
            "mismatches": report.get("mismatches", {}),
            "error": report.get("error"),
        }
        summaries.append(summary)
        write_json(output / "summary.json", summaries)
        print(
            f"  {status}; 保存 {len(summary['saved_images'])} 张；实际参数 {summary['negotiated']}",
            flush=True,
        )
        if summary["mismatches"]:
            print("  注意：设备报告参数与请求不一致，详见 report.json。", flush=True)
        if summary["error"]:
            print(f"  {summary['error']}", flush=True)
    print("检查 PNG：若原图也缺损，优先排查采集/USB；若原图完整，再排查 WebRTC/显示。")
    print(f"报告: {output / 'summary.json'}")
    return 0 if all(s["status"] == "completed" for s in summaries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
