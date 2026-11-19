"""Prepare, check and select versioned installations; never start system services.

Linux/Python 3.10.12. Run prepare from the reviewed checkout. Other commands need
only this standard-library script and an already prepared installation root.
"""

import argparse
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ROLES = ("server", "vehicle", "driving")
COMPATIBILITY = {"control_alpn": "rd-control/2", "control_revision": "video-bound/1"}


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def release_path(root, release_id):
    if release_id == "none" or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", release_id
    ):
        raise ValueError("invalid release id")
    path = root / "releases" / release_id
    if path.is_symlink() or path.parent.resolve() != root / "releases":
        raise ValueError("release directory must not be a symlink")
    return path


def environment():
    # Instance settings must come from the paired snapshot, not the deployer's
    # forgotten shell overrides. Credential and ROS environment remain external.
    if "RD_CONFIG" in os.environ or any(
        key.startswith(tuple("RD_" + role.upper() + "__" for role in ROLES))
        for key in os.environ
    ):
        raise ValueError("unset RD_CONFIG and role configuration overrides")
    return {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}


def command(argv, *, cwd, env, timeout=120):
    result = subprocess.run(
        [str(item) for item in argv],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        # Do not accidentally include a credential or authenticated package URL
        # in a subprocess error. Inspect the command separately when diagnosing.
        step = " ".join(map(str, argv[:3])) if argv[0] == "uv" else Path(argv[0]).name
        raise ValueError(f"{step} failed (exit {result.returncode})")
    return result.stdout


def file_inventory(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if relative == Path("manifest.json") or "__pycache__" in relative.parts:
            continue
        if path.is_symlink():
            result[str(relative)] = {"symlink": os.readlink(path)}
        elif path.is_file():
            result[str(relative)] = {"sha256": digest(path)}
    return result


def tls_files(config, role):
    if role == "server":
        return {
            value
            for name in config["services"]
            for value in config[name]["tls"].values()
            if value
        }
    components = (
        {"control", "media"} if role == "driving" else set(config["components"])
    )
    if role == "vehicle" and config["controller"] == "none":
        components.discard("control")
    return {
        config["network"][key]
        for component, key in (
            ("control", "quic_ca_file"),
            ("media", "signaling_ca_file"),
        )
        if component in components and config["network"][key]
    }


def check_protocol_config(config):
    if config.get("allow_legacy_development"):
        raise ValueError(
            "managed releases require authenticated v2; legacy mode is excluded"
        )


def current_id(root):
    current = root / "current"
    if not current.is_symlink():
        if current.exists():
            raise ValueError("current must be a managed symlink")
        return None
    target = os.readlink(current)
    release_id = Path(target).name
    if target != "releases/" + release_id:
        raise ValueError("current points outside managed releases")
    release_path(root, release_id)
    return release_id


@contextmanager
def locked(root, *, exclusive):
    # Read-only fd lets the service account hold a lock on a root-owned
    # deployment tree. Keep this descriptor open across exec for run's lifetime.
    with (root / ".release.lock").open("rb") as stream:
        try:
            fcntl.flock(
                stream, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
            )
        except BlockingIOError:
            raise ValueError(
                "application or another activation is still running"
            ) from None
        yield stream


def verify(root, release_id, *, check_config=True):
    directory = release_path(root, release_id)
    manifest = json.loads((directory / "manifest.json").read_text())
    if (
        manifest.get("ready") is not True
        or manifest.get("schema") != 1
        or manifest.get("compatibility") != COMPATIBILITY
        or manifest.get("role") not in ROLES
        or manifest.get("release_id") != release_id
        or manifest.get("directory") != str(directory)
    ):
        raise ValueError("release is incomplete, relocated or protocol-incompatible")
    if file_inventory(directory) != manifest["files"]:
        raise ValueError("release files changed after preparation")
    check_protocol_config(json.loads((directory / "config.json").read_text()))
    for name, expected in manifest["external_tls"].items():
        if digest(Path(name)) != expected:
            raise ValueError("external TLS file changed; prepare a new release")
    if (
        digest(Path(manifest["interpreter"]["executable"]))
        != manifest["interpreter"]["sha256"]
    ):
        raise ValueError("base Python interpreter changed")
    if check_config:
        command(
            [
                directory / "venv/bin" / ("rd-" + manifest["role"]),
                "--config",
                directory / "config.json",
                "--check-config",
            ],
            cwd=root,
            env=environment(),
        )
    return manifest


def prepare(args):
    from verify_wheels import wheel_inventory

    env = environment()
    source = args.source.resolve()
    wheels, inventory = wheel_inventory(args.wheels, source=source)
    acceptance = json.loads(args.acceptance.read_text())
    lock_hash = digest(source / "uv.lock")
    if (
        acceptance.get("passed") is not True
        or acceptance.get("wheels") != inventory
        or acceptance.get("lock_sha256") != lock_hash
        or set(acceptance.get("roles", {})) != set(ROLES)
    ):
        raise ValueError("acceptance must pass for exactly these wheels and lockfile")
    root = args.root.resolve()
    (root / "releases").mkdir(parents=True, exist_ok=True)
    (root / ".release.lock").touch(exist_ok=True)
    directory = release_path(root, args.release)
    directory.mkdir()  # Never overwrite even an incomplete installation.
    manifest = {"ready": False, "schema": 1, "release_id": args.release}
    write_json(directory / "manifest.json", manifest)
    flags = ["--offline"] if args.offline else []
    requirements = command(
        [
            "uv",
            "export",
            "--package",
            "rd-" + args.role,
            "--locked",
            "--no-dev",
            "--no-emit-workspace",
            "--no-header",
            "--no-annotate",
            "--emit-index-url",
            *flags,
        ],
        cwd=source,
        env=env,
    )
    if any(
        urlsplit(word).username is not None
        for word in requirements.split()
        if word.startswith(("http://", "https://"))
    ):
        raise ValueError("do not embed authenticated package URLs in release files")
    (directory / "wheels").mkdir()
    names = {"rd-core", "rd-quic", "rd-" + args.role}
    if args.role != "server":
        names.add("rd-webrtc")
    for name in sorted(names):
        destination = directory / "wheels" / wheels[name].name
        shutil.copyfile(wheels[name], destination)
        requirements += (
            f"\n{destination.as_uri()} --hash=sha256:{inventory[name]['sha256']}\n"
        )
    (directory / "requirements.txt").write_text(requirements)
    shutil.copyfile(source / "uv.lock", directory / "uv.lock")
    shutil.copyfile(args.acceptance, directory / "acceptance.json")
    command(
        ["uv", "venv", "--python", args.python, directory / "venv", *flags],
        cwd=root,
        env=env,
    )
    python = directory / "venv/bin/python"
    interpreter = json.loads(
        command(
            [
                python,
                "-c",
                "import json,platform,sys; print(json.dumps({'version':platform.python_version(), 'executable':sys._base_executable}))",
            ],
            cwd=root,
            env=env,
        )
    )
    version = tuple(map(int, interpreter["version"].split(".")))
    if not (3, 10, 12) <= version < (3, 11):
        raise ValueError("release environment requires Python 3.10.12 or later in 3.10.x")
    interpreter["sha256"] = digest(Path(interpreter["executable"]))
    command(
        [
            "uv",
            "pip",
            "sync",
            "--python",
            python,
            "--require-hashes",
            "--no-build",
            "--link-mode",
            "copy",
            "--no-config",
            directory / "requirements.txt",
            *flags,
        ],
        cwd=root,
        env=env,
        timeout=600,
    )
    command(["uv", "pip", "check", "--python", python], cwd=root, env=env)
    compatibility = json.loads(
        command(
            [
                python,
                "-c",
                "import json; from rd_core.wire import ALPN, CONTROL_REVISION; print(json.dumps({'control_alpn':ALPN,'control_revision':CONTROL_REVISION}))",
            ],
            cwd=root,
            env=env,
        )
    )
    if compatibility != COMPATIBILITY:
        raise ValueError("installed protocol is incompatible")
    binary = directory / "venv/bin" / ("rd-" + args.role)
    effective = json.loads(
        command(
            [binary, "--config", args.config.resolve(), "--dump-config"],
            cwd=root,
            env=env,
        )
    )["config"]
    check_protocol_config(effective)
    log_directory = effective.get("logging", {}).get("directory")
    if log_directory and Path(log_directory).is_relative_to(root / "releases"):
        raise ValueError("logging directory must be outside the release")
    write_json(directory / "config.json", effective)
    command(
        [binary, "--config", directory / "config.json", "--check-config"],
        cwd=root,
        env=env,
    )
    manifest.update(
        {
            "schema": 1,
            "role": args.role,
            "ready": True,
            "directory": str(directory),
            "compatibility": COMPATIBILITY,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(),
            "interpreter": interpreter,
            "wheels": {name: inventory[name] for name in sorted(names)},
            "lock_sha256": lock_hash,
            "external_tls": {
                name: digest(Path(name))
                for name in sorted(tls_files(effective, args.role))
            },
            "files": file_inventory(directory),
        }
    )
    write_json(directory / "manifest.json", manifest)
    return {"prepared": args.release, "role": args.role, "current": current_id(root)}


def activate(root, release_id, expected):
    with locked(root, exclusive=True):
        before = current_id(root)
        if before != expected:
            raise ValueError("current release changed; check the expected release id")
        manifest = verify(root, release_id)
        if before is not None:
            previous = json.loads(
                (release_path(root, before) / "manifest.json").read_text()
            )
            if previous.get("role") != manifest["role"]:
                raise ValueError(
                    "cannot change the application role of an installation"
                )
        temporary = root / (".current-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to("releases/" + release_id)
            os.replace(temporary, root / "current")
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
        return {"previous": before, "current": release_id, "role": manifest["role"]}


def run(root):
    # One application per installation, also preventing duplicate vehicle hosts.
    with locked(root, exclusive=True) as lock:
        release_id = current_id(root)
        if release_id is None:
            raise ValueError("no active release")
        manifest = verify(root, release_id, check_config=False)
        directory = release_path(root, release_id)
        binary = directory / "venv/bin" / ("rd-" + manifest["role"])
        runtime = os.environ.get("RD_RUNTIME_DIR")
        if runtime and Path(runtime).resolve().is_relative_to(root / "releases"):
            raise ValueError("runtime directory must be outside releases")
        os.set_inheritable(lock.fileno(), True)
        os.chdir(root)
        # Resolve the release once: executable and configuration cannot refer to
        # different versions if current changes outside this tool.
        os.execve(
            binary,
            [str(binary), "--config", str(directory / "config.json")],
            environment(),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "check", "activate", "run"):
        sub = commands.add_parser(action)
        sub.add_argument("--root", type=Path, required=True)
        if action != "run":
            sub.add_argument("--release", required=True)
        if action == "prepare":
            sub.add_argument(
                "--source",
                type=Path,
                default=ROOT,
                help="matching reviewed checkout or source archive",
            )
            sub.add_argument("--role", choices=ROLES, required=True)
            for name in ("wheels", "acceptance", "config"):
                sub.add_argument("--" + name, type=Path, required=True)
            sub.add_argument("--python", default=sys.executable)
            sub.add_argument("--offline", action="store_true")
        elif action == "activate":
            sub.add_argument(
                "--expect-current",
                required=True,
                help="id, or 'none' for first activation",
            )
    args = parser.parse_args()
    try:
        root = args.root.resolve()
        if args.action == "prepare":
            result = prepare(args)
        elif args.action == "check":
            manifest = verify(root, args.release)
            result = {"checked": args.release, "role": manifest["role"]}
        elif args.action == "activate":
            result = activate(
                root,
                args.release,
                None if args.expect_current == "none" else args.expect_current,
            )
        else:
            run(root)
            return
        print(json.dumps(result))
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(
            f"release operation failed: {type(exc).__name__}: "
            + (
                str(exc)
                if isinstance(exc, ValueError)
                else "check files, environment and permissions"
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
