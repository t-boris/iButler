"""System 1 in shadow mode (DEC-047, DEC-057): a local decision model that predicts, never acts.

The router (`s1-llm-auto-router`, mmBERT-small pruned, ONNX int8, MIT) lives
under the core's model directory and runs on the CPU in a few milliseconds.
``Shadow`` records its category and difficulty next to the route the rules
and System 2 actually took; the evaluation criteria stay open until the owner
agrees them. Without the model files or the runtime the shadow records nothing.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

MODEL_DIR = Path(os.environ.get("BUTLER_CORE_MODELS", Path.home() / ".local/share/butler-core/models")) / "s1-llm-auto-router"
CATEGORIES = ("coding", "agentic", "math", "knowledge", "long_context", "tool_use", "design", "summarisation", "general")
MAX_TOKENS = 512


def softmax(values, temperature=1.0):
    scaled = [v / temperature for v in values]
    top = max(scaled)
    weights = [math.exp(v - top) for v in scaled]
    total = sum(weights)
    return [w / total for w in weights]


class RouterModel:
    """``predict(text)`` → (label, confidence) where label is ``<category>/d<difficulty>``."""

    name = "s1-llm-auto-router"

    def __init__(self, directory=MODEL_DIR):
        self.directory = Path(directory)
        self.session = self.tokenizer = None
        self.meta = {}
        self.error = None
        self.last_latency_ms = None

    def available(self):
        if self.session is not None:
            return True
        if self.error is not None:
            return False
        try:
            import onnxruntime
            from tokenizers import Tokenizer
            self.meta = json.loads((self.directory / "meta.json").read_text())
            self.tokenizer = Tokenizer.from_file(str(self.directory / "tokenizer/tokenizer.json"))
            self.session = onnxruntime.InferenceSession(str(self.directory / "model.gq.onnx"),
                                                        providers=["CPUExecutionProvider"])
        except Exception as error:  # missing files or runtime: shadow stays silent, never breaks admission
            self.error = f"{type(error).__name__}"
            self.session = None
            return False
        return True

    def predict(self, text):
        if not self.available():
            raise RuntimeError(f"model unavailable: {self.error}")
        import numpy
        encoded = self.tokenizer.encode(str(text)[:4000])
        ids = encoded.ids[:MAX_TOKENS]
        mask = encoded.attention_mask[:MAX_TOKENS]
        started = time.perf_counter()
        outputs = self.session.run(None, {"input_ids": numpy.array([ids], dtype=numpy.int64),
                                          "attention_mask": numpy.array([mask], dtype=numpy.int64)})
        self.last_latency_ms = round((time.perf_counter() - started) * 1000, 2)
        temperatures = self.meta.get("temperatures", {})
        category = softmax(list(map(float, outputs[0][0])), float(temperatures.get("category", 1.0)))
        difficulty = softmax(list(map(float, outputs[1][0])), float(temperatures.get("difficulty", 1.0)))
        best = max(range(len(category)), key=category.__getitem__)
        level = max(range(len(difficulty)), key=difficulty.__getitem__) + 1
        label = f"{CATEGORIES[best] if best < len(CATEGORIES) else best}/d{level}"
        return label, round(category[best], 4)

    def health(self):
        return {"name": self.name, "available": self.available(), "error": self.error, "directory": str(self.directory),
                "last_latency_ms": self.last_latency_ms}


def load(directory=MODEL_DIR):
    model = RouterModel(directory)
    return model if model.available() else None
