"""Content-free local call awareness; quiet is independent of recording permission."""
from __future__ import annotations


class Meeting:
    MAX_AGE = 3
    RELEASE_DELAY = 12
    REQUIRED_CHECKS = 3

    def __init__(self, service):
        self.service = service
        self.last_active = float('-inf')
        self.active_apps = []
        self.quiet = bool(service.config.get('audio_helper'))
        self.revision = 0
        self.state = 'checking' if self.quiet else 'not-configured'
        self.fresh = False
        self.input_streak = 0      # consecutive fresh checks with a microphone app: quiet needs three (3 s)
        self.last_sample_at = float('-inf')
        self.last_input_apps = ()
        self.automatic_quiet, self.automatic_state = self.quiet, self.state

    def update(self):
        now = self.service.clock()
        mode = self.service.store.get('meeting_mode', 'auto')
        health = getattr(self.service.inputs, 'audio_status', {})
        health = health if isinstance(health, dict) else {}
        at, activity = health.get('meeting_at'), health.get('meeting')
        self.fresh = (type(at) in (int, float) and 0 <= now-at <= self.MAX_AGE
                      and type(activity) is dict and activity.get('available') is True)
        inputs = activity.get('input_apps', []) if self.fresh else []
        outputs = activity.get('output_apps', []) if self.fresh else []
        valid = lambda values: isinstance(values, list) and all(isinstance(x, str) and len(x) <= 80 for x in values)
        if not valid(inputs) or not valid(outputs):
            self.fresh = False; inputs = outputs = []
        if not self.fresh:
            self.input_streak = 0
        elif at > self.last_sample_at:
            signature = tuple(sorted(set(inputs)))
            self.input_streak = (self.input_streak + 1 if signature == self.last_input_apps else 1) if inputs else 0
            self.last_input_apps = signature
            self.last_sample_at = at
        if self.fresh and inputs and self.input_streak >= self.REQUIRED_CHECKS:
            self.active_apps = inputs[:8]
            self.last_active = now
        # A muted input can stop while the same call continues to play audio.
        held_output = self.fresh and bool(set(self.active_apps) & set(outputs))
        recent = 0 <= now-self.last_active < self.RELEASE_DELAY
        if mode == 'quiet':
            quiet, state = True, 'manual-meeting'
        elif mode == 'available':
            quiet, state = False, 'manual-available'
        elif self.fresh and inputs and self.input_streak >= self.REQUIRED_CHECKS:
            quiet, state = True, 'external-microphone-active'
        elif held_output or recent:
            quiet, state = True, 'meeting-release-delay'
        elif self.fresh:
            quiet, state = False, 'available'
            self.active_apps = []
        elif self.service.config.get('audio_helper'):
            # DEC-066: unknown is not busy. Without the detector Butler stays available; the name still works either way.
            quiet, state = False, 'detector-unavailable'
        else:
            quiet, state = False, 'not-configured'
        self.automatic_quiet, self.automatic_state = quiet, state
        attention = getattr(self.service, "attention", None)
        manual = getattr(attention, "manual_mode", None)
        if manual == "free": quiet, state = False, 'manual-attention-free'
        elif manual == "busy": quiet, state = True, 'manual-attention-busy'
        changed_to_quiet = quiet and not self.quiet
        if quiet != self.quiet:
            self.revision += 1
        self.quiet, self.state = quiet, state
        if changed_to_quiet and manual != "busy":
            voice = self.service.voice
            if voice is not None:
                debug = getattr(voice, '_debug', None)
                if callable(debug):
                    debug('meeting-quiet', ('Тихий режим: микрофон занят — ' + ', '.join(self.active_apps[:3])) if self.active_apps
                          else 'Тихий режим включён: ' + state)
                if not getattr(voice, 'reply_in_progress', False) and getattr(voice, 'turn_committed', False) is not True:
                    # An admitted turn (the owner named Butler or was answered) is finished; only idle dialogue stops.
                    voice.cancel(clear_context=False, reason='meeting-started')
        return changed_to_quiet

    def snapshot(self):
        return {'quiet':self.quiet, 'state':self.state, 'fresh':self.fresh,
                'mode':self.service.store.get('meeting_mode', 'auto'),
                'apps':list(self.active_apps), 'revision':self.revision,
                'source':'CoreAudio process activity; no audio or browser content',
                'reply_rule':'explicit Butler-directed requests only' if self.quiet else 'contextual conversation'}
