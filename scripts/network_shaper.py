"""Bounded, seeded UDP fault injection around real asyncio loopback sockets.

Diagnostic use only: no kernel/network configuration changes, no payload logs,
no fabricated protocol ACKs. Delays include UDP STUN/DTLS and QUIC handshakes.
"""

import asyncio
import ipaddress
import math
import random
from collections import Counter
from dataclasses import asdict, dataclass
from unittest.mock import patch


@dataclass(frozen=True)
class Policy:
    delay_ms: float = 0
    jitter_ms: float = 0
    loss: float = 0
    rate_bps: int = 0
    blackhole: bool = False
    max_queue_bytes: int = 256 * 1024
    max_queue_packets: int = 256
    max_queue_ms: float = 500

    def __post_init__(self):
        if (
            any(
                not math.isfinite(v) or v < 0
                for v in (self.delay_ms, self.jitter_ms, self.rate_bps)
            )
            or not math.isfinite(self.loss)
            or not 0 <= self.loss <= 1
            or type(self.rate_bps) is not int
            or type(self.max_queue_bytes) is not int
            or self.max_queue_bytes <= 0
            or type(self.max_queue_packets) is not int
            or self.max_queue_packets <= 0
            or type(self.blackhole) is not bool
            or not math.isfinite(self.max_queue_ms)
            or self.max_queue_ms <= 0
        ):
            raise ValueError("invalid UDP impairment policy")


def category(protocol):
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.asyncio.server import QuicServer

    if isinstance(protocol, (QuicConnectionProtocol, QuicServer)):
        return "control"
    if type(protocol).__module__.startswith("aioice."):
        return "media"
    raise TypeError(f"unclassified UDP endpoint: {type(protocol).__name__}")


class ShapedTransport(asyncio.DatagramTransport):
    def __init__(self, raw, wrapper, shaper, label, index):
        self.raw, self.wrapper, self.shaper, self.label = raw, wrapper, shaper, label
        self.random = random.Random(shaper.seed + index)
        self.stats = Counter()
        self.pending = {}
        self.queued_bytes = 0
        self.serialized_until = 0

    def sendto(self, data, addr=None):
        destination = addr or self.raw.get_extra_info("peername")
        host = ipaddress.ip_address(destination[0].split("%", 1)[0])
        host = getattr(host, "ipv4_mapped", None) or host
        if not host.is_loopback:
            self.shaper.errors.append("non_loopback_destination")
            raise RuntimeError("fault injection is restricted to loopback")
        if self.is_closing():
            return
        policy = self.shaper.policies.get(self.label, Policy())
        self.stats["offered_packets"] += 1
        self.stats["offered_bytes"] += len(data)
        if policy.blackhole or self.random.random() < policy.loss:
            self.stats["blackhole_drops" if policy.blackhole else "random_drops"] += 1
            return
        now = self.shaper.loop.time()
        serialization = len(data) * 8 / policy.rate_bps if policy.rate_bps else 0
        finish = max(now, self.serialized_until) + serialization
        delay = (
            max(
                0,
                policy.delay_ms
                + self.random.uniform(-policy.jitter_ms, policy.jitter_ms),
            )
            / 1000
        )
        due = finish + delay
        if (
            self.queued_bytes + len(data) > policy.max_queue_bytes
            or len(self.pending) >= policy.max_queue_packets
            or due - now > policy.max_queue_ms / 1000
        ):
            self.stats["queue_drops"] += 1
            return
        self.serialized_until = finish
        payload = bytes(data)
        handle = None

        def deliver():
            self.pending.pop(handle, None)
            self.queued_bytes -= len(payload)
            if self.is_closing():
                self.stats["closed_drops"] += 1
                return
            try:
                self.raw.sendto(payload, addr)
                self.stats["sent_packets"] += 1
                self.stats["sent_bytes"] += len(payload)
            except Exception as exc:
                self.shaper.errors.append(type(exc).__name__)
                self.wrapper.inner.error_received(exc)

        handle = self.shaper.loop.call_at(due, deliver)
        self.pending[handle] = len(payload)
        self.queued_bytes += len(payload)
        self.stats["queue_peak_bytes"] = max(
            self.stats["queue_peak_bytes"], self.queued_bytes
        )

    def discard_pending(self):
        for handle in self.pending:
            handle.cancel()
        self.stats["cancelled_packets"] += len(self.pending)
        self.pending.clear()
        self.queued_bytes = 0
        self.serialized_until = self.shaper.loop.time()

    def close(self):
        self.discard_pending()
        self.raw.close()

    def abort(self):
        self.discard_pending()
        self.raw.abort()

    def is_closing(self):
        return self.raw.is_closing()

    def get_extra_info(self, name, default=None):
        return self.raw.get_extra_info(name, default)

    def get_protocol(self):
        return self.wrapper.inner

    def set_protocol(self, protocol):
        self.wrapper.inner = protocol

    def get_write_buffer_size(self):
        return self.queued_bytes + self.raw.get_write_buffer_size()

    def get_write_buffer_limits(self):
        return self.raw.get_write_buffer_limits()

    def set_write_buffer_limits(self, high=None, low=None):
        return self.raw.set_write_buffer_limits(high, low)


class Shaper:
    def __init__(self, *, seed=42, classify=category):
        self.seed, self.classify = seed, classify
        self.loop = asyncio.get_running_loop()
        self.policies, self.endpoints, self.errors = {}, [], []
        self.history = []

    def set(self, label, policy, *, discard_pending=True):
        self.policies[label] = policy
        self.history.append(
            {
                "at": self.loop.time(),
                "category": label,
                "policy": asdict(policy),
                "discard_pending": discard_pending,
            }
        )
        for endpoint in self.endpoints:
            if endpoint.label == label and discard_pending:
                # Fault entry may replace the old path. Recovery can preserve
                # already scheduled packets to exercise late delivery.
                endpoint.discard_pending()

    def install(self):
        original = self.loop.create_datagram_endpoint
        shaper = self

        class Wrapper(asyncio.DatagramProtocol):
            def __init__(self, inner):
                self.inner, self.transport = inner, None

            def connection_made(self, raw):
                label = shaper.classify(self.inner)
                self.transport = ShapedTransport(
                    raw, self, shaper, label, len(shaper.endpoints)
                )
                shaper.endpoints.append(self.transport)
                self.inner.connection_made(self.transport)

            def datagram_received(self, data, addr):
                self.transport.stats["received_packets"] += 1
                self.inner.datagram_received(data, addr)

            def error_received(self, exc):
                self.inner.error_received(exc)

            def connection_lost(self, exc):
                if self.transport is not None:
                    self.transport.discard_pending()
                self.inner.connection_lost(exc)

        async def create(factory, *args, **kwargs):
            if len(shaper.endpoints) >= 1024:
                raise RuntimeError("diagnostic endpoint limit exceeded")
            inner = factory()
            # Reject before creating a socket; callback exceptions alone would
            # otherwise leave the endpoint half constructed.
            shaper.classify(inner)
            raw, wrapper = await original(lambda: Wrapper(inner), *args, **kwargs)
            return wrapper.transport, wrapper.inner

        return patch.object(self.loop, "create_datagram_endpoint", create)

    def snapshot(self):
        return {
            "at": self.loop.time(),
            "seed": self.seed,
            "policies": list(self.history),
            "errors": list(self.errors),
            "endpoints": [
                {
                    "category": endpoint.label,
                    "local": endpoint.get_extra_info("sockname"),
                    "stats": dict(endpoint.stats),
                    "queued_bytes": endpoint.queued_bytes,
                    "pending_packets": len(endpoint.pending),
                    "closed": endpoint.is_closing(),
                }
                for endpoint in self.endpoints
            ],
        }

    def close(self):
        for endpoint in self.endpoints:
            endpoint.close()
