import asyncio

import pytest
from rd_core.async_compat import async_timeout
from rd_server.runtime import ServerRuntime


class Service:
    def __init__(self, name, events, fail=False):
        self.name, self.events, self.fail = name, events, fail
        self.closed = asyncio.Event()

    async def start(self):
        self.events.append("start:" + self.name)
        self.closed.clear()
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
@pytest.mark.parametrize("failed", ["signaling", "relay"])
@pytest.mark.parametrize("during_start", [True, False])
async def test_only_failed_listener_restarts(failed, during_start):
    events = []
    services = {name: Service(name, events) for name in ("signaling", "relay")}
    healthy = "relay" if failed == "signaling" else "signaling"
    services[failed].fail = during_start
    runtime = ServerRuntime(services)
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    try:
        async with async_timeout(3):
            if not during_start:
                while not runtime.ready:
                    await asyncio.sleep(0.01)
                services[failed].closed.set()
            while "stop:" + failed not in events:
                await asyncio.sleep(0.01)
            assert not runtime.ready
            services[failed].fail = False
            while not runtime.ready:
                await asyncio.sleep(0.01)
        assert events.count("start:" + failed) == 2
        assert events.count("start:" + healthy) == 1 and "stop:" + healthy not in events
    finally:
        stop.set()
        await asyncio.wait_for(task, 1)
    assert not runtime.ready and all(service.closed.is_set() for service in services.values())


@pytest.mark.asyncio
async def test_single_listener_bind_failure_is_reported_to_caller():
    service = Service("relay", [], fail=True)
    runtime = ServerRuntime({"relay": service})
    with pytest.raises(OSError, match="bind failure"):
        await runtime.run(asyncio.Event())
    assert service.closed.is_set() and not runtime.ready
