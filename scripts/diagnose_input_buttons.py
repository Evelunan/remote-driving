"""记录原始 USB 报文和驾驶端匹配的按键，便于核对 input_config.py。"""

import argparse
import asyncio
import csv
import sys
import threading
from datetime import datetime, timezone
from itertools import zip_longest
from pathlib import Path

import usb1
from rd_core.configuration import load_config
from rd_core.settings import DrivingSettings
from rd_driving.infrastructure.communication import LatestValueQueue
from rd_driving.infrastructure.device import USBConnector
from rd_driving.infrastructure.processing import AnalogCalculator, DataProcessor


def report_changes(previous, current):
    if previous is None:
        return "首帧"
    changes = []
    for index, (before, after) in enumerate(zip_longest(previous, current)):
        if before != after:
            old = "--" if before is None else f"{before:02X}"
            new = "--" if after is None else f"{after:02X}"
            changes.append(f"data[{index}] {old}->{new}")
    return "  ".join(changes)


def read_labels(loop, events):
    for line in sys.stdin:
        label = line.strip()
        if label:
            try:
                loop.call_soon_threadsafe(events.put_nowait, label)
            except RuntimeError:  # The monitor has already exited.
                return


async def monitor(config, once, output, label):
    usb = load_config(DrivingSettings, "driving", path=config).value.usb
    device = USBConnector(usb.vendor_id, usb.product_id)
    events = asyncio.Queue()
    device.set_data_callback(events.put_nowait)
    processor = DataProcessor(LatestValueQueue(), AnalogCalculator())
    previous = None
    previous_pressed = None
    new_label = False
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        await device.connect()
        with output.open("x", encoding="utf-8-sig", newline="") as log:
            writer = csv.writer(log)
            writer.writerow(["采样时间", "测试标签", "USB VID:PID", "完整报文(hex)", "字节变化", "匹配按键"])
            log.flush()
            print(f"日志文件 {output.resolve()}", flush=True)
            if not once and sys.stdin.isatty():
                print("输入要测的键名并按回车，再操作控制器。换键时直接输入下一个名字。Ctrl+C 结束。", flush=True)
                threading.Thread(
                    target=read_labels, args=(asyncio.get_running_loop(), events), daemon=True
                ).start()
            if label:
                print(f"当前测试：{label}", flush=True)
            device_id = f"{usb.vendor_id:04X}:{usb.product_id:04X}"
            while True:
                event = await asyncio.wait_for(events.get(), timeout=5)
                if isinstance(event, str):
                    label = event
                    new_label = True
                    print(f"当前测试：{label}", flush=True)
                    continue
                data = event.data
                if data is None:
                    raise ConnectionError("控制器已断开")
                if data == previous and not new_label:
                    continue
                captured = datetime.fromtimestamp(event.timestamp_ms / 1000, timezone.utc)
                timestamp = captured.astimezone().isoformat(timespec="milliseconds")
                state = processor._process_digital(data)
                pressed = ", ".join(key.name for key, active in state.items() if active) or "无"
                changes = report_changes(previous, data) or "状态未变"
                raw = data.hex(" ")
                writer.writerow([timestamp, label, device_id, raw, changes, pressed])
                log.flush()
                if new_label or pressed != previous_pressed:
                    print(f"匹配 = {pressed}", flush=True)
                previous_pressed = pressed
                new_label = False
                if once:
                    return
                previous = data
    finally:
        await device.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/driving.yaml"))
    parser.add_argument("--once", action="store_true", help="记录首帧后退出")
    parser.add_argument("--label", default="", help="开始时的测试标签，运行中可输入新标签并回车")
    parser.add_argument("--output", type=Path, help="新建 CSV 日志的路径，默认在 artifacts/input-buttons/ 下按时间命名")
    args = parser.parse_args()
    output = args.output or Path("artifacts/input-buttons") / f"{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
    try:
        asyncio.run(monitor(args.config, args.once, output, args.label))
    except KeyboardInterrupt:
        pass
    except asyncio.TimeoutError:
        parser.exit(1, "连续 5 秒未收到 USB 输入报文。\n")
    except (usb1.USBError, ConnectionError) as error:
        parser.exit(1, f"USB 读取失败：{error}\n")
    except FileExistsError:
        parser.exit(1, f"日志已存在，请选择新的文件名：{output}\n")
