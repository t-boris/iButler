"""Explicit owner-local device/model controls; never a model-facing tool."""
from __future__ import annotations

import http.client
import json
import re
import os
import shutil
from . import processes as subprocess
from pathlib import Path

from .analysis import LocalAnalyzer, DEFAULT_DIALOGUE_MODEL


def devices(ffmpeg="ffmpeg"):
    # Enumeration only: -list_devices exits before opening a capture stream.
    result = subprocess.run([ffmpeg, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=8)
    found = {"camera": [], "microphone": []}
    section = None
    for line in result.stderr.decode(errors="replace").splitlines():
        if "AVFoundation video devices:" in line: section = "camera"
        elif "AVFoundation audio devices:" in line: section = "microphone"
        elif section:
            match = re.search(r"\] \[(\d+)\] (.+)$", line)
            if match and not match[2].startswith("Capture screen"):
                found[section].append({"id": match[1], "name": match[2]})
    return found


def camera_modes(service):
    helper=service.config.get("audio_helper")
    if not helper:return []
    try:
        result=subprocess.run([helper,"--camera-modes"],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=5)
        value=json.loads(result.stdout)
        return value if result.returncode==0 and isinstance(value,list) else []
    except (OSError,ValueError,subprocess.TimeoutExpired):return []


def snapshot(service):
    choices = devices(service.config.get("ffmpeg_cli","ffmpeg"))
    models = []
    conn = http.client.HTTPConnection("127.0.0.1", 11434, timeout=3)
    try:
        conn.request("GET", "/api/tags")
        response = conn.getresponse()
        if response.status == 200:
            models = [m["name"] for m in json.loads(response.read(1_000_000)).get("models", [])
                      if isinstance(m.get("name"), str) and "cloud" not in m["name"].lower()]
    except (OSError, ValueError):
        pass
    finally:
        conn.close()
    config = service.config
    return {"devices": choices, "models": models,"camera_modes":camera_modes(service),
            "camera": str(config.get("camera", "0")), "microphone": str(config.get("microphone", "0")),
            "camera_size": config.get("camera_size","640x480"),
            "vision_model": config.get("vision_model", "gemma3:4b"),
            "conversation_model": config.get("conversation_model", DEFAULT_DIALOGUE_MODEL),
            "asr_model": Path(config.get("whisper_model", "unavailable")).name,
            "speech_language": config.get("speech_language", "ru"),
            "tts_model": "Qwen3-TTS 1.7B Base 8bit · synthetic selected preset",
            "echo_backend": "SpeexDSP 1.2.1 · application-local playback reference",
            "processing": "local only; no paid or external fallback"}


def executable(value, family):
    found = shutil.which(str(Path(value).expanduser()))
    if not found or not os.access(found,os.X_OK): raise ValueError("selected CLI is not executable")
    name = Path(found).name
    valid = (bool(re.fullmatch(r"python(?:[0-9.]+)?",name)) if family == "python" else name == family)
    if not valid: raise ValueError("selected CLI is not a compatible executable for this role")
    return str(Path(found).absolute())


def recognition_model(path, role):
    """Load and exercise model shapes with synthetic data, never owner media."""
    try:
        import numpy as np
        if role == "voice_identity_model":
            from resemblyzer import VoiceEncoder
            model = VoiceEncoder(device="cpu", verbose=False,weights_fpath=path)
            if model.embed_utterance(np.zeros(16000,dtype=np.float32)).shape != (256,): raise ValueError()
        else:
            import cv2
            blank = np.zeros((480,640,3),dtype=np.uint8)
            if role == "face_detector":
                cv2.FaceDetectorYN.create(str(path),"",(640,480),.8).detect(blank)
            elif role == "face_recognizer":
                vector = cv2.FaceRecognizerSF.create(str(path),"").feature(np.zeros((112,112,3),dtype=np.uint8))
                if vector.shape != (1,128):raise ValueError()
            else:
                from .people import PeopleDetector
                PeopleDetector(path).detect(blank)
    except Exception:
        raise ValueError("model is incompatible with this recognition role; existing enrollment was kept") from None


def update(service, values):
    file_keys = {"face_detector","face_recognizer","person_detector","voice_identity_model"}
    python_keys = {"voice_python","image_python"}
    cli_keys = {"whisper_cli":"whisper-cli","ffmpeg_cli":"ffmpeg","openclaw_cli":"openclaw"}
    model_dirs = {"voice_model_path":"config.json","image_model_path":"model_index.json"}
    allowed = {"camera", "microphone", "camera_size", "vision_model", "conversation_model", "speech_language", "whisper_model"} | file_keys | python_keys | cli_keys.keys() | model_dirs.keys()
    if not isinstance(values, dict) or not values or set(values) - allowed:
        raise ValueError("unsupported local setting")
    if any(not isinstance(v, str) or len(v) > 200 for v in values.values()):
        raise ValueError("invalid local setting")
    changes = dict(values)
    for key,family in cli_keys.items():
        if key in changes: changes[key] = executable(changes[key],family)
    for key in python_keys & changes.keys():
        changes[key] = executable(changes[key],"python")
        module = "mlx_audio" if key == "voice_python" else "diffusers"
        result = subprocess.run([changes[key],"-c",f"import {module}"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        if result.returncode: raise ValueError("selected Python environment lacks the required local model runtime")
    for key in file_keys & changes.keys():
        path = Path(changes[key]).expanduser().resolve()
        if not path.is_file() or path.suffix != (".pt" if key == "voice_identity_model" else ".onnx"):
            raise ValueError("compatible local recognition model required")
        recognition_model(path,key)
        changes[key] = str(path)
    for key,metadata in model_dirs.items():
        if key not in changes: continue
        path = Path(changes[key]).expanduser().resolve()
        if not path.is_dir() or not (path/metadata).is_file(): raise ValueError("complete local model folder required")
        data = json.loads((path/metadata).read_text())
        if key == "voice_model_path" and (data.get("model_type") != "qwen3_tts" or data.get("tts_model_type") != "base"):
            raise ValueError("compatible Qwen3-TTS Base model required")
        if key == "image_model_path" and data.get("_class_name") not in {"StableDiffusionPipeline","StableDiffusionXLPipeline"}:
            raise ValueError("compatible offline Stable Diffusion model required")
        if not any(path.rglob("*.safetensors")): raise ValueError("local model weights are missing")
        changes[key] = str(path)
    if "camera_size" in changes:
        if changes["camera_size"] not in {"640x480","1280x720","1920x1080"}: raise ValueError("unsupported camera size")
        changes["recording_width"] = 640
    if "whisper_model" in changes:
        model = Path(changes["whisper_model"]).expanduser().resolve()
        if not model.is_file() or model.suffix != ".bin" or model.stat().st_size < 1_000_000:
            raise ValueError("local Whisper model required")
        with model.open("rb") as stream:
            if stream.read(4) != b"lmgg": raise ValueError("compatible GGML Whisper model required")
        changes["whisper_model"] = str(model)
    if "speech_language" in changes and changes["speech_language"] not in {"ru", "auto", "en", "de", "uk"}:
        raise ValueError("unsupported speech language")
    if {"camera", "microphone"} & changes.keys():
        choices = devices(changes.get("ffmpeg_cli",service.config.get("ffmpeg_cli","ffmpeg")))
        for kind in ("camera", "microphone"):
            if kind not in changes: continue
            device = next((d for d in choices[kind] if d["id"] == changes[kind]), None)
            if device is None: raise ValueError("device is no longer available")
            if kind == "microphone": changes["expected_microphone"] = device["name"]
    if "camera_size" in changes and service.config.get("audio_helper"):
        if "choices" not in locals():choices=devices(changes.get("ffmpeg_cli",service.config.get("ffmpeg_cli","ffmpeg")))
        selected=next((d for d in choices["camera"] if d["id"]==changes.get("camera",str(service.config.get("camera","0")))),None)
        modes=next((d for d in camera_modes(service) if selected and d.get("name")==selected["name"]),None)
        if modes is None or changes["camera_size"] not in modes.get("sizes_at_30fps",[]):
            raise ValueError("selected camera does not report this resolution at 30 fps")
    for key in ("vision_model", "conversation_model"):
        if key in changes:
            LocalAnalyzer(changes[key], dialogue=key == "conversation_model").check_local(require_vision=key == "vision_model")
    path = service.store.root / "config.json"
    # Read/merge the private installed configuration, preserving all unrelated
    # paths and settings. A change becomes effective after the own-service restart.
    config = json.loads(path.read_text())
    config.update(changes)
    temporary = path.with_suffix(".json.tmp")
    with service.lock:
        service._invalidate("settings-change")
        temporary.write_text(json.dumps(config, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    return {"saved": True, "restart_required": True, "changed": sorted(values)}
