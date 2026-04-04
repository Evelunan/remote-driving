# track_manager.py
from .video_track import VideoFrameTrack


class TrackManager:
    def __init__(self):
        self.video_tracks: dict[str, VideoFrameTrack] = {}

    def create_track(self, name: str) -> VideoFrameTrack:
        track = VideoFrameTrack(track_id=name)
        self.video_tracks[name] = track
        return track

    def get_track(self, name: str) -> VideoFrameTrack:
        return self.video_tracks[name]

    def all_tracks(self):
        return self.video_tracks.values()
