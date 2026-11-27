"""The only aioquic 1.3 private-API bridge; covered by real transport tests.

There is no public abort method or bounded replace-latest datagram queue. Keep
these operations here and pin aioquic until an upgrade passes the same tests.
"""

from rd_core.wire import Execution, Packet, Telemetry, VehicleStatus, Window


def cancel_pending_io(protocol):
    for handle in (protocol._timer, protocol._transmit_task):
        if handle is not None:
            handle.cancel()
    protocol._timer = protocol._transmit_task = None


def _wire_packet(data):
    try:
        return Packet.decode(data)
    except ValueError:
        return None  # The transport also accepts opaque application datagrams.


def _queue_datagram(quic, data, best_effort):
    """Keep one pending datagram, or one window and one ACK for the same route."""
    pending = quic._datagrams_pending
    if pending:
        if best_effort:
            return False
        packet = _wire_packet(data)
        for index, previous in enumerate(pending):
            queued = _wire_packet(previous)
            if queued is None or not isinstance(queued.payload, (Window, Execution)):
                pending.clear()
                break
            if packet is not None and isinstance(packet.payload, (Telemetry, VehicleStatus)):
                return False
            if (
                packet is None or not isinstance(packet.payload, (Window, Execution))
                or packet.session != queued.session or packet.epoch != queued.epoch
            ):
                pending.clear()
                break
            if type(queued.payload) is type(packet.payload):
                # Preserve FIFO between the two kinds even if one is refreshed
                # many times before the congestion window admits another frame.
                pending[index] = data
                return True
    quic.send_datagram_frame(data)
    return True


def send_latest_datagram(protocol, data, *, best_effort):
    if not _queue_datagram(protocol._quic, data, best_effort):
        return False
    protocol.transmit()
    return True


def discard_pending_datagrams(protocol):
    protocol._quic._datagrams_pending.clear()


def has_stream_capacity(protocol, limit):
    # Include streams waiting for transport ACKs, not just application futures.
    return len(protocol._quic._streams) < limit


def finish_abandoned_close(protocol):
    """Release aioquic context-manager waiters after a forced local deadline."""
    waiter = protocol._connected_waiter
    protocol._connected_waiter = None
    if waiter is not None and not waiter.done():
        waiter.cancel()
    for waiter in protocol._ping_waiters.values():
        if not waiter.done():
            waiter.cancel()
    protocol._ping_waiters.clear()
    protocol._closed.set()
