import asyncio
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from rd_core.async_compat import async_timeout
from rd_core.control import ControlSupervisor
from rd_core.settings import VehicleSettings
from rd_core.wire import Control, Execution, Packet, Window
from rd_vehicle.application.control_client import VehicleControlClient


@pytest.mark.asyncio
async def test_vehicle_uses_latest_bound_route_and_stops_on_its_command_deadline():
    async def until(predicate):
        async with async_timeout(2):
            while not predicate():
                await asyncio.sleep(0.005)

    frames, events, commands, stops = [], [], [], []
    protocol = SimpleNamespace(
        rpc_ready=True, send_datagram=lambda data: frames.append(Packet.decode(data)),
        notify=lambda method, params: events.append((method, params)),
        discard_pending_datagrams=lambda: None,
    )
    adapter = SimpleNamespace(
        send_command=lambda *values: commands.append(values), request_safe_stop=stops.append,
        close=lambda: None,
    )
    vehicle = VehicleControlClient(VehicleSettings(session=42), adapter)
    vehicle._loop = asyncio.get_running_loop()
    vehicle.supervisor = ControlSupervisor(adapter, timeout_s=0.3, on_result=vehicle._post_result)
    generation = vehicle._generation = object()
    old, epoch, next_epoch = uuid4(), uuid4(), uuid4()

    def route(revision, current):
        vehicle._event(generation, "control_route", {
            "epoch": str(current) if current else None,
            "vehicle_principal": "vehicle" if current else None, "revision": revision,
        })

    def windows(target):
        return [pkt for pkt in frames if pkt.epoch == target and isinstance(pkt.payload, Window)]

    def drive(target, token, seq, throttle, brake):
        packet = Packet(42, target, seq, time.time_ns() // 1_000_000, Control(throttle, brake, 0, token))
        vehicle._datagram(generation, packet.encode())

    route(2, epoch)
    route(1, old)  # An older event reaches its independent RPC stream late.
    assert vehicle.epoch is None
    vehicle.connection = SimpleNamespace(protocol=protocol, max_control_age_ms=200)
    watch = asyncio.create_task(vehicle._watch())
    try:
        await until(lambda: len(windows(epoch)) >= 2)
        assert not commands and not stops and not windows(old)
        token = windows(epoch)[-1].payload.token
        drive(epoch, token, 0, 0, 1)
        await until(lambda: any(isinstance(pkt.payload, Execution) and pkt.seq == 0 for pkt in frames))
        assert commands == [(0, 1, 0)]
        drive(epoch, token, 0, 0.5, 0)
        drive(old, token, 1, 0.6, 0)
        assert vehicle._last_seq == 0

        await until(lambda: events == [("stop_control", {"epoch": str(epoch)})])
        await until(lambda: stops)
        drive(epoch, token, 2, 1, 0)
        route(3, next_epoch)
        await until(lambda: windows(next_epoch))
        drive(next_epoch, token, 0, 1, 0)  # Fresh route cannot use an old window.
        assert vehicle._last_seq is None
        drive(next_epoch, windows(next_epoch)[-1].payload.token, 0, 0.25, 0)
        await until(lambda: commands == [(0, 1, 0), (0.25, 0, 0)])
        route(4, None)
        await until(lambda: len(stops) == 2)
        assert vehicle.epoch is None
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)
        assert vehicle.supervisor.close()
