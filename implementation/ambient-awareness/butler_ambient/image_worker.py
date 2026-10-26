"""One requested image, offline weights, isolated process and bounded resources."""
import contextlib
import json
import os
import sys
from pathlib import Path


def main():
    os.umask(0o077)
    os.nice(10)
    root = Path(sys.argv[1]).resolve()
    model_path = Path(sys.argv[2]).resolve() if len(sys.argv)>2 else root/"model"
    value = json.loads(sys.stdin.buffer.readline(8192))
    prompt, key = value["prompt"], value["id"]
    import re
    if not isinstance(prompt, str) or not 1 <= len(prompt) <= 1000 or not re.fullmatch(r"[a-f0-9]{32}", key):
        raise ValueError("invalid image request")
    output = root / "generated" / (key + ".png")
    output.parent.mkdir(mode=0o700, exist_ok=True)
    def progress(stage):
        print(json.dumps({"progress": stage}), flush=True)
    progress("loading")
    with contextlib.redirect_stdout(sys.stderr):
        import torch
        from diffusers import AutoPipelineForText2Image
        torch.set_num_threads(2)
        if not torch.backends.mps.is_available(): raise RuntimeError("local Metal device unavailable")
        pipeline = AutoPipelineForText2Image.from_pretrained(
            str(model_path), torch_dtype=torch.float16, variant="fp16", use_safetensors=True,
            local_files_only=True).to("mps")
        pipeline.set_progress_bar_config(disable=True)
    progress("drawing")
    with contextlib.redirect_stdout(sys.stderr):
        image = pipeline(prompt=prompt, num_inference_steps=4, guidance_scale=0.0, height=512, width=512).images[0]
        image.save(output)
    output.chmod(0o600)
    model = "stabilityai/sd-turbo" if model_path.resolve()==(root/"model").resolve() else "local:"+model_path.name
    print(json.dumps({"file": str(output), "model": model, "generated": True}), flush=True)


if __name__ == "__main__":
    try: main()
    except Exception:
        print(json.dumps({"error": "local image generation unavailable"}), flush=True)
        raise SystemExit(1) from None
