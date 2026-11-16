"""Capture sources outlive individual signaling / media subscriptions."""

from rd_webrtc.video import CapturedImage, FrameSource


class TrackManager:
    def __init__(self):
        self.sources: dict[str, FrameSource] = {}
        self.subscriptions = {}

    def register(self, name: str, fps=30, max_age_s=0.5):
        if name in self.sources:
            raise ValueError("duplicate camera name")
        self.sources[name] = FrameSource(name, fps, max_age_s)

    def create_tracks(self, peer_id):
        self.close_tracks(peer_id)
        tracks = {name: source.subscribe() for name, source in self.sources.items()}
        self.subscriptions[peer_id] = tracks
        return tracks

    def publish(self, name, image: CapturedImage):
        return self.sources[name].publish(image)

    def invalidate(self, name):
        self.sources[name].invalidate()

    def close_tracks(self, peer_id=None):
        owners = tuple(self.subscriptions) if peer_id is None else (peer_id,)
        for owner in owners:
            for track in self.subscriptions.pop(owner, {}).values():
                track.stop()

    def close(self):
        self.close_tracks()
        for source in self.sources.values():
            source.close()
