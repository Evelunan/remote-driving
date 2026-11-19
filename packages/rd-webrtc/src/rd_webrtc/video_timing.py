"""Capture clock correlation for each RTP stream in one peer connection."""

import struct
import time


CHANNEL = "rd-video-clock-v1"
VIEWS = ("front", "back", "left", "right")
RATE = 90_000
MASK = (1 << 32) - 1
PROBE = struct.Struct("!Q")
ANCHOR = struct.Struct("!BI")  # view index, RTP clock at the instant of reply
SYNC_TTL = 5.0


class PublisherTiming:
    def __init__(self, pc):
        self.origins = {}
        channel = pc.createDataChannel(CHANNEL, ordered=False, maxRetransmits=0)

        @channel.on("message")
        def reply(probe):
            if not isinstance(probe, bytes) or len(probe) != PROBE.size:
                return
            if channel.readyState != "open" or channel.bufferedAmount:
                return
            now = time.monotonic()
            message = bytearray(probe)
            for view, (rtp, captured_at) in self.origins.items():
                ticks = rtp + round((now - captured_at) * RATE)
                message.extend(ANCHOR.pack(view, ticks & MASK))
            channel.send(bytes(message))

    def bind(self, sender, track):
        """Learn aiortc 1.14's random RTP offset once, after a frame was sent."""
        if track.id not in VIEWS:
            return
        view = VIEWS.index(track.id)
        encode = sender._next_encoded_frame
        encoded_pts = None

        async def learn_origin(codec):
            nonlocal encoded_pts
            if encoded_pts is not None:
                # The RTP loop requests the next frame only after transmitting
                # the preceding one. Dropped encoder inputs never set this key.
                wire_pts = sender._RTCRtpSender__rtp_timestamp
                self.origins[view] = (
                    (wire_pts - encoded_pts) & MASK,
                    track.capture_origin,
                )
                sender._next_encoded_frame = encode
                return await encode(codec)
            frame = await encode(codec)
            if frame is not None:
                encoded_pts = frame.timestamp
            return frame

        sender._next_encoded_frame = learn_origin


class ReceiverTiming:
    def __init__(self):
        self.channel = None
        self.pending = None
        self.next_probe = 0
        self.sample_at = 0
        self.anchors = {}

    def attach(self, channel):
        if channel.label != CHANNEL or self.channel is not None:
            return
        self.channel = channel
        channel.on("message", self._reply)

    def _reply(self, message):
        if not isinstance(message, bytes) or self.pending is None:
            return
        size = len(message) - PROBE.size
        if not 0 <= size <= len(VIEWS) * ANCHOR.size or size % ANCHOR.size:
            return
        if PROBE.unpack_from(message)[0] != self.pending:
            return
        now = time.monotonic()
        sent = self.pending / 1e9
        self.pending = None
        # The midpoint estimates the instant of the remote reply. Its error
        # due to path asymmetry can be up to half the probe's round trip.
        if not 0 <= now - sent < SYNC_TTL:
            return
        self.sample_at = (sent + now) / 2
        self.anchors = {
            VIEWS[index]: ticks
            for index, ticks in ANCHOR.iter_unpack(message[PROBE.size :])
            if index < len(VIEWS)
        }

    def capture_time(self, receiver, pts):
        """Return this decoded frame's capture time on the local monotonic clock."""
        channel = self.channel
        if channel is None or channel.readyState != "open":
            return None
        now = time.monotonic()
        if self.pending is not None and now - self.pending / 1e9 >= SYNC_TTL:
            self.pending = None
        if (
            self.pending is None
            and now >= self.next_probe
            and not channel.bufferedAmount
        ):
            self.pending = time.monotonic_ns()
            channel.send(PROBE.pack(self.pending))
            self.next_probe = now + 1
        anchor = self.anchors.get(receiver.track.id)
        if anchor is None or now - self.sample_at >= SYNC_TTL:
            return None
        # aiortc 1.14 rebases decoded PTS to the first complete RTP frame.
        # Reversing its mapper also works after packet loss and 32-bit wrap.
        origin = receiver._RTCRtpReceiver__timestamp_mapper._origin
        if origin is None:
            return None
        delta = (pts + origin - anchor + (1 << 31)) & MASK
        captured_at = self.sample_at + (delta - (1 << 31)) / RATE
        return captured_at if captured_at <= now else None
