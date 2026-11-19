"""Check SIGINT/SIGTERM on the real, disconnected installed desktop entry."""

import argparse
import asyncio
import json
import os
import signal
import sys
from collections import deque
from pathlib import Path

from rd_core.async_compat import async_timeout


async def check(config, number):
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    process = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).with_name("rd-driving")),
        "--config",
        str(config),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    ready, messages = asyncio.Event(), deque(maxlen=100)

    async def read():
        while line := await process.stdout.readline():
            messages.append(line.decode(errors="replace"))
            if "Driving GUI event loop responding" in messages[-1]:
                ready.set()
        if not ready.is_set():
            raise RuntimeError("desktop exited before its GUI event loop responded")

    reader = asyncio.create_task(read())
    waiter = asyncio.create_task(ready.wait())
    try:
        async with async_timeout(15):
            done, _ = await asyncio.wait(
                (reader, waiter), return_when=asyncio.FIRST_COMPLETED
            )
            if reader in done:
                reader.result()
            if process.returncode is not None:
                raise RuntimeError("desktop exited before shutdown signal")
            process.send_signal(number)
            result = await process.wait()
            await reader
        if result != 0:
            raise RuntimeError(f"desktop did not shut down gracefully ({result})")
        return {"signal": number.name, "gui_event_loop_ready": True, "exitcode": result}
    except Exception:
        # Test instances use environment credentials; keep any diagnostics bounded
        # and remove their values even though the disconnected UI never logs them.
        detail = "".join(messages)
        for value in environment.values():
            if len(value) >= 32:
                detail = detail.replace(value, "<redacted>")
        print(detail, file=sys.stderr)
        raise
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        reader.cancel()
        waiter.cancel()
        await asyncio.gather(reader, waiter, return_exceptions=True)


async def main(config):
    reports = [
        await check(config, number) for number in (signal.SIGTERM, signal.SIGINT)
    ]
    print(json.dumps({"results": reports}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.config.resolve()))
