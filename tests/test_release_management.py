"""A failed or concurrent release selection must leave the old version usable."""

import importlib.util
import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_release.py"
spec = importlib.util.spec_from_file_location("release_management", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
OLD_PROTOCOL = {"control_alpn": "rd-control/2", "control_revision": "direct-control/4"}
NEW_PROTOCOL = {"control_alpn": "rd-control/2", "control_revision": "direct-control/5"}


def write_wire(path, protocol):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"ALPN = {protocol['control_alpn']!r}\n"
        f"CONTROL_REVISION = {protocol['control_revision']!r}\n"
    )


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.delenv("RD_CONFIG", raising=False)
    root = tmp_path / "installation"
    (root / "releases").mkdir(parents=True)
    (root / ".release.lock").touch()
    for name, protocol in (("old", OLD_PROTOCOL), ("new", NEW_PROTOCOL)):
        directory = root / "releases" / name
        venv.EnvBuilder(symlinks=True).create(directory / "venv")
        python_version = f"python{sys.version_info.major}.{sys.version_info.minor}"
        write_wire(
            directory / "venv/lib" / python_version / "site-packages/rd_core/wire.py",
            protocol,
        )
        binary = directory / "venv/bin/rd-server"
        binary.write_text(
            "#!/bin/sh\n"
            f'case "$2" in */releases/{name}/config.json) ;; *) exit 3 ;; esac\n'
            'if [ "$3" = --check-config ]; then exit 0; fi\n'
            "touch started\nexec sleep 60\n"
        )
        binary.chmod(0o755)
        (directory / "config.json").write_text(json.dumps({"version": name}))
        manifest = {
            "ready": True,
            "schema": 1,
            "role": "server",
            "release_id": name,
            "directory": str(directory),
            "compatibility": protocol,
            "external_tls": {},
            "files": release.file_inventory(directory),
            "interpreter": {
                "executable": sys.executable,
                "sha256": release.digest(Path(sys.executable)),
            },
        }
        release.write_json(directory / "manifest.json", manifest)
    release.activate(root, "old", None)
    return root


def test_upgrade_and_explicit_rollback_pair_configuration_with_executable(installation):
    root = installation
    assert release.activate(root, "new", "old")["previous"] == "old"
    assert release.verify(root, "new")["compatibility"] == NEW_PROTOCOL
    assert json.loads((root / "current/config.json").read_text())["version"] == "new"
    assert release.activate(root, "old", "new")["current"] == "old"
    assert release.verify(root, "old")["compatibility"] == OLD_PROTOCOL
    assert json.loads((root / "current/config.json").read_text())["version"] == "old"
    with pytest.raises(ValueError, match="current release changed"):
        release.activate(root, "new", "new")
    assert release.current_id(root) == "old"


@pytest.mark.parametrize(
    "fault",
    [
        "incomplete",
        "protocol",
        "mislabeled",
        "file",
        "added",
        "role",
        "tls",
        "interpreter",
        "legacy",
    ],
)
def test_invalid_candidate_does_not_change_current(installation, fault):
    root = installation
    directory = root / "releases/new"
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if fault == "incomplete":
        manifest["ready"] = False
    elif fault == "protocol":
        manifest["compatibility"]["control_revision"] = "direct-control/6"
    elif fault == "mislabeled":
        # The manifest names an accepted protocol that the installed wheel lacks.
        manifest["compatibility"] = OLD_PROTOCOL
    elif fault == "role":
        manifest["role"] = "vehicle"
    elif fault == "file":
        (directory / "config.json").write_text("corrupt")
    elif fault == "added":
        (directory / "venv/unreviewed.py").touch()
    elif fault == "tls":
        manifest["external_tls"] = {str(root / ".release.lock"): "old certificate"}
    elif fault == "interpreter":
        manifest["interpreter"]["sha256"] = "different interpreter"
    elif fault == "legacy":
        (directory / "config.json").write_text('{"allow_legacy_development": true}')
        manifest["files"] = release.file_inventory(directory)
    release.write_json(manifest_path, manifest)
    with pytest.raises((ValueError, FileNotFoundError)):
        release.activate(root, "new", "old")
    assert release.current_id(root) == "old"


@pytest.mark.parametrize(
    ("source_protocol", "installed"),
    [
        (OLD_PROTOCOL, OLD_PROTOCOL),
        (NEW_PROTOCOL, NEW_PROTOCOL),
        (NEW_PROTOCOL, OLD_PROTOCOL),
    ],
)
def test_prepare_requires_the_source_protocol_even_for_same_version_wheels(
    tmp_path, monkeypatch, source_protocol, installed
):
    source = tmp_path / "source"
    write_wire(source / "packages/rd-core/src/rd_core/wire.py", source_protocol)
    lock = source / "uv.lock"
    lock.write_text("lockfile")
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    wheels, inventory = {}, {}
    for name in ("rd-core", "rd-quic", "rd-server"):
        wheel = wheel_dir / f"{name}-0.2.0.whl"
        wheel.write_text(name)
        wheels[name] = wheel
        inventory[name] = {
            "filename": wheel.name,
            "version": "0.2.0",
            "sha256": release.digest(wheel),
        }
    verify_wheels = ModuleType("verify_wheels")
    verify_wheels.wheel_inventory = lambda directory, source: (wheels, inventory)
    monkeypatch.setitem(sys.modules, "verify_wheels", verify_wheels)
    acceptance = tmp_path / "acceptance.json"
    release.write_json(
        acceptance,
        {
            "passed": True,
            "wheels": inventory,
            "lock_sha256": release.digest(lock),
            "roles": dict.fromkeys(release.ROLES),
        },
    )
    config = tmp_path / "config.json"
    config.write_text('{"services": []}')

    def mock_command(argv, **kwargs):
        if argv[0] == "uv":
            return ""
        if "sys._base_executable" in argv[-1]:
            return json.dumps({"version": "3.10.12", "executable": sys.executable})
        if argv[-1] == "--dump-config":
            return json.dumps({"config": json.loads(config.read_text())})
        assert argv[-1] == "--check-config"
        return ""

    monkeypatch.setattr(release, "command", mock_command)
    monkeypatch.setattr(
        release, "installed_compatibility", lambda python, **kwargs: installed
    )
    args = SimpleNamespace(
        source=source,
        wheels=wheel_dir,
        acceptance=acceptance,
        config=config,
        root=tmp_path / "installation",
        release="candidate",
        role="server",
        python=sys.executable,
        offline=True,
    )
    manifest_path = args.root / "releases/candidate/manifest.json"
    if installed == source_protocol:
        release.prepare(args)
        manifest = json.loads(manifest_path.read_text())
        assert manifest["ready"] and manifest["compatibility"] == source_protocol
    else:
        with pytest.raises(
            ValueError, match="installed protocol does not match release source"
        ):
            release.prepare(args)
        assert json.loads(manifest_path.read_text())["ready"] is False


def test_run_pins_old_version_and_blocks_selection_and_duplicate_start(installation):
    root = installation
    release.activate(root, "new", "old")
    release.activate(root, "old", "new")
    process = subprocess.Popen(
        [sys.executable, SCRIPT, "run", "--root", root], cwd=root
    )
    try:
        deadline = time.monotonic() + 5
        while not (root / "started").exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.01)
        with pytest.raises(ValueError, match="still running"):
            release.activate(root, "new", "old")
        duplicate = subprocess.run(
            [sys.executable, SCRIPT, "run", "--root", root],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
        assert duplicate.returncode == 1 and "still running" in duplicate.stderr
        assert release.current_id(root) == "old"
    finally:
        process.terminate()
        process.wait(timeout=5)
    assert release.activate(root, "new", "old")["current"] == "new"


def test_paths_and_shell_overrides_cannot_select_unpaired_configuration(
    installation, monkeypatch
):
    for name in ("../escape", ".", "/absolute", "a/b", ""):
        with pytest.raises(ValueError):
            release.release_path(installation, name)
    (installation / "releases/link").symlink_to("old", target_is_directory=True)
    with pytest.raises(ValueError):
        release.release_path(installation, "link")
    monkeypatch.setenv("RD_SERVER__RELAY__PORT", "9999")
    with pytest.raises(ValueError, match="overrides"):
        release.verify(installation, "old")
    assert release.current_id(installation) == "old"


def test_read_only_deployment_lock_can_be_held_by_runner(installation):
    lock = installation / ".release.lock"
    lock.chmod(0o444)
    with (
        release.locked(installation, exclusive=True),
        pytest.raises(ValueError, match="still running"),
        release.locked(installation, exclusive=True),
    ):
        pass
    assert not os.stat(lock).st_mode & 0o222
