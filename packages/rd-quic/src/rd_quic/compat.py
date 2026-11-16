"""The only aioquic 1.3 private-API bridge; covered by real transport tests.

There is no public abort method or bounded replace-latest datagram queue. Keep
these operations here and pin aioquic until an upgrade passes the same tests.
"""


def cancel_pending_io(protocol):
    for handle in (protocol._timer, protocol._transmit_task):
        if handle is not None:
            handle.cancel()
    protocol._timer = protocol._transmit_task = None


def send_latest_datagram(protocol, data):
    protocol._quic._datagrams_pending.clear()
    protocol._quic.send_datagram_frame(data)
    protocol.transmit()


def has_stream_capacity(protocol, limit):
    # Include streams waiting for transport ACKs, not just application futures.
    return len(protocol._quic._streams) < limit
