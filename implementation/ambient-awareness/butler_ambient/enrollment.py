"""Explicit owner enrollment; reference media remain transient and local."""
from __future__ import annotations

import json
import time

from .sensing import Inputs, Verifier


def enroll_live(store, config, participant, name, seconds=10, *, multiple_samples=False):
    if not 1 <= seconds <= 30:
        raise ValueError("enrollment duration must be 1–30 seconds")
    if store.get("enabled", False):
        raise ValueError("pause collection before enrolling a participant")
    verifier = Verifier(config["face_detector"], config["face_recognizer"])
    inputs = Inputs(config.get("camera", "0"), config.get("microphone", "0"))
    pcm = bytearray()
    frames = []
    try:
        inputs.start()
        deadline = time.monotonic() + seconds + 3
        while len(pcm) < 32000 * seconds and time.monotonic() < deadline:
            sample = inputs.batch()
            pcm.extend(sample.pcm)
            if sample.frames:
                frames.append(sample.frames[-1])
    finally:
        inputs.stop()
    if not frames:
        reason = inputs.errors.get("camera", "").strip()
        raise ValueError("camera unavailable: no fresh reference frame" + ("; " + reason if reason else ""))
    if len(pcm) < 32000:
        raise ValueError("microphone unavailable: no reference audio")
    selected = None
    face_samples = []
    for frame in frames:
        faces = verifier._faces(frame, check_bodies=False)
        if len(faces) > 1:
            raise ValueError("reference rejected: more than one person visible")
        if len(faces) == 1:
            selected = frame
            if multiple_samples:
                face_samples.append(faces[0].tolist())
    if selected is None:
        raise ValueError("reference rejected: a single visible face is required")
    reference = verifier.enroll(selected, bytes(pcm))
    if multiple_samples:
        # This branch is explicit setup only. No media are saved, and normal
        # verification never learns a new face or voice from its own results.
        reference["face_samples"] = face_samples[:30]
        reference["voice_samples"] = [sample.tolist() for sample in verifier._voices(bytes(pcm))][:30]
        if not reference["voice_samples"]:
            raise ValueError("clear reference speech is required")
    store.execute("INSERT OR REPLACE INTO agreements VALUES (?,?,1,?)", (participant, name, json.dumps(reference)))
    return {"enrolled": participant, "agreement": True, "reference_media_retained": False}
