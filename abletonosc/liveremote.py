from .handler import AbletonOSCHandler


class LiveRemoteHandler(AbletonOSCHandler):
    """
    Handlers added by LiveRemote. Kept apart from the upstream handlers so upstream
    changes merge cleanly.
    """

    # Version of this file's handlers; LiveService.requiredScriptVersion must match.
    SCRIPT_VERSION = 1

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
        self.global_listeners = []
        self.global_pending = False
        self.global_active = False
        self.global_poll_token = 0
        self.global_undo_state = (None, None)
        self.position_active = False
        self.last_position = None
        self.slot_listeners = []
        self.slot_grid_active = False
        self.slot_grid_pending = False
        self.slot_grid_batch = 0
        self.scene_listeners = []
        self.scene_data_active = False
        self.scene_data_pending = False
        self.scene_data_batch = 0
        self.scenes_function = None
        self.exclusive_arm_active = False

    def init_api(self):
        self.osc_server.add_handler("/live/song/get/track_snapshot", lambda params: self._snapshot())
        self.osc_server.add_handler("/live/song/start_listen/clip_grid", self._start_listen_clip_grid)
        self.osc_server.add_handler("/live/song/set/arm_exclusive", self._set_arm_exclusive)
        self.osc_server.add_handler("/live/song/start_listen/device_page", self._start_listen_device_page)
        self.osc_server.add_handler("/live/song/start_listen/tracks", self._start_listen_tracks)
        self.osc_server.add_handler("/live/song/stop_listen/tracks", self._stop_listen_tracks)
        self.osc_server.add_handler("/live/song/start_listen/global_state", self._start_listen_global_state)
        self.osc_server.add_handler("/live/song/stop_listen/global_state", self._stop_listen_global_state)
        self.osc_server.add_handler("/live/song/set/session_automation_record", self._set_session_automation_record)
        self.osc_server.add_handler("/live/song/set/scale_mode", self._set_scale_mode)
        self.osc_server.add_handler("/live/song/start_listen/position", self._start_listen_position)
        self.osc_server.add_handler("/live/song/stop_listen/position", self._stop_listen_position)
        self.osc_server.add_handler("/live/song/start_listen/slot_grid", self._start_listen_slot_grid)
        self.osc_server.add_handler("/live/song/stop_listen/slot_grid", self._stop_listen_slot_grid)
        self.osc_server.add_handler("/live/song/start_listen/scene_data", self._start_listen_scene_data)
        self.osc_server.add_handler("/live/song/stop_listen/scene_data", self._stop_listen_scene_data)
        self.osc_server.add_handler("/live/song/launch_scene", self._launch_scene)
        self.osc_server.add_handler("/live/song/launch_slot", self._launch_slot)
        # The app asks for this to tell whether Live still runs an older copy of this file,
        # which only a restart of Live replaces. Bump it with every change the app relies on.
        self.osc_server.add_handler("/live/song/get/script_version", lambda params: (self.SCRIPT_VERSION,))
        self.osc_server.add_handler("/live/song/get/exclusive_arm", lambda params: (bool(self.song.exclusive_arm),))
        self.osc_server.add_handler("/live/song/start_listen/exclusive_arm", self._start_listen_exclusive_arm)
        self.osc_server.add_handler("/live/song/stop_listen/exclusive_arm", self._stop_listen_exclusive_arm)

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

    # Global state (top bar) ---------------------------------------------------------

    GLOBAL_LISTENED = (
        "is_playing", "tempo", "signature_numerator", "signature_denominator", "metronome",
        "record_mode", "arrangement_overdub", "session_record", "session_record_status",
        "session_automation_record", "re_enable_automation_enabled", "can_capture_midi",
        "clip_trigger_quantization", "root_note", "scale_name", "scale_mode", "count_in_duration",
        "groove_amount",
    )

    def _global_values(self):
        """
        One flat list, in this order: is_playing, tempo, signature_numerator,
        signature_denominator, metronome, record_mode, arrangement_overdub, session_record,
        session_record_status, session_automation_record, re_enable_automation_enabled,
        can_capture_midi, can_undo, can_redo, clip_trigger_quantization, root_note,
        scale_name, scale_mode, count_in_duration, groove_amount.
        """
        song = self.song
        return (
            bool(song.is_playing),
            float(song.tempo),
            int(song.signature_numerator),
            int(song.signature_denominator),
            bool(song.metronome),
            bool(song.record_mode),
            bool(song.arrangement_overdub),
            bool(song.session_record),
            int(song.session_record_status),
            bool(song.session_automation_record),
            bool(song.re_enable_automation_enabled),
            bool(song.can_capture_midi),
            bool(song.can_undo),
            bool(song.can_redo),
            int(song.clip_trigger_quantization),
            int(song.root_note),
            str(song.scale_name),
            bool(song.scale_mode),
            int(song.count_in_duration),
            float(song.groove_amount),
        )

    def _queue_global(self):
        """
        Several properties often change in the same moment (Play also moves the record
        state). The listeners only queue, and one message with the whole state goes out on
        the next tick, so the top bar redraws at once.
        """
        if self.global_pending:
            return
        self.global_pending = True

        def flush():
            self.global_pending = False
            if not self.global_active:
                return
            self.global_undo_state = (bool(self.song.can_undo), bool(self.song.can_redo))
            self.osc_server.send("/live/song/get/global_state", self._global_values())

        self.manager.schedule_message(1, flush)

    def _poll_undo(self, token):
        """can_undo and can_redo have no listeners in Live: re-read them twice a second."""
        if token != self.global_poll_token or not self.global_active:
            return
        state = (bool(self.song.can_undo), bool(self.song.can_redo))
        if state != self.global_undo_state:
            self._queue_global()
        self.manager.schedule_message(5, lambda: self._poll_undo(token))

    def _start_listen_global_state(self, params):
        """
        /live/song/start_listen/global_state

        Replies on /live/song/get/global_state with 20 values (see _global_values), right
        away and again after any change, coalesced into one message per tick. Powers the
        top bar: transport, record and automation buttons, tempo, signature, Metronome,
        Undo and Redo, Global Quantization, Scale Mode and scale, count-in and groove amount.
        """
        self._remove_global_listeners()
        for name in self.GLOBAL_LISTENED:
            try:
                getattr(self.song, "add_%s_listener" % name)(self._queue_global)
                self.global_listeners.append(name)
            except Exception as e:
                self.logger.info("No listener for %s: %s" % (name, e))
        self.global_active = True
        self.global_poll_token += 1
        token = self.global_poll_token
        self.global_undo_state = (bool(self.song.can_undo), bool(self.song.can_redo))
        self.osc_server.send("/live/song/get/global_state", self._global_values())
        self.manager.schedule_message(5, lambda: self._poll_undo(token))

    def _stop_listen_global_state(self, params):
        self._remove_global_listeners()

    def _remove_global_listeners(self):
        self.global_active = False
        self.global_poll_token += 1
        for name in self.global_listeners:
            try:
                getattr(self.song, "remove_%s_listener" % name)(self._queue_global)
            except Exception as e:
                self.logger.info("Exception whilst removing global listener (likely benign): %s" % e)
        self.global_listeners = []

    def _set_session_automation_record(self, params):
        """/live/song/set/session_automation_record <0|1>  (Automation Arm)"""
        self.song.session_automation_record = bool(int(params[0]))

    def _set_scale_mode(self, params):
        """/live/song/set/scale_mode <0|1>  (Scale Mode on or off; the change comes back in global_state)"""
        self.song.scale_mode = bool(int(params[0]))

    def _position(self):
        t = self.song.get_current_beats_song_time()
        return (int(t.bars), int(t.beats))

    def _start_listen_position(self, params):
        """
        /live/song/start_listen/position

        Replies on /live/song/get/position with [bars, beats] right away and then once
        per new beat, also when the song time is set (for example back to 1, 1). Bars and
        beats count in the time signature's denominator, as Live's position display does.
        """
        self._stop_listen_position()

        def time_changed():
            position = self._position()
            if position != self.last_position:
                self.last_position = position
                self.osc_server.send("/live/song/get/position", position)

        self.position_function = time_changed
        self.song.add_current_song_time_listener(time_changed)
        self.position_active = True
        self.last_position = self._position()
        self.osc_server.send("/live/song/get/position", self.last_position)

    def _stop_listen_position(self, params=()):
        if self.position_active:
            try:
                self.song.remove_current_song_time_listener(self.position_function)
            except Exception as e:
                self.logger.info("Exception whilst removing position listener (likely benign): %s" % e)
        self.position_active = False
        self.last_position = None

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
        clip its start, end, position (beats) and looping, and last whether the playing slot
        records (7 values). start is -1 when nothing plays. start/end is the loop range when
        looping, else the start/end markers. The client animates the position itself from
        the tempo. A group records while any clip inside it records in the playing row.
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
            start, end, position, looping, recording = -1.0, 0.0, 0.0, False, False
            if playing >= 0:
                # A group follows the longest clip playing in its row.
                candidates = [track] if not track.is_foldable else [
                    other for other in tracks if self._is_in_group(other, track)]
                best = -1.0
                for other in candidates:
                    if int(other.playing_slot_index) != playing or not other.clip_slots[playing].has_clip:
                        continue
                    clip = other.clip_slots[playing].clip
                    if bool(clip.is_recording):
                        recording = True
                    is_looping = bool(clip.looping)
                    if is_looping:
                        c_start, c_end = float(clip.loop_start), float(clip.loop_end)
                    else:
                        c_start, c_end = float(clip.start_marker), float(clip.end_marker)
                    if c_end - c_start > best:
                        best = c_end - c_start
                        start, end, looping = c_start, c_end, is_looping
                        position = float(clip.playing_position)
            values += [playing, fired, start, end, position, looping, recording]
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
            if self.slot_grid_active:
                self._queue_slot_grid()

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

    # Batches ------------------------------------------------------------------------

    # macOS caps a UDP datagram at 9216 bytes (net.inet.udp.maxdgram). Larger state goes
    # out as a batch of messages that each stay under this many bytes.
    BATCH_LIMIT = 8000

    @staticmethod
    def _osc_size(value):
        """Bytes one argument takes in an OSC message, without its type tag."""
        if isinstance(value, str):
            return (len(value.encode("utf-8")) // 4 + 1) * 4
        return 4

    def _send_batched(self, address, batch_id, header, items):
        """
        Sends `items` (each a tuple of values) on `address` as one or more messages in the
        same tick. Each message is [batch_id, part, part_count, *header, first_item, then the
        values of its items]. The client keeps the parts of the newest batch_id and applies
        them once all part_count parts have arrived.
        """
        fixed = len(address) + 4 + (len(header) + 4) * 5 + 8
        parts, current, first, size = [], [], 0, fixed
        for index, item in enumerate(items):
            item_size = sum(self._osc_size(v) + 1 for v in item)
            if current and size + item_size > self.BATCH_LIMIT:
                parts.append((first, current))
                current, first, size = [], index, fixed
            current.append(item)
            size += item_size
        parts.append((first, current))
        for part, (first, chunk) in enumerate(parts):
            values = [batch_id, part, len(parts)] + list(header) + [first]
            for item in chunk:
                values += list(item)
            self.osc_server.send(address, tuple(values))

    # Scene list ---------------------------------------------------------------------

    def _update_scenes_listener(self):
        """One listener on the scene list serves both slot_grid and scene_data."""
        wanted = self.slot_grid_active or self.scene_data_active
        if wanted and self.scenes_function is None:
            def scenes_changed():
                if self.slot_grid_active:
                    self._queue_slot_grid()
                if self.scene_data_active:
                    self._queue_scene_data()
            self.scenes_function = scenes_changed
            self.song.add_scenes_listener(scenes_changed)
        elif not wanted and self.scenes_function is not None:
            try:
                self.song.remove_scenes_listener(self.scenes_function)
            except Exception as e:
                self.logger.info("Exception whilst removing scenes listener (likely benign): %s" % e)
            self.scenes_function = None

    # Slot grid ----------------------------------------------------------------------

    def _slot_values(self, track, slot):
        """kind (0 empty, 1 clip, 2 group slot that launches clips), has_stop_button, color, name."""
        stop = bool(slot.has_stop_button)
        if slot.has_clip:
            clip = slot.clip
            return (1, stop, int(clip.color), str(clip.name))
        if self._slot_launchable(track, slot):
            return (2, stop, int(slot.color), "")
        return (0, stop, -1, "")

    def _send_slot_grid(self):
        tracks = list(self.song.tracks)
        items = []
        for track in tracks:
            items += [self._slot_values(track, slot) for slot in track.clip_slots]
        self.slot_grid_batch += 1
        self._send_batched("/live/song/get/slot_grid", self.slot_grid_batch,
                           (len(tracks), len(self.song.scenes)), items)

    def _queue_slot_grid(self):
        """
        Listeners only queue. On the next tick the slot listeners are rebuilt (a slot may
        have gained or lost a clip) and one batch with the whole grid goes out.
        """
        if self.slot_grid_pending:
            return
        self.slot_grid_pending = True

        def flush():
            self.slot_grid_pending = False
            if not self.slot_grid_active:
                return
            self._rebuild_slot_listeners()
            self._send_slot_grid()

        self.manager.schedule_message(1, flush)

    def _listen_slot(self, target, name, queue):
        # Live may refuse one function twice on the same object, so each gets its own.
        function = lambda: queue()
        getattr(target, "add_%s_listener" % name)(function)
        self.slot_listeners.append((target, name, function))

    def _rebuild_slot_listeners(self):
        self._remove_slot_listeners()
        for track in self.song.tracks:
            for slot in track.clip_slots:
                self._listen_slot(slot, "has_clip", self._queue_slot_grid)
                self._listen_slot(slot, "has_stop_button", self._queue_slot_grid)
                if track.is_foldable:
                    self._listen_slot(slot, "controls_other_clips", self._queue_slot_grid)
                    self._listen_slot(slot, "color", self._queue_slot_grid)
                if slot.has_clip:
                    clip = slot.clip
                    self._listen_slot(clip, "name", self._queue_slot_grid)
                    self._listen_slot(clip, "color", self._queue_slot_grid)
                    # A recording that ends leaves playing_slot_index as it was.
                    self._listen_slot(clip, "is_recording", self._queue_playback)

    def _remove_slot_listeners(self):
        for target, name, function in self.slot_listeners:
            try:
                getattr(target, "remove_%s_listener" % name)(function)
            except Exception as e:
                self.logger.info("Exception whilst removing slot listener (likely benign): %s" % e)
        self.slot_listeners = []

    def _start_listen_slot_grid(self, params):
        """
        /live/song/start_listen/slot_grid

        Replies on /live/song/get/slot_grid with every clip slot of the audio, MIDI and group
        tracks (return tracks and Main are not in song.tracks), once right away and again
        after a slot gains or loses a clip or a stop button, a clip is renamed or recolored,
        or tracks or scenes change. Coalesced into one batch per tick.

        Sent as a batch (see _send_batched) with header [track_count, scene_count,
        first_slot] and 4 values per slot: kind (0 empty, 1 clip, 2 group slot that launches
        clips), has_stop_button, color (0xRRGGBB, -1 when empty) and name. Slots go track by
        track, scene by scene within a track.

        Also listens to Clip.is_recording on every clip, so track_playback goes out when a
        recording ends.
        """
        self.slot_grid_active = True
        self._update_scenes_listener()
        self._rebuild_slot_listeners()
        self._send_slot_grid()

    def _stop_listen_slot_grid(self, params=()):
        self.slot_grid_active = False
        self._remove_slot_listeners()
        self._update_scenes_listener()

    # Scene data ---------------------------------------------------------------------

    SCENE_LISTENED = ("name", "color", "tempo", "tempo_enabled", "time_signature_numerator",
                      "time_signature_denominator", "time_signature_enabled")

    def _scene_values(self, scene):
        """name, color, tempo (-1 when the scene has none), numerator and denominator (0, 0 when it has none)."""
        tempo = float(scene.tempo) if scene.tempo_enabled else -1.0
        if scene.time_signature_enabled:
            numerator, denominator = int(scene.time_signature_numerator), int(scene.time_signature_denominator)
        else:
            numerator, denominator = 0, 0
        return (str(scene.name), int(scene.color), tempo, numerator, denominator)

    def _send_scene_data(self):
        scenes = list(self.song.scenes)
        self.scene_data_batch += 1
        self._send_batched("/live/song/get/scene_data", self.scene_data_batch,
                           (len(scenes), int(self.song.master_track.color)),
                           [self._scene_values(scene) for scene in scenes])

    def _queue_scene_data(self):
        if self.scene_data_pending:
            return
        self.scene_data_pending = True

        def flush():
            self.scene_data_pending = False
            if not self.scene_data_active:
                return
            self._rebuild_scene_listeners()
            self._send_scene_data()

        self.manager.schedule_message(1, flush)

    def _rebuild_scene_listeners(self):
        self._remove_scene_listeners()
        # The Main track heads the Scene Launch buttons, in its own color.
        function = lambda: self._queue_scene_data()
        self.song.master_track.add_color_listener(function)
        self.scene_listeners.append((self.song.master_track, "color", function))
        for scene in self.song.scenes:
            for name in self.SCENE_LISTENED:
                # Live refuses one function twice on the same scene ("Observer already
                # connected"), so each property gets its own.
                function = lambda: self._queue_scene_data()
                getattr(scene, "add_%s_listener" % name)(function)
                self.scene_listeners.append((scene, name, function))
            # Sent right away, not coalesced: the app tells a click on a Scene Launch button
            # from a click on a clip by it, since both move the selected scene.
            function = (lambda scene: lambda: self._scene_triggered(scene))(scene)
            scene.add_is_triggered_listener(function)
            self.scene_listeners.append((scene, "is_triggered", function))

    def _scene_triggered(self, scene):
        scenes = list(self.song.scenes)
        if scene in scenes:
            self.osc_server.send("/live/song/get/scene_triggered", (scenes.index(scene),))

    def _remove_scene_listeners(self):
        for scene, name, function in self.scene_listeners:
            try:
                getattr(scene, "remove_%s_listener" % name)(function)
            except Exception as e:
                self.logger.info("Exception whilst removing scene listener (likely benign): %s" % e)
        self.scene_listeners = []

    def _start_listen_scene_data(self, params):
        """
        /live/song/start_listen/scene_data

        Replies on /live/song/get/scene_data with every scene, once right away and again
        after a scene is renamed, recolored, gets or loses its own tempo or time signature,
        scenes are added, deleted or moved, or the Main track is recolored. Coalesced into one batch per tick.

        Sent as a batch (see _send_batched) with header [scene_count, main_color, first_scene]
        and 5 values per scene: name, color, tempo (-1 when the scene has no tempo of its own),
        time signature numerator and denominator (0 and 0 when it has none).

        Also replies on /live/song/get/scene_triggered <scene_index> each time a scene is
        launched or its launch takes effect (Scene.is_triggered changes). A click on a clip
        does not trigger its scene, so this tells the two apart.
        """
        self.scene_data_active = True
        self._update_scenes_listener()
        self._rebuild_scene_listeners()
        self._send_scene_data()

    def _stop_listen_scene_data(self, params=()):
        self.scene_data_active = False
        self._remove_scene_listeners()
        self._update_scenes_listener()

    def _launch_scene(self, params):
        """
        /live/song/launch_scene <scene_index>

        Launches the scene as a click on its Scene Launch button in Live does: the scene is
        selected only when Live's "Select on Launch" preference is on, and "Select Next Scene
        on Launch" does not apply (in Live that is for the Enter key). fire_as_selected is
        not used: it always moves on to the next scene.
        """
        scene = self.song.scenes[int(params[0])]
        if self.song.select_on_launch:
            self.song.view.selected_scene = scene
        scene.fire(False, False)  # force_legato, can_select_scene_on_launch

    def _launch_slot(self, params):
        """
        /live/song/launch_slot <track_index> <scene_index>

        Fires a clip slot as a click on its button in Live does. Launching a clip or a group
        slot selects its track and scene when Live's "Select on Launch" preference is on.
        A stop button or an empty slot (recording on an armed track) leaves the selection
        alone.
        """
        track = self.song.tracks[int(params[0])]
        scene = self.song.scenes[int(params[1])]
        slot = track.clip_slots[int(params[1])]
        if self.song.select_on_launch and (slot.has_clip or slot.controls_other_clips):
            self.song.view.selected_track = track
            self.song.view.selected_scene = scene
        slot.fire()

    # Exclusive Arm preference -------------------------------------------------------

    def _exclusive_arm_changed(self):
        self.osc_server.send("/live/song/get/exclusive_arm", (bool(self.song.exclusive_arm),))

    def _start_listen_exclusive_arm(self, params):
        """
        /live/song/start_listen/exclusive_arm

        Replies on /live/song/get/exclusive_arm [bool] with Live's Exclusive Arm preference,
        right away and again whenever it is toggled in Live's Preferences.
        """
        self._stop_listen_exclusive_arm()
        self.song.add_exclusive_arm_listener(self._exclusive_arm_changed)
        self.exclusive_arm_active = True
        self._exclusive_arm_changed()

    def _stop_listen_exclusive_arm(self, params=()):
        if self.exclusive_arm_active:
            try:
                self.song.remove_exclusive_arm_listener(self._exclusive_arm_changed)
            except Exception as e:
                self.logger.info("Exception whilst removing exclusive arm listener (likely benign): %s" % e)
        self.exclusive_arm_active = False

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
        self._stop_listen_slot_grid()
        self._stop_listen_scene_data()
        self._stop_listen_exclusive_arm()
        self._remove_global_listeners()
        self._stop_listen_position()
        self._remove_clip_listeners()
        self._remove_playback_listeners()
        self._remove_mixer_listeners()
        self._remove_device_listeners()
        self.device_page_active = False
        self.clip_grid_active = False
        super().clear_api()
