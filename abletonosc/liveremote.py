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
        self.device_listeners = []
        self.device_page_active = False
        self.playback_listeners = []
        self.playback_pending = False
        self.mixer_listeners = []
        self.mixer_pending = False

    def init_api(self):
        self.osc_server.add_handler("/live/song/get/track_snapshot", lambda params: self._snapshot())
        self.osc_server.add_handler("/live/song/start_listen/clip_grid", self._start_listen_clip_grid)
        self.osc_server.add_handler("/live/song/set/arm_exclusive", self._set_arm_exclusive)
        self.osc_server.add_handler("/live/song/start_listen/device_page", self._start_listen_device_page)
        self.osc_server.add_handler("/live/song/start_listen/tracks", self._start_listen_tracks)
        self.osc_server.add_handler("/live/song/stop_listen/tracks", self._stop_listen_tracks)

    def _set_arm_exclusive(self, params):
        """
        /live/song/set/arm_exclusive <track_index>

        Arms the track and disarms every other track, as selecting a track does on Push.
        Tracks that cannot be armed (group tracks) are left alone. The arm listeners
        report the result.
        """
        index = int(params[0])
        for i, track in enumerate(self.song.tracks):
            if track.can_be_armed:
                track.arm = (i == index)

    # Mixer state (pan and sends) ----------------------------------------------------

    def _mixer_values(self):
        """
        [send_count, then per track: panning, send 1 ... send N]. Sent as one message for
        all tracks, like the playback state, so the mixer redraws at once.
        """
        send_count = len(self.song.return_tracks)
        values = [send_count]
        for track in self.song.tracks:
            values.append(float(track.mixer_device.panning.value))
            sends = track.mixer_device.sends
            for i in range(send_count):
                values.append(float(sends[i].value) if i < len(sends) else 0.0)
        return tuple(values)

    def _queue_mixer(self):
        if self.mixer_pending:
            return
        self.mixer_pending = True

        def flush():
            self.mixer_pending = False
            self.osc_server.send("/live/song/get/track_mixer", self._mixer_values())

        self.manager.schedule_message(1, flush)

    def _rebuild_mixer_listeners(self):
        self._remove_mixer_listeners()
        parameters = []
        for track in self.song.tracks:
            parameters.append(track.mixer_device.panning)
            parameters.extend(track.mixer_device.sends)
        for parameter in parameters:
            parameter.add_value_listener(self._queue_mixer)
            self.mixer_listeners.append(parameter)

    def _remove_mixer_listeners(self):
        for parameter in self.mixer_listeners:
            try:
                parameter.remove_value_listener(self._queue_mixer)
            except Exception as e:
                self.logger.info("Exception whilst removing mixer listener (likely benign): %s" % e)
        self.mixer_listeners = []

    # Playback state ----------------------------------------------------------------

    def _playback_values(self):
        """
        Per track, in track order: playing_slot_index, fired_slot_index, then for the playing
        clip its start, end, position (beats) and looping. start is -1 when nothing plays.
        start/end is the loop range when looping, else the start/end markers. The client
        animates the position itself from the tempo.
        """
        values = []
        tracks = list(self.song.tracks)
        for track in tracks:
            playing = int(track.playing_slot_index)
            fired = int(track.fired_slot_index)
            if track.is_foldable:
                # A group slot plays and queues with the clips inside the group.
                for other in tracks:
                    if self._is_in_group(other, track):
                        if playing < 0 and int(other.playing_slot_index) >= 0:
                            playing = int(other.playing_slot_index)
                        if fired < 0 and int(other.fired_slot_index) >= 0:
                            fired = int(other.fired_slot_index)
            start, end, position, looping = -1.0, 0.0, 0.0, False
            if playing >= 0:
                # A group follows the longest clip playing in its row.
                candidates = [track] if not track.is_foldable else [
                    other for other in tracks if self._is_in_group(other, track)]
                best = -1.0
                for other in candidates:
                    if int(other.playing_slot_index) != playing or not other.clip_slots[playing].has_clip:
                        continue
                    clip = other.clip_slots[playing].clip
                    is_looping = bool(clip.looping)
                    if is_looping:
                        c_start, c_end = float(clip.loop_start), float(clip.loop_end)
                    else:
                        c_start, c_end = float(clip.start_marker), float(clip.end_marker)
                    if c_end - c_start > best:
                        best = c_end - c_start
                        start, end, looping = c_start, c_end, is_looping
                        position = float(clip.playing_position)
            values += [playing, fired, start, end, position, looping]
        return tuple(values)

    @staticmethod
    def _is_in_group(track, group):
        """True when `track` sits anywhere inside the group track `group`."""
        parent = track.group_track
        while parent is not None:
            if parent == group:
                return True
            parent = parent.group_track
        return False

    @staticmethod
    def _slot_launchable(track, slot):
        """A slot with a clip, or a group slot that launches the clips inside the group."""
        if slot.has_clip:
            return True
        try:
            return bool(track.is_foldable and slot.controls_other_clips)
        except Exception:
            return False

    def _queue_playback(self):
        """
        Launching a scene changes the playing clip of every track in the same moment. One
        reply per track would reach the client one OSC tick (100 ms) apart and the grid
        would update cell by cell. So the listeners only queue, and one message with all
        tracks goes out on the next tick.
        """
        if self.playback_pending:
            return
        self.playback_pending = True

        def flush():
            self.playback_pending = False
            self.osc_server.send("/live/song/get/track_playback", self._playback_values())

        self.manager.schedule_message(1, flush)

    def _rebuild_playback_listeners(self):
        self._remove_playback_listeners()
        for track in self.song.tracks:
            for name in ("playing_slot_index", "fired_slot_index"):
                add = getattr(track, "add_%s_listener" % name)
                add(self._queue_playback)
                self.playback_listeners.append((track, name))

    def _remove_playback_listeners(self):
        for track, name in self.playback_listeners:
            try:
                getattr(track, "remove_%s_listener" % name)(self._queue_playback)
            except Exception as e:
                self.logger.info("Exception whilst removing playback listener (likely benign): %s" % e)
        self.playback_listeners = []

    def _snapshot(self):
        """
        /live/song/get/track_snapshot

        One flat reply with 10 values per track, in track order: name, color, volume, mute,
        solo, arm, playing_slot_index, fired_slot_index, is_group, group_root (index of the
        outermost group track this track belongs to, itself when it is one, else -1). Lets a client redraw the whole
        mixer at once instead of waiting for one listener reply per property.
        """
        values = []
        tracks = list(self.song.tracks)
        for track in tracks:
            root = track
            while root.group_track is not None:
                root = root.group_track
            group_root = next((i for i, t in enumerate(tracks) if t == root), -1) \
                if (root is not track or track.is_foldable) else -1
            values += [
                track.name,
                track.color,
                float(track.mixer_device.volume.value),
                bool(track.mute),
                bool(track.solo),
                bool(track.arm) if track.can_be_armed else False,
                int(track.playing_slot_index),
                int(track.fired_slot_index),
                bool(track.is_foldable),
                group_root,
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
            self._rebuild_playback_listeners()
            self.osc_server.send("/live/song/get/track_playback", self._playback_values())
            self._rebuild_mixer_listeners()
            self.osc_server.send("/live/song/get/track_mixer", self._mixer_values())
            if self.clip_grid_active:
                self._rebuild_clip_listeners()
                self._send_clip_grid()

        self.song.add_tracks_listener(tracks_changed)
        self.listener_functions[key] = tracks_changed
        self.listener_objects[key] = self.song
        self.osc_server.send("/live/song/get/tracks", (len(self.song.tracks),))
        self.osc_server.send("/live/song/get/track_snapshot", self._snapshot())
        self._rebuild_playback_listeners()
        self.osc_server.send("/live/song/get/track_playback", self._playback_values())
        self._rebuild_mixer_listeners()
        self.osc_server.send("/live/song/get/track_mixer", self._mixer_values())

    def _stop_listen_tracks(self, params):
        self._stop_listen(self.song, "tracks", ())
        self._remove_playback_listeners()
        self._remove_mixer_listeners()

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
                def has_clip_changed(slot=slot, track=track, track_index=track_index, scene_index=scene_index):
                    self.osc_server.send("/live/song/get/clip_slot_has_clip",
                                         (track_index, scene_index, self._slot_launchable(track, slot)))
                slot.add_has_clip_listener(has_clip_changed)
                self.clip_listeners.append((slot, "has_clip", has_clip_changed))
                if track.is_foldable:
                    slot.add_controls_other_clips_listener(has_clip_changed)
                    self.clip_listeners.append((slot, "controls_other_clips", has_clip_changed))

    def _remove_clip_listeners(self):
        for slot, name, function in self.clip_listeners:
            try:
                getattr(slot, "remove_%s_listener" % name)(function)
            except Exception as e:
                self.logger.info("Exception whilst removing clip listener (likely benign): %s" % e)
        self.clip_listeners = []

    def _send_clip_grid(self):
        tracks = list(self.song.tracks)
        values = [len(tracks), len(self.song.scenes)]
        for track in tracks:
            values += [self._slot_launchable(track, slot) for slot in track.clip_slots]
            if track.is_foldable:
                self.logger.info("Group track %s: launchable slots %s" % (
                    track.name, [self._slot_launchable(track, slot) for slot in track.clip_slots]))
        self.osc_server.send("/live/song/get/clip_grid", tuple(values))

    # Device page --------------------------------------------------------------------

    def _start_listen_device_page(self, params):
        """
        /live/song/start_listen/device_page

        Follows the track selected in Live and sends, once right away and again on every change:

        /live/song/get/device_chain [track, selected_device, count, (name, class_name, type, is_on) x count]
            The devices of the selected track. `track` is -1 for return and master tracks,
            which are not in song.tracks (the chain is empty then).
        /live/song/get/device_params [track, device, count, (name, value, min, max, is_quantized, value_string) x count]
            Every parameter of the selected device, in Live's own order (a "Device On" parameter
            comes first). Positions are the indices to use when setting values.
        /live/song/get/device_param_value [track, device, param, value, value_string]
            One parameter changed. `param` is the position in device_params.

        Everything is re-listened inside Live whenever the selection or a chain changes, so
        indices never go stale. Set a value with /live/device/set/parameter/value, using the
        parameter's position in device_params.
        """
        self.device_page_active = True
        key = ("selected_track_device_page", ())
        if key in self.listener_functions:
            self._stop_listen(self.song.view, "selected_track", ())
            self.listener_functions.pop(key, None)
            self.listener_objects.pop(key, None)

        def selected_track_changed():
            self._rebuild_device_page()

        self.song.view.add_selected_track_listener(selected_track_changed)
        self.listener_functions[key] = selected_track_changed
        self.listener_objects[key] = self.song.view
        self._rebuild_device_page()

    @staticmethod
    def _is_device_on_parameter(device):
        parameters = device.parameters
        return len(parameters) > 0 and parameters[0].name == "Device On"

    def _watch(self, add_listener, remove_listener, function):
        add_listener(function)
        self.device_listeners.append((remove_listener, function))

    def _remove_device_listeners(self):
        for remove_listener, function in self.device_listeners:
            try:
                remove_listener(function)
            except Exception as e:
                self.logger.info("Exception whilst removing device listener (likely benign): %s" % e)
        self.device_listeners = []

    def _rebuild_device_page(self):
        self._remove_device_listeners()
        track = self.song.view.selected_track
        tracks = list(self.song.tracks)
        track_index = tracks.index(track) if track in tracks else -1
        devices = list(track.devices) if track_index >= 0 else []
        selected = track.view.selected_device if track_index >= 0 else None
        selected_index = devices.index(selected) if selected in devices else -1

        if track_index >= 0:
            self._watch(track.add_devices_listener, track.remove_devices_listener, self._rebuild_device_page)
            self._watch(track.view.add_selected_device_listener, track.view.remove_selected_device_listener,
                        self._rebuild_device_page)
            for device in devices:
                if self._is_device_on_parameter(device):
                    self._watch(device.parameters[0].add_value_listener, device.parameters[0].remove_value_listener,
                                self._send_device_chain)

        self._device_page_state = (track_index, selected_index)
        self._send_device_chain()

        if selected_index >= 0:
            parameters = list(selected.parameters)
            for position, parameter in enumerate(parameters):
                def value_changed(parameter=parameter, position=position):
                    self.osc_server.send("/live/song/get/device_param_value",
                                         (track_index, selected_index, position, float(parameter.value),
                                          parameter.str_for_value(parameter.value)))
                self._watch(parameter.add_value_listener, parameter.remove_value_listener, value_changed)
            self._send_device_params(track_index, selected_index, parameters)
        else:
            self._send_device_params(track_index, -1, [])

    def _send_device_chain(self):
        track_index, selected_index = self._device_page_state
        values = [track_index, selected_index]
        devices = []
        if track_index >= 0:
            devices = list(self.song.tracks[track_index].devices)
        values.append(len(devices))
        for device in devices:
            is_on = bool(device.parameters[0].value) if self._is_device_on_parameter(device) else True
            values += [device.name, device.class_name, int(device.type), is_on]
        self.osc_server.send("/live/song/get/device_chain", tuple(values))

    def _send_device_params(self, track_index, device_index, parameters):
        values = [track_index, device_index, len(parameters)]
        for parameter in parameters:
            values += [parameter.name, float(parameter.value), float(parameter.min), float(parameter.max),
                       bool(parameter.is_quantized), parameter.str_for_value(parameter.value)]
        self.osc_server.send("/live/song/get/device_params", tuple(values))

    def clear_api(self):
        self._remove_clip_listeners()
        self._remove_playback_listeners()
        self._remove_mixer_listeners()
        self._remove_device_listeners()
        self.device_page_active = False
        self.clip_grid_active = False
        super().clear_api()
