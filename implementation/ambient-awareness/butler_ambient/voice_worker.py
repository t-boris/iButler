"""Isolated local MLX speech worker; never reads owner enrollment references."""
from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import queue
import uuid
from pathlib import Path


DEMO = ("Здравствуйте, Борис. Это образец моего голоса. Я могу кратко отвечать на вопросы, "
        "объяснять увиденное и говорить с вами спокойно, естественно, без спешки.")
PROFILES = [
    ("calm", "Спокойный мужской", "A native Russian male voice in his late thirties, warm clear mid-low register, relaxed conversational intonation, precise Russian pronunciation, moderate natural pace, attentive and friendly, no announcer delivery."),
    ("baritone", "Тёплый баритон", "A native Russian adult male voice with a warm soft baritone, rich but not booming, composed and friendly, subtle expressive conversational intonation, clear native Russian pronunciation, natural moderate pace, not theatrical."),
    ("warm", "Тёплый женский", "A native Russian woman in her early thirties, warm clear medium register, calm friendly natural conversation, expressive but restrained intonation, precise native Russian pronunciation, moderate natural pace, not an announcer."),
]


def write_audio(results, path):
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    chunks, rate = [], None
    for result in results:
        chunks.append(result.audio)
        rate = result.sample_rate
    if not chunks:
        raise ValueError("no local audio")
    audio = np.asarray(mx.concatenate(chunks))
    if not rate or len(audio) == 0 or len(audio) > rate * 90 or not np.isfinite(audio).all():
        raise ValueError("invalid local audio")
    sf.write(str(path), audio, rate, subtype="PCM_16")
    path.chmod(0o600)


def previews(root):
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model
    destination = root / "previews"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    model = load_model(str(root / "design-model"))
    profiles = []
    for index, (key, name, style) in enumerate(PROFILES):
        mx.random.seed(100 + index)
        path = destination / (key + ".wav")
        write_audio(model.generate_voice_design(text=DEMO, instruct=style, language="russian",
                    max_tokens=400, verbose=False), path)
        profiles.append({"id": key, "name": name, "file": str(path), "text": DEMO, "style": style})
        print("Preview ready: " + key, flush=True)
    temporary = root / "profiles.json.tmp"
    temporary.write_text(json.dumps({"synthetic": True, "profiles": profiles}, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(root / "profiles.json")


def serve(root, model_path=None):
    # Runtime is offline even when the public model installer has network access.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    from mlx_audio.tts.utils import load_model
    with contextlib.redirect_stdout(sys.stderr):
        model = load_model(str(model_path or root / "base-model"))
    profiles = {p["id"]: p for p in json.loads((root / "profiles.json").read_text())["profiles"]}
    output = root / "output"
    output.mkdir(exist_ok=True, mode=0o700)
    # Keep weights and the synthetic reference cache resident between turns.
    # The input reader remains responsive while MLX is generating: cancellation
    # ends only this utterance, not the model process and its warm caches.
    requests = queue.Queue(maxsize=2)
    cancelled = threading.Event()
    credit = threading.Event()
    active = {"id": None}
    write_lock = threading.Lock()
    protocol = sys.stdout
    aborted = set()
    outputs = {}

    def emit(value):
        with write_lock:
            print(json.dumps(value), file=protocol, flush=True)

    def generate():
      while True:
        request = requests.get()
        if request is None: return
        files = []
        request_id = request.get("id")
        if request_id is not None: outputs[request_id] = files
        try:
            cancelled.clear()
            active["id"] = request_id
            if request_id in aborted:
                cancelled.set()
                emit({"id": request_id, "done": True, "cancelled": True})
                continue
            profile = profiles[request["profile"]]
            text = request["text"]
            if not isinstance(text, str) or not 1 <= len(text) <= 700:
                raise ValueError("invalid utterance")
            streaming = request.get("stream") is True
            with contextlib.redirect_stdout(sys.stderr):
                results = model.generate(text=text, ref_audio=profile["file"], ref_text=profile["text"],
                            lang_code="russian", max_tokens=700, verbose=False,
                            stream=streaming, streaming_interval=0.48)
                if not streaming:
                    path = output / (uuid.uuid4().hex + ".wav")
                    write_audio(results, path)
                    files.append(path)
                else:
                    # At most one unacknowledged chunk in the pipe. Parent
                    # grants credit only while its native playback queue is bounded.
                    for sequence, result in enumerate(results):
                        if cancelled.is_set(): break
                        path = output / (uuid.uuid4().hex + ".wav")
                        write_audio([result], path)
                        files.append(path)
                        credit.clear()
                        emit({"id": request_id, "file": str(path), "sequence": sequence})
                        while not credit.wait(.05):
                            if cancelled.is_set(): break
                        if cancelled.is_set(): break
            if streaming:
                emit({"id": request_id, "done": True, "cancelled": cancelled.is_set()})
            else:
                emit({"file": str(path)})
        except Exception:
            cancelled.set()
            emit({"id": request_id, "error": "local-voice-unavailable"})
        finally:
            if cancelled.is_set():
                for path in files: path.unlink(missing_ok=True)
            active["id"] = None

    threading.Thread(target=generate, daemon=True).start()
    emit({"ready": True})
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if request.get("command") in {"cancel", "release"}:
                aborted.add(request.get("id"))
                if len(aborted) > 128: aborted.intersection_update({active["id"], request.get("id")})
                if active["id"] == request.get("id"):
                    cancelled.set()
                    credit.set()
                for path in outputs.pop(request.get("id"), []): path.unlink(missing_ok=True)
            elif request.get("command") == "credit":
                if active["id"] == request.get("id"): credit.set()
            else:
                requests.put(request, timeout=1)
        except Exception:
            emit({"error": "invalid-voice-request"})


if __name__ == "__main__":
    os.umask(0o077)
    command, location = sys.argv[1:3]
    root = Path(location).resolve()
    if command == "previews":
        previews(root)
    elif command == "serve":
        serve(root, Path(sys.argv[3]).resolve() if len(sys.argv)>3 else None)
    else:
        raise SystemExit(2)
