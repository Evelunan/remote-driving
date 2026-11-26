"""Release complete VP8 frames without waiting for the next RTP timestamp."""

from aiortc import __version__
from aiortc.jitterbuffer import JitterBuffer, JitterFrame


class Vp8JitterBuffer(JitterBuffer):
    def __init__(self, capacity, payload_types):
        super().__init__(capacity=capacity, is_video=True)
        self._vp8_payload_types = frozenset(payload_types)
        self._discarded_frame = False

    def add(self, packet):
        self._discarded_frame = False
        pli, frame = super().add(packet)
        return pli or self._discarded_frame, frame

    def _remove_frame(self, sequence_number):
        arriving = self._packets[sequence_number % self.capacity]
        while True:
            first = self._packets[self._origin % self.capacity]
            if first is None:
                return None
            if not first.payload and first.padding_size:
                self.remove(1)
                continue
            # aiortc sends a released frame to the decoder for the arrival's codec.
            if (
                first.payload_type != arriving.payload_type
                or first.ssrc != arriving.ssrc
            ):
                return None
            if first.payload_type not in self._vp8_payload_types:
                return super()._remove_frame(sequence_number)

            chunks = []
            complete = _starts_frame(first)
            for count in range(self.capacity):
                packet = self._packets[(self._origin + count) % self.capacity]
                if packet is None:
                    return None  # Keep the sequence gap available for NACK/RTX.
                if not packet.payload and packet.padding_size:
                    continue
                if (
                    packet.timestamp != first.timestamp
                    or packet.payload_type != first.payload_type
                    or packet.ssrc != first.ssrc
                ):
                    self.remove(count)
                    self._discarded_frame = True
                    break
                if not packet._data or (count and _starts_frame(packet)):
                    complete = False
                if complete:
                    chunks.append(packet._data)
                if packet.marker:
                    self.remove(count + 1)
                    if complete:
                        return JitterFrame(b"".join(chunks), first.timestamp)
                    self._discarded_frame = True
                    break
            else:
                return None


def _starts_frame(packet):
    # RFC 7741: the first packet sets S=1 and PID=0 in the VP8 descriptor.
    return bool(packet.payload and packet.payload[0] & 0x17 == 0x10)


def enable_low_latency_vp8(transceiver):
    # aiortc 1.14 has no public depacketizer or jitter-buffer hook.
    if __version__ != "1.14.0":
        raise RuntimeError("review the VP8 jitter buffer for this aiortc version")
    payload_types = {
        codec.payloadType
        for codec in transceiver._codecs
        if codec.mimeType.lower() == "video/vp8"
    }
    if transceiver._codecs and transceiver._codecs[0].payloadType in payload_types:
        receiver = transceiver.receiver
        original = receiver._RTCRtpReceiver__jitter_buffer
        if type(original) is not JitterBuffer or original._origin is not None:
            raise RuntimeError("expected an unused aiortc video jitter buffer")
        receiver._RTCRtpReceiver__jitter_buffer = Vp8JitterBuffer(
            original.capacity, payload_types
        )
