"""Local owner controls and a separate read-only MCP surface."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import signal
from . import processes as subprocess
import sys
import time
from pathlib import Path

from . import mcp
from .analysis import LocalAnalyzer, DEFAULT_DIALOGUE_MODEL
from .service import RPCServer, Service
from .store import Store
from .summaries import summarize


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, default=Path.home() / ".local/share/butler-ambient")
    p.add_argument("--config", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("run", "mcp", "status", "pause", "resume", "doctor", "screen-permission", "ui-state", "ui-local-access-status", "ui-visual-cancel", "ui-settings", "ui-voice-check", "ui-voice-benchmark", "ui-video-effects", "ui-owner-setup", "ui-owner-setup-cancel", "ui-components", "ui-schedules", "ui-catalog", "ui-catalog-refresh", "ui-feedback", "ui-panel-chat-clear", "ui-display-clear"):
        item = sub.add_parser(command)
        if command == "ui-state":
            item.add_argument("--preview", action="store_true")
    observe = sub.add_parser("observe")
    observe.add_argument("source", choices=("camera", "screen"))
    local_observe = sub.add_parser("ui-observe")
    local_observe.add_argument("source", choices=("camera", "screen"))
    for command in ("voice-preview", "voice-select"):
        voice = sub.add_parser(command)
        voice.add_argument("profile", choices=("calm", "baritone", "warm"))
    for command in ("ui-material-add", "ui-material-remove"):
        sub.add_parser(command).add_argument("path")
    sub.add_parser("ui-visual-delete").add_argument("id")
    sub.add_parser("ui-guest-setup").add_argument("name")
    visual = sub.add_parser("ui-visual-request")
    visual.add_argument("intent", choices=("show-camera", "show-screen", "find-image", "generate-image"))
    visual.add_argument("criteria", nargs="?", default="")
    visual.add_argument("--telegram", action="store_true")
    summary = sub.add_parser("summary")
    summary.add_argument("start", type=float)
    summary.add_argument("end", type=float)
    summary.add_argument("--local-details", action="store_true")
    enroll = sub.add_parser("enroll")
    enroll.add_argument("id")
    enroll.add_argument("name")
    enroll.add_argument("--photo", type=Path, required=True)
    enroll.add_argument("--voice", type=Path, required=True)
    enroll.add_argument("--prior-agreement", action="store_true", required=True)
    live = sub.add_parser("enroll-live")
    live.add_argument("id")
    live.add_argument("name")
    live.add_argument("--prior-agreement", action="store_true", required=True)
    live.add_argument("--multiple-samples", action="store_true", help="explicit setup with several local face/voice templates")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("id")
    context = sub.add_parser("context")
    context.add_argument("id")
    context.add_argument("name")
    context.add_argument("description")
    context.add_argument("--kind", choices=("person", "household", "dog"), default="person")
    for command in ("preserve", "delete"):
        item = sub.add_parser(command)
        item.add_argument("id")
        if command == "delete":
            item.add_argument("--results", action="store_true")
    correct = sub.add_parser("correct")
    correct.add_argument("subject")
    correct.add_argument("name")
    preferences = sub.add_parser("preferences")
    preferences.add_argument("json", help='e.g. {"muted":true,"disabled_topics":["reading"],"daily_time":"20:00"}')
    for command in ("ui-single-user", "ui-settings-update", "ui-components-update", "ui-schedule-control", "ui-display-select", "ui-display-action", "ui-feedback-correct", "ui-rule", "ui-attention", "ui-send-text", "ui-dictate", "ui-observations", "ui-recordings", "ui-recording-control", "ui-recording-meeting", "ui-recording-play", "ui-history-summary"):
        sub.add_parser(command).add_argument("json")
    return p


def doctor(config):
    from . import __version__
    local = LocalAnalyzer(config.get("vision_model", "gemma3:4b"))
    try:
        local.check_local(require_vision=True)
        vision_ready = True
    except Exception:
        vision_ready = False
    dialogue=LocalAnalyzer(config.get('conversation_model',DEFAULT_DIALOGUE_MODEL), dialogue=True)
    try:
        dialogue.check_local()
        dialogue_ready=True
    except Exception:
        dialogue_ready=False
    return {"version": __version__, "commands": {key: shutil.which(key) for key in ("ffmpeg", "whisper-cli", "ollama", "openclaw")},
            "sensing_packages": {key: bool(importlib.util.find_spec(key)) for key in ("numpy", "cv2", "resemblyzer", "webrtcvad")},
            "models": {key: bool(config.get(key) and Path(config[key]).is_file())
                       for key in ("face_detector", "face_recognizer", "whisper_model", "person_detector")},
            "vision_model": config.get("vision_model", "gemma3:4b"),
            "local_vision_ready": vision_ready,
            "conversation_model": dialogue.model,
            "local_conversation_ready": dialogue_ready,
            "note": "No devices accessed. Enrollment, local models and macOS permissions are required; failures pause capture."}


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    config = json.loads(args.config.read_text()) if args.config else {}
    try:
        if args.command == "mcp":
            mcp.serve(args.state)
            return
        if args.command == "doctor":
            result = doctor(config)
        elif args.command == "run":
            from .sensing import Inputs, Verifier
            # Acquire the single-instance lock before starting any devices.
            store = Store(args.state, spool_limit_mb=config.get("spool_limit_mb"))
            analyzer = LocalAnalyzer(config.get("vision_model", "gemma3:4b"), config.get("whisper_model"),
                whisper_language=config.get("speech_language", "ru"), whisper_cli=config.get("whisper_cli", "whisper-cli"))
            inputs = Inputs(config.get("camera", "0"), config.get("microphone", "0"),
                            audio_helper=config.get("audio_helper"), voice_root=args.state / "voice",
                            expected_microphone=config.get("expected_microphone", "Jabra SPEAK"),camera_size=config.get("camera_size","640x480"),
                            ffmpeg_cli=config.get("ffmpeg_cli","ffmpeg"))
            service = Service(store, inputs, None, analyzer, config)
            service.restart = lambda: os.kill(os.getpid(), signal.SIGTERM)
            server = RPCServer(service)
            panel = None
            try:
                try:
                    service.verifier = Verifier(config["face_detector"], config["face_recognizer"], config.get("person_detector"),voice_model=config.get("voice_identity_model"))
                except Exception:
                    # Controls and expiration remain available even without local sensing dependencies.
                    from .policy import Evidence
                    class UnavailableVerifier:
                        unavailable = True
                        def verify(self, batch, references):
                            return Evidence(batch.end, False, False)
                    service.verifier = UnavailableVerifier()
                def stop(*_):
                    raise KeyboardInterrupt
                signal.signal(signal.SIGTERM, stop)
                from .catalog import Catalog
                from .components import WORKSPACE, constants, owner_routing
                defaults = constants(WORKSPACE / "lib/dispatch.py")
                executables = {"claude": defaults.get("CLAUDE_BIN",shutil.which("claude") or "claude"),
                    "codex": next((p for p in defaults.get("CODEX_BIN_CANDIDATES",[]) if shutil.which(p)),shutil.which("codex") or "codex")}
                executables.update(owner_routing(WORKSPACE).get("executables",{}))
                service.catalog = Catalog(WORKSPACE,executables,runtime_cli=config.get("openclaw_cli",shutil.which("openclaw") or "openclaw"))
                service.catalog.refresh()
                service.start()
                if config.get("panel_helper"):
                    service.panel_launch = lambda: subprocess.Popen([config["panel_helper"], str(config["owner_cli"]),
                                                                    *(["--background"] if config.get("panel_start_hidden") else [])],
                                                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    panel = service.panel = service.panel_launch()
                server.serve_forever(poll_interval=.2)
            except KeyboardInterrupt:
                pass
            finally:
                panel = getattr(service, "panel", panel) or panel    # the service may have relaunched it
                if panel is not None and panel.poll() is None:
                    panel.terminate()
                    try:
                        panel.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        panel.kill()
                        panel.wait()
                service.stop()
                server.server_close()
                store.close()
            return
        elif args.command in {"enroll", "enroll-live", "context", "summary"}:
            store = Store(args.state)
            try:
                if args.command == "enroll-live":
                    from .enrollment import enroll_live
                    multiple = args.multiple_samples or bool(config.get("enrollment_multiple_samples", False))
                    if config.get("enrollment_ready_dialog", False):
                        instruction = ('Выключите постороннюю речь и звук из колонок. После нажатия «Начать» говорите обычным голосом 15 секунд, смотрите в камеру и немного поворачивайте голову. Перед камерой должны быть только вы. Сохраняются только локальные эталоны, исходные фото и звук удаляются.'
                                       if multiple else 'После нажатия «Начать» смотрите в камеру и говорите обычным голосом 15 секунд. Перед камерой должны быть только вы. Исходные фото и звук не сохраняются.')
                        subprocess.run(["/usr/bin/osascript", "-e",
                            'display dialog ' + json.dumps(instruction, ensure_ascii=False) + ' with title "Регистрация владельца Butler" buttons {"Отмена", "Начать"} default button "Начать" cancel button "Отмена"'], check=True)
                    result = enroll_live(store, config, args.id, args.name, int(config.get("enrollment_seconds", 10)),
                                         multiple_samples=multiple)
                elif args.command == "enroll":
                    # Enrollment uses owner-selected existing reference media, never paused sensing.
                    import wave
                    from .sensing import Verifier
                    verifier = Verifier(config["face_detector"], config["face_recognizer"])
                    with wave.open(str(args.voice), "rb") as voice:
                        if (voice.getnchannels(), voice.getsampwidth(), voice.getframerate()) != (1, 2, 16000):
                            raise ValueError("reference voice must be 16 kHz mono PCM16 WAV")
                        pcm = voice.readframes(160000)
                    reference = verifier.enroll(args.photo.read_bytes(), pcm)
                    store.execute("INSERT OR REPLACE INTO agreements VALUES (?,?,1,?)", (args.id, args.name, json.dumps(reference)))
                    result = {"enrolled": args.id, "agreement": True}
                elif args.command == "context":
                    store.execute("INSERT OR REPLACE INTO identities VALUES (?,?,?,?)", (args.id, args.name, args.description, args.kind))
                    result = {"context_added": args.id, "basis": "owner-supplied", "participant_authorization": False}
                else:
                    result = summarize(store, args.start, args.end, local=args.local_details)
            finally:
                store.close()
        else:
            command = args.command
            values = vars(args)
            if command in {"pause", "resume"}:
                command, values = "enabled", {"value": command == "resume"}
            elif command in {"preferences", "ui-single-user", "ui-settings-update", "ui-components-update", "ui-schedule-control", "ui-display-select", "ui-display-action", "ui-feedback-correct", "ui-rule", "ui-attention", "ui-send-text", "ui-dictate", "ui-observations", "ui-recordings", "ui-recording-control", "ui-recording-meeting", "ui-recording-play", "ui-history-summary"}:
                values = json.loads(args.json)
            elif command == "observe":
                values = {"source": args.source}
            else:
                values = {k: v for k, v in values.items() if k not in {"state", "config", "command"}}
            result = mcp.rpc(args.state, command, values)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        if args.command == "enroll-live" and isinstance(error, ValueError):
            print("Enrollment unavailable: " + str(error), file=sys.stderr)
            raise SystemExit(1) from None
        print("Ambient request failed: source, local dependency, service or policy unavailable. See doctor/status.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
