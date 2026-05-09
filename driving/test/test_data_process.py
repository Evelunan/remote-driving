"""
数据处理脚本：分析 fps_stats.csv 和 rtt_stats.csv
- fps_stats.csv：每4行一组，分别对应 front/back/left/right 摄像头，第二列为帧率
- rtt_stats.csv：第二列为 RTT（ms），统计平均/最小/最大/P95/P99
"""

import csv
import numpy as np
from pathlib import Path


DATA_DIR = Path(__file__).parent
FPS_FILE = DATA_DIR / "fps_stats.csv"
RTT_FILE = DATA_DIR / "rtt_stats.csv"

CAMERA_LABELS = ["front", "back", "left", "right"]


# ─────────────────────────────────────────────
#  读取 CSV 第二列（去除0值）
# ─────────────────────────────────────────────

def read_second_column(filepath: Path) -> list[float]:
    """读取 CSV 文件第二列的所有非零值"""
    values = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                continue
            try:
                val = float(row[1])
                if val != 0:
                    values.append(val)
            except ValueError:
                continue
    return values


# ─────────────────────────────────────────────
#  FPS 统计
# ─────────────────────────────────────────────

def process_fps(filepath: Path) -> None:
    """
    按 front/back/left/right 分组（每4行一组），去除0值后统计：
    - 平均帧率
    - 最低帧率
    """
    # 先读原始数据（保留行序，不过滤0，便于分组后再过滤）
    raw_values: list[float | None] = []
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 2:
                raw_values.append(None)
                continue
            try:
                raw_values.append(float(row[1]))
            except ValueError:
                raw_values.append(None)

    # 按摄像头分组：行索引 % 4 决定摄像头
    camera_data: dict[str, list[float]] = {label: [] for label in CAMERA_LABELS}
    for idx, val in enumerate(raw_values):
        if val is None or val == 0:
            continue
        label = CAMERA_LABELS[idx % 4]
        camera_data[label].append(val)

    print("=" * 50)
    print("[FPS] FPS 统计（去除0值后）")
    print("=" * 50)
    for label in CAMERA_LABELS:
        data = camera_data[label]
        if not data:
            print(f"  [{label:>5}]  无有效数据")
            continue
        arr = np.array(data)
        print(f"  [{label:>5}]  样本数={len(arr):>5d}  "
              f"平均帧率={arr.mean():.2f} fps  "
              f"最低帧率={arr.min():.0f} fps")
    print()


# ─────────────────────────────────────────────
#  RTT 统计
# ─────────────────────────────────────────────

def process_rtt(filepath: Path) -> None:
    """
    去除0值后统计 RTT（ms）：
    - 平均 RTT
    - 最小 RTT
    - 最大 RTT
    - P95 RTT
    - P99 RTT
    """
    data = read_second_column(filepath)
    if not data:
        print("RTT 文件中无有效数据")
        return

    arr = np.array(data)

    print("=" * 50)
    print("[RTT] RTT 统计（去除0值后）")
    print("=" * 50)
    print(f"  样本数  : {len(arr)}")
    print(f"  平均RTT : {arr.mean():.2f} ms")
    print(f"  最小RTT : {arr.min():.2f} ms")
    print(f"  最大RTT : {arr.max():.2f} ms")
    print(f"  P95 RTT : {np.percentile(arr, 95):.2f} ms")
    print(f"  P99 RTT : {np.percentile(arr, 99):.2f} ms")
    print()


# ─────────────────────────────────────────────
#  入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    process_fps(FPS_FILE)
    process_rtt(RTT_FILE)
