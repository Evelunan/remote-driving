"""Install each role into a disposable environment and check from outside the repo."""

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def wheel_inventory(directory, *, source=ROOT):
    expected = {}
    for manifest in [
        *source.glob("apps/*/pyproject.toml"),
        *source.glob("packages/*/pyproject.toml"),
    ]:
        project = tomllib.loads(manifest.read_text())["project"]
        expected[project["name"]] = project["version"]
    wheels, records = {}, {}
    for wheel in sorted(directory.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            (metadata_name,) = [
                n for n in archive.namelist() if n.endswith(".dist-info/METADATA")
            ]
            metadata = BytesParser().parsebytes(archive.read(metadata_name))
            name, version = metadata["Name"], metadata["Version"]
            if name in wheels or expected.get(name) != version:
                raise ValueError(f"unexpected or duplicate wheel: {wheel.name}")
            for member in archive.namelist():
                path = Path(member)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or path.suffix in {".key", ".pem", ".csv"}
                ):
                    raise ValueError(
                        f"unexpected packaged file: {wheel.name}: {member}"
                    )
            wheels[name] = wheel.resolve()
            records[name] = {
                "filename": wheel.name,
                "version": version,
                "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            }
    if set(wheels) != set(expected):
        raise ValueError(f"missing wheels: {sorted(set(expected) - set(wheels))}")
    return wheels, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Never leave an earlier successful report in place when this run fails.
    args.output.write_text(json.dumps({"passed": False, "status": "started"}) + "\n")
    if os.name != "posix" or sys.version_info[:2] != (3, 12):
        parser.error("the current acceptance environment is POSIX with Python 3.12")
    wheels, inventory = wheel_inventory(args.wheels)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("RD_") and k != "PYTHONPATH"
    }
    env["QT_QPA_PLATFORM"] = "offscreen"
    flags = ["--offline"] if args.offline else []
    report = {
        "scope": "isolated wheels; loopback networking, mock controller, synthetic video and Qt offscreen",
        "created_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_dirty": bool(
            subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT)
        ),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "uv": subprocess.check_output(["uv", "--version"], text=True).strip(),
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "wheels": inventory,
        "roles": {},
        "physical_stop_verified": False,
        "passed": False,
    }
    with tempfile.TemporaryDirectory(prefix="rd-wheel-acceptance-") as temporary:
        work = Path(temporary)
        constraints = work / "constraints.txt"
        subprocess.run(
            [
                "uv",
                "export",
                "--all-packages",
                "--locked",
                "--no-dev",
                "--no-emit-workspace",
                "--no-hashes",
                "--output-file",
                str(constraints),
                *flags,
            ],
            cwd=ROOT,
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
            timeout=120,
        )
        # No package import can resolve through the source checkout, including via
        # sys.path[0] of the acceptance scripts themselves.
        for name in (
            "check_installed.py",
            "smoke_vehicle_processes.py",
            "smoke_driving_signals.py",
        ):
            shutil.copy2(ROOT / "scripts" / name, work / name)
        shutil.copytree(
            ROOT / "configs",
            work / "configs",
            ignore=shutil.ignore_patterns("tls", "*.key", "*.pem"),
        )
        for role in ("server", "vehicle", "driving"):
            venv = work / role
            subprocess.run(
                ["uv", "venv", "--python", sys.executable, str(venv), *flags],
                env=env,
                cwd=work,
                check=True,
                timeout=60,
            )
            python = venv / "bin/python"
            names = ["rd-core", "rd-quic", f"rd-{role}"]
            if role != "server":
                names.append("rd-webrtc")
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--no-sources",
                    "--constraint",
                    str(constraints),
                    *(str(wheels[name]) for name in names),
                    *flags,
                ],
                env=env,
                cwd=ROOT,
                check=True,
                timeout=300,
            )
            subprocess.run(
                ["uv", "pip", "check", "--python", str(python)],
                env=env,
                cwd=work,
                check=True,
                timeout=30,
            )
            result = work / f"{role}.json"
            subprocess.run(
                [
                    str(python),
                    str(work / "check_installed.py"),
                    "--role",
                    role,
                    "--wheels",
                    str(args.wheels.resolve()),
                    "--output",
                    str(result),
                ],
                cwd=work,
                env=env,
                check=True,
                timeout=120,
            )
            report["roles"][role] = json.loads(result.read_text())
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            print(f"{role}: isolated wheel acceptance passed", flush=True)
    report["passed"] = True
    args.output.write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
