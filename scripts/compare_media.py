"""Compare repeated media runs only when environment, workload and probes match."""

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path

PROBES = ("scripts/benchmark_media.py", "scripts/media_probe.py")
ENVIRONMENT = (
    "schema",
    "scope",
    "topology",
    "python",
    "platform",
    "cpu_model",
    "cpu_count",
    "qt_platform",
    "versions",
)


def summary(values):
    if any(
        value is not None
        and (type(value) not in (int, float) or not math.isfinite(value))
        for value in values
    ):
        raise ValueError("comparison requires finite, measured values")
    if any(value is None for value in values):
        # A frozen run may have no frame latency samples. Do not silently remove
        # it and calculate a favorable percentile from the remaining runs.
        return {
            "runs": len(values),
            "median": None,
            "min": None,
            "max": None,
            "per_run": values,
        }
    return {
        "runs": len(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def difference(before, after):
    left, right = summary(before), summary(after)
    delta = (
        right["median"] - left["median"]
        if left["median"] is not None and right["median"] is not None
        else None
    )
    return {
        "before": left,
        "after": right,
        "delta": delta,
        "change_percent": delta / left["median"] * 100
        if delta is not None and left["median"]
        else None,
    }


def cpu_per_frame(phase):
    fps = sum(view["rates"]["paint"]["fps"] for view in phase["views"].values())
    return phase["process_cpu_percent_one_core"] * 10 / fps if fps else None


def load_runs(paths):
    records, hashes = [], set()
    for path in paths:
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in hashes:
            raise ValueError("duplicate measurement content is not an independent run")
        hashes.add(digest)
        report = json.loads(content)
        if (
            report.get("schema") != "rd-media-benchmark/1"
            or report.get("valid") is not True
            or report.get("measurement_errors")
        ):
            raise ValueError(f"invalid measurement: {path.name}")
        if not report.get("camera_threads_closed") or not report.get(
            "control_threads_closed"
        ):
            raise ValueError(f"unfinished resource cleanup: {path.name}")
        records.append((path, digest, report))
    if not records:
        raise ValueError("no measurements supplied")
    original = records[0][2]
    for _, _, report in records:
        if any(report.get(key) != original.get(key) for key in ENVIRONMENT):
            raise ValueError("environment changed within measurement group")
        if report["source_sha256"] != original["source_sha256"]:
            raise ValueError("source changed within measurement group")
    return records


def encoder_configuration(report):
    # Independent tracks can initialize in a different order. Counts and all
    # recorded codec/thread settings must still match.
    return Counter(
        json.dumps(item, sort_keys=True) for item in report["encoder_instances"]
    )


def compare(before_paths, after_paths):
    before, after = load_runs(before_paths), load_runs(after_paths)
    old, new = before[0][2], after[0][2]
    if any(old.get(key) != new.get(key) for key in ENVIRONMENT):
        raise ValueError("environment differs between before and after")
    if old["qt_platform"] != "offscreen":
        raise ValueError("this comparison requires the calibrated offscreen pipeline")
    for path in PROBES:
        if not old["source_sha256"].get(path) or old["source_sha256"][path] != new[
            "source_sha256"
        ].get(path):
            raise ValueError("instrumentation changed; obtain comparable baselines")
    if {r["workload"]["views"] for _, _, r in before} != {
        r["workload"]["views"] for _, _, r in after
    }:
        raise ValueError("before and after must cover the same camera counts")
    cases = {}
    for count in sorted({r["workload"]["views"] for _, _, r in before}):
        left = [r for _, _, r in before if r["workload"]["views"] == count]
        right = [r for _, _, r in after if r["workload"]["views"] == count]
        if len(left) < 3 or len(left) != len(right):
            raise ValueError(
                "each camera count requires equal groups of at least three runs"
            )
        if any(r["workload"] != left[0]["workload"] for r in left + right):
            raise ValueError(
                "workload differs; FPS, scene, dimensions or timing changed"
            )
        if any(
            encoder_configuration(r) != encoder_configuration(left[0])
            for r in left + right
        ):
            raise ValueError("codec or encoder resource configuration changed")
        case = {"workload": left[0]["workload"]}
        for phase in ("steady", "recovered"):
            a, b = [r[phase] for r in left], [r[phase] for r in right]
            result = {
                "process_cpu_percent_one_core": difference(
                    [r["process_cpu_percent_one_core"] for r in a],
                    [r["process_cpu_percent_one_core"] for r in b],
                ),
                "samples": {},
                "views": {},
            }
            result["cpu_ms_per_painted_frame"] = difference(
                [cpu_per_frame(r) for r in a],
                [cpu_per_frame(r) for r in b],
            )
            if any(set(r["samples"]) != set(a[0]["samples"]) for r in a + b):
                raise ValueError("sample metric coverage changed")
            for sample in a[0]["samples"]:
                result["samples"][sample] = {
                    q: difference(
                        [r["samples"][sample][q] for r in a],
                        [r["samples"][sample][q] for r in b],
                    )
                    for q in ("p50", "p95", "p99", "max")
                }
            if any(set(r["views"]) != set(a[0]["views"]) for r in a + b):
                raise ValueError("view coverage changed")
            for view in a[0]["views"]:
                x, y = [r["views"][view] for r in a], [r["views"][view] for r in b]
                result["views"][view] = {
                    "fps": {
                        stage: difference(
                            [r["rates"][stage]["fps"] for r in x],
                            [r["rates"][stage]["fps"] for r in y],
                        )
                        for stage in x[0]["rates"]
                    },
                    "latency_ms": {
                        stage: {
                            q: difference(
                                [r["latency_ms"][stage][q] for r in x],
                                [r["latency_ms"][stage][q] for r in y],
                            )
                            for q in ("p50", "p95", "p99", "max")
                        }
                        for stage in x[0]["latency_ms"]
                    },
                    "gaps_over_500ms": difference(
                        [r["gaps_over_500ms"] for r in x],
                        [r["gaps_over_500ms"] for r in y],
                    ),
                }
            case[phase] = result
        for metric in (
            "reconnect_all_views_painted_ms",
            "reconnect_new_control_epoch_ms",
        ):
            case[metric] = difference(
                [r[metric] for r in left], [r[metric] for r in right]
            )
        cases[str(count)] = case
    changed = sorted(
        path
        for path in old["source_sha256"].keys() | new["source_sha256"].keys()
        if old["source_sha256"].get(path) != new["source_sha256"].get(path)
    )
    return {
        "comparable": True,
        "aggregation": "differences of median per-run statistics; frame percentiles are not pooled",
        "change_sign": "negative means a lower value, not automatically an improvement",
        "scope": old["scope"],
        "changed_sources": changed,
        "inputs": {
            side: [
                {
                    "file": str(path),
                    "sha256": digest,
                    "source_commit": r["git_commit"],
                    "source_dirty": r["git_dirty"],
                }
                for path, digest, r in records
            ]
            for side, records in (("before", before), ("after", after))
        },
        "cases": cases,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", nargs="+", type=Path, required=True)
    parser.add_argument("--after", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"comparable": false, "status": "started"}\n')
    report = compare(args.before, args.after)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        f"Comparable groups: {', '.join(report['cases'])} views; {len(report['changed_sources'])} changed source files"
    )


if __name__ == "__main__":
    main()
