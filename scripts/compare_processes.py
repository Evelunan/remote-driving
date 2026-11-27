"""Compare repeated combined/split process runs with identical code and input."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from compare_media import compare_control_recovery, difference

CONDITIONS = (
    "schema",
    "scope",
    "resource_scope",
    "python",
    "platform",
    "cpu_model",
    "cpu_count",
    "qt_platform",
    "versions",
    "workload",
    "source_sha256",
)


def load(paths, topology):
    if len(paths) < 3:
        raise ValueError("at least three runs per topology required")
    records, hashes = [], set()
    for path in paths:
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        if digest in hashes:
            raise ValueError("duplicate measurement content")
        hashes.add(digest)
        value = json.loads(content)
        if (
            value.get("schema") != "rd-process-benchmark/1"
            or value.get("valid") is not True
            or value.get("measurement_errors")
            or value.get("topology") != topology
        ):
            raise ValueError("invalid measurement or wrong topology")
        if value.get("host_exitcode") != 0 or value.get("forced_cleanup") is not False:
            raise ValueError("failed host cleanup")
        roles = {"combined"} if topology == "combined" else {"control", "media"}
        if {item["role"] for item in value["component_cleanup"]} != roles:
            raise ValueError("missing component cleanup")
        if any(
            item["forbidden_control_imports"]
            or item["remaining_threads"]
            or not item["camera_threads_closed"]
            for item in value["component_cleanup"]
        ):
            raise ValueError("failed component cleanup/isolation")
        outcomes = value["child_outcomes"]
        if len(outcomes) != (2 if topology == "split" else 0) or any(
            item["exitcode"] != 0 or item["forced"] for item in outcomes
        ):
            raise ValueError("failed child cleanup")
        records.append(
            (
                value,
                {
                    "file": str(path),
                    "sha256": digest,
                    "source_commit": value["git_commit"],
                    "source_dirty": value["git_dirty"],
                },
            )
        )
    return records


def compare(combined, split):
    before, after = load(combined, "combined"), load(split, "split")
    reference = before[0][0]
    encoders = Counter(
        json.dumps(item, sort_keys=True) for item in reference["encoder_instances"]
    )
    for value, _ in before + after:
        for key in CONDITIONS:
            if value[key] != reference[key]:
                raise ValueError(f"changed comparison condition: {key}")
        if (
            Counter(
                json.dumps(item, sort_keys=True) for item in value["encoder_instances"]
            )
            != encoders
        ):
            raise ValueError("changed encoder configuration")
    a, b = [v for v, _ in before], [v for v, _ in after]
    result = {
        "comparable": True,
        "comparison_kind": "identical vehicle component code/input; combined host versus production split supervisor",
        "aggregation": "median of per-run statistics; percentiles are not pooled; negative is lower, not automatically better",
        "scope": reference["scope"],
        "resource_scope": reference["resource_scope"],
        "workload": reference["workload"],
        "inputs": {
            "combined": [info for _, info in before],
            "split": [info for _, info in after],
        },
    }
    for phase in ("steady", "recovered"):
        left, right = [value[phase] for value in a], [value[phase] for value in b]
        section = {
            "resources": {
                "cpu_percent_one_core": difference(
                    [v["resources"]["total"]["cpu_percent_one_core"] for v in left],
                    [v["resources"]["total"]["cpu_percent_one_core"] for v in right],
                ),
                **{
                    name: {
                        q: difference(
                            [v["resources"]["total"][name][q] for v in left],
                            [v["resources"]["total"][name][q] for v in right],
                        )
                        for q in ("p50", "p95", "max")
                    }
                    for name in (
                        "rss_mib",
                        "pss_mib",
                        "native_threads",
                        "file_descriptors",
                    )
                },
            },
            "samples": {},
            "views": {},
        }
        for key in (
            "control_sample_to_ack_ms",
            "control_sample_to_mock_submit_ms",
            "harness_loop_lag_ms",
            "gui_timer_lag_ms",
            "vehicle_control_loop_lag_ms",
        ):
            source = (
                "vehicle_combined_loop_lag_ms"
                if key == "vehicle_control_loop_lag_ms"
                else key
            )
            section["samples"][key] = {
                q: difference(
                    [v["samples"][source][q] for v in left],
                    [v["samples"][key][q] for v in right],
                )
                for q in ("p50", "p95", "p99", "max")
            }
        for view in reference[phase]["views"]:
            x, y = [v["views"][view] for v in left], [v["views"][view] for v in right]
            section["views"][view] = {
                "paint_fps": difference(
                    [v["rates"]["paint"]["fps"] for v in x],
                    [v["rates"]["paint"]["fps"] for v in y],
                ),
                "capture_to_qt_paint_ms": {
                    q: difference(
                        [v["latency_ms"]["capture_to_qt_paint"][q] for v in x],
                        [v["latency_ms"]["capture_to_qt_paint"][q] for v in y],
                    )
                    for q in ("p50", "p95", "p99", "max")
                },
                "gaps_over_500ms": difference(
                    [v["gaps_over_500ms"] for v in x], [v["gaps_over_500ms"] for v in y]
                ),
            }
        result[phase] = section
    for key in (
        "startup_ready_ms",
        "shutdown_ms",
        "reconnect_all_views_painted_ms",
    ):
        result[key] = difference([v[key] for v in a], [v[key] for v in b])
    result.update(compare_control_recovery(a, b))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined", type=Path, nargs="+", required=True)
    parser.add_argument("--split", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text('{"comparable": false, "status": "started"}\n')
    report = compare(args.combined, args.split)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(f"Comparable: {len(args.combined)} combined / {len(args.split)} split runs")


if __name__ == "__main__":
    main()
