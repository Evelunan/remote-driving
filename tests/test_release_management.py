"""A failed or concurrent release selection must leave the old version usable."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/manage_release.py"
spec = importlib.util.spec_from_file_location("release_management", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.delenv("RD_CONFIG", raising=False)
    root = tmp_path / "installation"
    (root / "releases").mkdir(parents=True)
    (root / ".release.lock").touch()
    for name in ("old", "new"):
        directory = root / "releases" / name
        (directory / "venv/bin").mkdir(parents=True)
        binary = directory / "venv/bin/rd-server"
        binary.write_text(
            '#!/bin/sh\nif [ "$3" = --check-config ]; then exit 0; fi\n'
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
            "compatibility": release.COMPATIBILITY,
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
    assert json.loads((root / "current/config.json").read_text())["version"] == "new"
    assert release.activate(root, "old", "new")["current"] == "old"
    assert json.loads((root / "current/config.json").read_text())["version"] == "old"
    with pytest.raises(ValueError, match="current release changed"):
        release.activate(root, "new", "new")
    assert release.current_id(root) == "old"


@pytest.mark.parametrize(
    "fault",
    ["incomplete", "protocol", "file", "added", "role", "tls", "interpreter", "legacy"],
)
def test_invalid_candidate_does_not_change_current(installation, fault):
    root = installation
    directory = root / "releases/new"
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if fault == "incomplete":
        manifest["ready"] = False
    elif fault == "protocol":
        manifest["compatibility"] = {"control_alpn": "v1"}
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


def test_run_pins_old_version_and_blocks_selection_and_duplicate_start(installation):
    root = installation
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
