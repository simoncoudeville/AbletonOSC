from .handler import AbletonOSCHandler


class LiveRemoteHandler(AbletonOSCHandler):
    """
    Handlers added by LiveRemote. Kept apart from the upstream handlers so upstream
    changes merge cleanly.
    """

    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "song"

    def init_api(self):
        # /live/song/start_listen/tracks
        # Replies on /live/song/get/tracks with the track count whenever a track is added,
        # deleted or moved (and once right away). Live offers no other way to notice this.
        def tracks_getter(params):
            return len(self.song.tracks)

        def start_listen_tracks(params):
            self._start_listen(self.song, "tracks", (), getter=tracks_getter)

        def stop_listen_tracks(params):
            self._stop_listen(self.song, "tracks", ())

        self.osc_server.add_handler("/live/song/start_listen/tracks", start_listen_tracks)
        self.osc_server.add_handler("/live/song/stop_listen/tracks", stop_listen_tracks)
