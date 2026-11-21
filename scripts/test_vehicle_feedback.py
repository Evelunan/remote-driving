"""Run the reviewed feedback regressions with all artifacts inside this checkout.

No dependency installation, real ROS nodes, device access or external listeners.
The QUIC fixtures bind only loopback addresses and dynamically allocated ports.
"""

import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    "test_vehicle_status_wire.py", "test_vehicle_feedback.py", "test_vehicle_status_ui.py",
    "test_v2_wire.py", "test_control_leases.py", "test_control_clients.py",
    "test_configuration.py", "test_legacy_controllers.py", "test_driving_input_status.py",
    "test_driving_usb_retry.py", "test_scoped_reconnect.py", "test_legacy_control_panel.py",
    "test_legacy_qt_signals.py", "test_client_entrypoints.py", "test_gui_shutdown_signals.py",
    "test_vehicle_shutdown.py", "test_control_supervisor.py", "test_input_freshness.py",
)


def main():
    python = ROOT / ".venv/bin/python"
    if not python.is_file():
        raise SystemExit("Use the existing project .venv; this runner does not install dependencies.")
    base = ROOT / ".rd-runtime/feedback-tests"
    base.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=base))
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    for name, subdir in {
        "TMPDIR": "tmp", "TMP": "tmp", "TEMP": "tmp",
        "XDG_CACHE_HOME": "cache", "XDG_RUNTIME_DIR": "qt",
        "XDG_CONFIG_HOME": "config", "XDG_DATA_HOME": "data",
        "RD_RUNTIME_DIR": "runtime", "RUFF_CACHE_DIR": "cache/ruff",
        "ROS_LOG_DIR": "ros-logs",
    }.items():
        directory = run / subdir
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        env[name] = str(directory)
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
               QT_QPA_PLATFORM="offscreen")
    print(f"Feedback test artifacts: {run.relative_to(ROOT)}", flush=True)
    result = subprocess.run([
        str(python), "-m", "pytest", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider",
        "-q", f"--basetemp={run / 'pytest'}", f"--junitxml={run / 'pytest.xml'}",
        *(f"tests/{name}" for name in TESTS),
    ], cwd=ROOT, env=env, check=False)
    if result.returncode:
        return result.returncode
    return subprocess.run([
        str(python), "-m", "ruff", "check", "--isolated", "--target-version", "py310",
        "--select", "E9,F63,F7,F82", "apps", "packages", "scripts", "tests",
    ], cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
