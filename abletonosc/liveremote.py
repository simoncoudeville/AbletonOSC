from .handler import AbletonOSCHandler


class LiveRemoteHandler(AbletonOSCHandler):
    """
    Handlers added by LiveRemote. Kept apart from the upstream handlers so upstream
    changes merge cleanly.
    """

    def __init__(self, manager):
        super().__init__(manager)
        self.class_identifier = "song"
        self.clip_listeners = []
        self.clip_grid_active = False

    def init_api(self):
        self.osc_server.add_handler("/live/song/get/track_snapshot", lambda params: self._snapshot())
        self.osc_server.add_handler("/live/song/start_listen/clip_grid", self._start_listen_clip_grid)
        self.osc_server.add_handler("/live/song/start_listen/tracks", self._start_listen_tracks)
        self.osc_server.add_handler("/live/song/stop_listen/tracks", self._stop_listen_tracks)

    def _snapshot(self):
        """
        /live/song/get/track_snapshot

        One flat reply with 8 values per track, in track order: name, color, volume, mute,
        solo, arm, playing_slot_index, fired_slot_index. Lets a client redraw the whole
        mixer at once instead of waiting for one listener reply per property.
        """
        values = []
        for track in self.song.tracks:
            values += [
                track.name,
                track.color,
                float(track.mixer_device.volume.value),
                bool(track.mute),
                bool(track.solo),
                bool(track.arm) if track.can_be_armed else False,
                int(track.playing_slot_index),
                int(track.fired_slot_index),
            ]
        return tuple(values)

    def _start_listen_tracks(self, params):
        """
        /live/song/start_listen/tracks

        Replies on /live/song/get/tracks with the track count, once right away and again
        whenever a track is added, deleted, duplicated or moved.

        Track listeners are bound to a track object but report the index they were
        registered under. When the list changes those indices are wrong, so all track
        listeners are removed here, before the reply. The client registers them again.
        """
        key = ("tracks", ())
        if key in self.listener_functions:
            self._stop_listen(self.song, "tracks", ())

        def tracks_changed():
            self.logger.info("Track list changed, clearing track listeners")
            for handler in self.manager.handlers:
                if handler.class_identifier == "track":
                    handler._clear_listeners()
            self.logger.info("Sending track count %d" % len(self.song.tracks))
            self.osc_server.send("/live/song/get/tracks", (len(self.song.tracks),))
            self.osc_server.send("/live/song/get/track_snapshot", self._snapshot())
            if self.clip_grid_active:
                self._rebuild_clip_listeners()
                self._send_clip_grid()

        self.song.add_tracks_listener(tracks_changed)
        self.listener_functions[key] = tracks_changed
        self.listener_objects[key] = self.song
        self.osc_server.send("/live/song/get/tracks", (len(self.song.tracks),))
        self.osc_server.send("/live/song/get/track_snapshot", self._snapshot())

    def _stop_listen_tracks(self, params):
        self._stop_listen(self.song, "tracks", ())

    # Clip grid ----------------------------------------------------------------------

    def _start_listen_clip_grid(self, params):
        """
        /live/song/start_listen/clip_grid

        Replies on /live/song/get/clip_grid with [track_count, scene_count, has_clip...]
        (every slot of track 0, then track 1, ...), once right away and again whenever
        tracks or scenes are added, deleted or moved. Between those, a slot gaining or
        losing a clip replies on /live/song/get/clip_slot_has_clip [track, scene, bool].
        The listeners live inside Live, so this costs no OSC traffic.
        """
        self.clip_grid_active = True
        key = ("scenes", ())
        if key in self.listener_functions:
            self._stop_listen(self.song, "scenes", ())

        def scenes_changed():
            self.logger.info("Scene list changed")
            self._rebuild_clip_listeners()
            self._send_clip_grid()

        self.song.add_scenes_listener(scenes_changed)
        self.listener_functions[key] = scenes_changed
        self.listener_objects[key] = self.song
        self._rebuild_clip_listeners()
        self._send_clip_grid()

    def _rebuild_clip_listeners(self):
        self._remove_clip_listeners()
        for track_index, track in enumerate(self.song.tracks):
            for scene_index, slot in enumerate(track.clip_slots):
                def has_clip_changed(slot=slot, track_index=track_index, scene_index=scene_index):
                    self.osc_server.send("/live/song/get/clip_slot_has_clip",
                                         (track_index, scene_index, bool(slot.has_clip)))
                slot.add_has_clip_listener(has_clip_changed)
                self.clip_listeners.append((slot, has_clip_changed))

    def _remove_clip_listeners(self):
        for slot, function in self.clip_listeners:
            try:
                slot.remove_has_clip_listener(function)
            except Exception as e:
                self.logger.info("Exception whilst removing clip listener (likely benign): %s" % e)
        self.clip_listeners = []

    def _send_clip_grid(self):
        tracks = list(self.song.tracks)
        values = [len(tracks), len(self.song.scenes)]
        for track in tracks:
            values += [bool(slot.has_clip) for slot in track.clip_slots]
        self.osc_server.send("/live/song/get/clip_grid", tuple(values))

    def clear_api(self):
        self._remove_clip_listeners()
        self.clip_grid_active = False
        super().clear_api()
