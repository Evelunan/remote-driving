"""After a completed media soak, distinguish reclaimable glibc heap from live data.

This diagnostic runs in its own process. It never trims memory in the application
or during measurement, and the result is not proof that all native memory is free.
"""

import argparse
import ctypes
import gc
import hashlib
import json
import platform
import sys
from pathlib import Path

import soak_media


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cycles", type=int, default=6)
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument(
        "--profile", choices=("small", "production"), default="production"
    )
    parser.add_argument("--soak-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.soak_output.resolve():
        parser.error("the memory and soak reports must have distinct paths")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "schema": "rd-media-heap-diagnostic/1",
        "valid": False,
        "scope": "four-view media soak followed by final Python GC and glibc malloc_trim; no control devices or production allocator changes",
        "diagnostic_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    if platform.libc_ver()[0] != "glibc":
        parser.error("this optional allocator diagnostic requires glibc")
    trim = ctypes.CDLL(None).malloc_trim
    trim.argtypes, trim.restype = [ctypes.c_size_t], ctypes.c_int
    original = sys.argv
    try:
        sys.argv = [
            str(Path(soak_media.__file__)),
            "--cycles",
            str(args.cycles),
            "--seconds",
            str(args.seconds),
            "--profile",
            args.profile,
            "--output",
            str(args.soak_output),
        ]
        soak_media.main()
        raw = args.soak_output.read_bytes()
        if json.loads(raw).get("valid") is not True:
            raise RuntimeError("underlying soak did not pass")
        report["soak_report"] = {
            "file": str(args.soak_output),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        report["after_soak_return"] = soak_media.resources()
        gc.collect()
        report["after_final_gc"] = soak_media.resources()
        report["malloc_trim_return"] = trim(0)
        report["after_trim"] = soak_media.resources()
        report["reclaimed_mib"] = (
            report["after_final_gc"]["rss_mib"] - report["after_trim"]["rss_mib"]
        )
        report["valid"] = True
    except BaseException as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        sys.argv = original
        args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
