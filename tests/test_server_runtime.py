import asyncio

import pytest
from rd_server.runtime import ServerRuntime


class Service:
    def __init__(self, name, events, fail=False):
        self.name, self.events, self.fail = name, events, fail
        self.closed = asyncio.Event()

    async def start(self):
        self.events.append("start:" + self.name)
        if self.fail:
            raise OSError("simulated bind failure")

    async def stop(self):
        self.events.append("stop:" + self.name)
        self.closed.set()

    async def wait_closed(self):
        await self.closed.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("names", [("signaling",), ("relay",), ("signaling", "relay")])
async def test_selected_modules_are_started_and_stopped(names):
    events = []
    runtime = ServerRuntime({name: Service(name, events) for name in names})
    assert not runtime.ready
    await runtime.start()
    assert runtime.ready
    await runtime.stop()
    await runtime.stop()
    assert not runtime.ready
    assert events == ["start:" + name for name in names] + [
        "stop:" + name for name in reversed(names)
    ]


@pytest.mark.asyncio
async def test_partial_start_rolls_back_even_the_failing_module():
    events = []
    runtime = ServerRuntime(
        {
            "signaling": Service("signaling", events),
            "relay": Service("relay", events, fail=True),
        }
    )
    with pytest.raises(OSError, match="bind failure"):
        await runtime.start()
    assert events == ["start:signaling", "start:relay", "stop:relay", "stop:signaling"]
    assert not runtime.ready


@pytest.mark.asyncio
async def test_lost_required_listener_stops_other_module():
    events = []
    signaling, relay = Service("signaling", events), Service("relay", events)
    runtime = ServerRuntime({"signaling": signaling, "relay": relay})
    task = asyncio.create_task(runtime.run(asyncio.Event()))
    await asyncio.sleep(0)
    assert runtime.ready
    signaling.closed.set()
    with pytest.raises(RuntimeError, match="unexpectedly"):
        await asyncio.wait_for(task, 1)
    assert not runtime.ready and relay.closed.is_set()
