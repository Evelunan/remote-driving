import json
import os
import subprocess
import sys
import textwrap
import time

import pytest
import yaml


@pytest.mark.parametrize("number", ["SIGINT", "SIGTERM"])
def test_stop_signals_repeat_without_cancelling_and_restore_handlers(
    legacy_python, number
):
    legacy_python(
        "vehicle",
        f"""
        import asyncio, signal
        from rd_vehicle.shutdown import stop_on_signals
        async def check():
            stop = asyncio.Event()
            previous = {{n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}}
            with stop_on_signals(stop):
                signal.raise_signal(signal.{number})
                await asyncio.wait_for(stop.wait(), 1)
                signal.raise_signal(signal.{number})
                await asyncio.sleep(0.08)
                assert not asyncio.current_task().cancelling()
            assert all(signal.getsignal(n) == handler for n, handler in previous.items())
            try:
                with stop_on_signals(asyncio.Event()):
                    raise RuntimeError('cleanup failed')
            except RuntimeError:
                pass
            assert all(signal.getsignal(n) == handler for n, handler in previous.items())
        asyncio.run(check())
    """,
    )


@pytest.mark.skipif(
    sys.platform != "win32", reason="real Windows console Ctrl+C broadcast"
)
def test_windows_console_ctrl_c_cleans_up_both_formal_components(tmp_path, local_tls):
    config = tmp_path / "vehicle.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "controller": "mock",
                "credentials": {"token_env": "VEHICLE_SHUTDOWN_TEST_TOKEN"},
                "network": {
                    "quic_host": "127.0.0.1",
                    "quic_server_name": "localhost",
                    "quic_ca_file": str(local_tls[0]),
                    "signaling_ca_file": str(local_tls[0]),
                    # These unused endpoints require no running server or real vehicle.
                    "quic_port": 9,
                    "signaling_url": "wss://127.0.0.1:9",
                },
                "cameras": [
                    {
                        "name": "front",
                        "source": "synthetic",
                        "width": 96,
                        "height": 64,
                        "fps": 10,
                    }
                ],
                "processes": {"startup_timeout_s": 10, "shutdown_timeout_s": 5},
            }
        ),
        encoding="utf-8",
    )
    ready, report = tmp_path / "ready", tmp_path / "report.json"
    script = tmp_path / "console_vehicle.py"
    script.write_text(
        textwrap.dedent(f"""
        import asyncio, json, signal
        from pathlib import Path
        from rd_vehicle.main import parse_args
        from rd_vehicle.processes import VehicleProcesses

        class ObservedProcesses(VehicleProcesses):
            async def _close(self):
                # Keep the parent in cleanup while the console sends repeats.
                await asyncio.sleep(0.3)
                await super()._close()

        async def main():
            settings = parse_args(['--config', {str(config)!r}]).loaded.value
            group = ObservedProcesses(settings)
            previous = {{n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}}
            task = asyncio.create_task(group.run(install_signals=True))
            try:
                while not (len(group.children) == 2 and all(c.heartbeat_at for c in group.children)):
                    if task.done():
                        task.result()
                        raise AssertionError('stopped before ready')
                    await asyncio.sleep(0.02)
                Path({str(ready)!r}).touch()
                await asyncio.wait_for(task, 15)
                assert all(signal.getsignal(n) == handler for n, handler in previous.items())
                Path({str(report)!r}).write_text(json.dumps(group.outcomes))
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        if __name__ == '__main__':
            asyncio.run(main())
    """),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["VEHICLE_SHUTDOWN_TEST_TOKEN"] = "shutdown-test-" + "x" * 40
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    output = tmp_path / "console.log"
    with output.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(script)],
            env=env,
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=startup,
        )
        try:
            deadline = time.monotonic() + 15
            while (
                not ready.exists()
                and process.poll() is None
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert ready.exists(), output.read_text(encoding="utf-8")
            # Attach a separate helper to this test's hidden console only. The
            # pytest/user console never receives the generated broadcast.
            sender = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    textwrap.dedent("""
                    import ctypes, sys, time
                    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                    kernel.FreeConsole()
                    assert kernel.AttachConsole(int(sys.argv[1])), ctypes.get_last_error()
                    assert kernel.SetConsoleCtrlHandler(None, True)
                    for _ in range(3):
                        assert kernel.GenerateConsoleCtrlEvent(0, 0), ctypes.get_last_error()
                        time.sleep(0.08)
                    kernel.FreeConsole()
                """),
                    str(process.pid),
                ],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            assert sender.returncode == 0, sender.stdout + sender.stderr
            assert process.wait(timeout=12) == 0, output.read_text(encoding="utf-8")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    outcomes = json.loads(report.read_text())
    assert {item["component"] for item in outcomes} == {"control", "media"}
    assert all(item["exitcode"] == 0 and not item["forced"] for item in outcomes)
    logged = output.read_text(encoding="utf-8")
    assert not any(
        text in logged for text in ["Traceback", "KeyboardInterrupt", "ProcessFailure"]
    ), logged
