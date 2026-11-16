# track_manager.py
from .video_track import VideoFrameTrack


class TrackManager:
    def __init__(self):
        self.video_tracks: dict[str, VideoFrameTrack] = {}
        self.track_names: set[str] = set()

    def register(self, name: str):
        self.track_names.add(name)

    def create_tracks(self):
        self.video_tracks.clear()

        for name in self.track_names:
            self.video_tracks[name] = VideoFrameTrack(track_id=name)

        return self.video_tracks

    def get_track(self, name: str):
        return self.video_tracks.get(name)

    def all_tracks(self):
        return self.video_tracks.values()

    def close_tracks(self):
        for track in self.video_tracks.values():
            try:
                track.stop()
            except Exception:
                pass

        self.video_tracks.clear()

    def reset_tracks(self):
        self.close_tracks()
        return self.create_tracks()
