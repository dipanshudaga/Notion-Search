"""EmbeddingGemma 2 on MLX (Apple Silicon GPU).

MLX streams are per-thread: build the Embedder and call it from ONE thread only.
Prompts are the model card's: queries "task: search result | query: ...", documents "title: ... | text: ...".
"""

import json
import os
import shutil
import struct
import urllib.request
from pathlib import Path

import numpy as np

DATA = Path(os.environ.get("NOTION_SEARCH_DATA", Path.home() / "Library/Application Support/NotionSearch"))
MODEL_DIR = DATA / "model"  # our own copy, so uninstall can remove it
MODEL = "google/embeddinggemma-2"
DIM = 768
AUDIO = ("audio_tower.", "embed_audio.")
QUERY_LEN = 32


def query_prompt(text):
    return f"task: search result | query: {text}"


def doc_prompt(title, text):
    return f"title: {title or 'none'} | text: {text}"


def fetch_text_weights(dest, progress):
    """Download the weights without the audio part (600 MB we do not use). We read the safetensors header, request
    the byte ranges of every other tensor (text and image), and write them as a smaller safetensors file."""
    from huggingface_hub import hf_hub_url

    url = hf_hub_url(MODEL, "model.safetensors")

    def get(start, end):  # bytes start..end inclusive
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})) as r:
            return r.read()

    (n,) = struct.unpack("<Q", get(0, 7))
    header = json.loads(get(8, 7 + n))
    meta = header.pop("__metadata__", None)
    keep = sorted((k for k in header if not k.startswith(AUDIO)), key=lambda k: header[k]["data_offsets"][0])
    new, pos = {}, 0
    for k in keep:
        start, end = header[k]["data_offsets"]
        new[k] = {**header[k], "data_offsets": [pos, pos + end - start]}
        pos += end - start
    blob = json.dumps(({"__metadata__": meta} if meta else {}) | new).encode()
    blob += b" " * (-len(blob) % 8)
    tmp = dest.with_suffix(".part")
    with open(tmp, "wb") as f:
        f.write(struct.pack("<Q", len(blob)) + blob)
        done = 0
        runs, cur = [], None  # one request per tensor is slow, so merge neighbours into runs of up to 32 MB
        for k in keep:
            s0, e0 = header[k]["data_offsets"]
            if cur and s0 == cur[1] and e0 - cur[0] <= 32 * 1024 * 1024:
                cur[1] = e0
            else:
                cur = [s0, e0]
                runs.append(cur)
        for s0, e0 in runs:
            f.write(get(8 + n + s0, 8 + n + e0 - 1))
            done += e0 - s0
            progress and progress(done, pos)
    tmp.rename(dest)


def model_path(say=print, progress=None):
    """Folder holding the model. Downloads it (about 900 MB) on the first run; progress(done, total) counts bytes."""
    if (MODEL_DIR / "model.safetensors").exists():
        say("Already on this Mac")
        return str(MODEL_DIR)
    from huggingface_hub import snapshot_download
    from huggingface_hub.utils import disable_progress_bars, logging as hf_logging

    disable_progress_bars()  # our own installer draws the progress
    hf_logging.set_verbosity_error()  # no "unauthenticated requests" warning

    try:  # reuse a copy from the shared Hugging Face cache instead of downloading again
        cached = snapshot_download(MODEL, local_files_only=True)
    except Exception:
        cached = None
    if cached:
        say("Copying from the Hugging Face cache")
        shutil.copytree(cached, MODEL_DIR, dirs_exist_ok=True)
    else:
        say("Downloading about 900 MB (once)")
        snapshot_download(MODEL, local_dir=MODEL_DIR, ignore_patterns=["model.safetensors"])  # tokenizer, config
        fetch_text_weights(MODEL_DIR / "model.safetensors", progress)
    return str(MODEL_DIR)


class Embedder:
    def __init__(self):
        import mlx.core as mx
        import mlx.nn as nn
        from mlx_vlm.embedding_loader import load_embedding_model
        from mlx_vlm.utils import load_config
        from transformers import AutoTokenizer

        path = model_path(lambda m: None)
        config = load_config(path)
        config["audio_config"] = config["vision_config"] = None  # text part only
        self.mx = mx
        self.model = load_embedding_model(Path(path), config=config)
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        nn.quantize(self.model, group_size=128, bits=8)  # 8-bit weights: 546 -> 280 MB, vectors differ by <0.0005 cosine
        mx.eval(self.model.parameters())
        mx.clear_cache()  # the freed bf16 weights would otherwise stay cached on the GPU
        mx.set_cache_limit(512 * 1024 * 1024)
        self.embed(["warm up"] * 2, max_length=QUERY_LEN, fixed=True)  # compile Metal kernels now
        self.version = f"mlx-{mx.__version__}"

    def embed(self, texts, max_length=512, fixed=False, progress=None):
        """fixed=True pads to max_length: MLX recompiles kernels for every new input shape (0.7-9 s a query)."""
        out = []
        for i in range(0, len(texts), 16):
            if progress:
                progress(i, len(texts))
            tok = self.tokenizer(texts[i:i + 16], return_tensors="np", padding="max_length" if fixed else True,
                                 pad_to_multiple_of=None if fixed else 64,  # few distinct shapes = few kernel compiles
                                 truncation=True, max_length=max_length)
            res = self.model(**{k: self.mx.array(v) for k, v in tok.items()})
            vecs = np.array(res.text_embeds.astype(self.mx.float32))
            out.append(vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9))
        if progress:
            progress(len(texts), len(texts))
        return np.vstack(out)
