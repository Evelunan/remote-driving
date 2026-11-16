"""Calibrate packet scheduling on real loopback UDP before using safety results."""

import asyncio
import importlib
import time

import pytest


@pytest.fixture
def module(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("network_shaper")


class Receiver(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received.put_nowait((data, time.monotonic()))


async def endpoints(shaper):
    loop = asyncio.get_running_loop()
    sink, receiver = await loop.create_datagram_endpoint(
        Receiver, local_addr=("127.0.0.1", 0)
    )
    with shaper.install():
        source, _ = await loop.create_datagram_endpoint(
            Receiver, local_addr=("127.0.0.1", 0)
        )
    return source, sink, receiver


@pytest.mark.asyncio
async def test_delay_rate_and_queue_limits_affect_real_packets(module):
    shaper = module.Shaper(classify=lambda _: "test")
    source, sink, receiver = await endpoints(shaper)
    address = sink.get_extra_info("sockname")
    try:
        shaper.set("test", module.Policy(delay_ms=40, rate_bps=8000))
        start = time.monotonic()
        for _ in range(3):
            source.sendto(b"x" * 100, address)
        arrivals = [
            await asyncio.wait_for(receiver.received.get(), 1) for _ in range(3)
        ]
        assert arrivals[0][1] - start >= 0.12
        assert arrivals[2][1] - start >= 0.32
        assert arrivals[2][1] - arrivals[0][1] >= 0.17
        shaper.set("test", module.Policy(delay_ms=100, max_queue_bytes=150))
        for _ in range(3):
            source.sendto(b"y" * 100, address)
        assert source.queued_bytes == 100 and source.stats["queue_drops"] == 2
        shaper.set("test", module.Policy(blackhole=True))
        assert not source.pending and source.queued_bytes == 0
        source.sendto(b"z", address)
        await asyncio.sleep(0.15)
        assert receiver.received.empty() and source.stats["blackhole_drops"] == 1
        shaper.set("test", module.Policy(delay_ms=40))
        late = time.monotonic()
        source.sendto(b"late", address)
        shaper.set("test", module.Policy(), discard_pending=False)
        packet, arrived = await asyncio.wait_for(receiver.received.get(), 1)
        assert packet == b"late" and arrived - late >= 0.03
        shaper.set("test", module.Policy(delay_ms=20, max_queue_packets=2))
        for _ in range(4):
            source.sendto(b"", address)
        assert len(source.pending) == 2
        source.close()
        await asyncio.sleep(0.04)
        assert not source.pending and receiver.received.empty()
    finally:
        source.close()
        sink.close()
    assert not shaper.errors


@pytest.mark.asyncio
async def test_seeded_loss_jitter_recovery_and_loopback_restriction(module):
    async def case():
        shaper = module.Shaper(seed=5, classify=lambda _: "test")
        source, sink, receiver = await endpoints(shaper)
        try:
            shaper.set("test", module.Policy(delay_ms=20, jitter_ms=15, loss=0.2))
            address = sink.get_extra_info("sockname")
            for index in range(100):
                source.sendto(bytes([index]), address)
            await asyncio.sleep(0.1)
            received = []
            while not receiver.received.empty():
                received.append(receiver.received.get_nowait()[0])
            assert 0 < len(received) < 100
            assert source.stats["random_drops"] == 100 - len(received)
            shaper.set("test", module.Policy())
            source.sendto(b"recovered", address)
            assert (await asyncio.wait_for(receiver.received.get(), 1))[
                0
            ] == b"recovered"
            with pytest.raises(RuntimeError, match="loopback"):
                source.sendto(b"must not leave host", ("192.0.2.1", 10000))
            return set(received)
        finally:
            shaper.close()
            sink.close()

    assert await case() == await case()
