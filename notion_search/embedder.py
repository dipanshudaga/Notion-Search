"""EmbeddingGemma 2 on MLX (Apple Silicon GPU).

MLX streams are per-thread: build the Embedder and call it from ONE thread only.
Prompts are the model card's: queries "task: search result | query: ...", documents "title: ... | text: ...".
"""

import json
import os
import shutil
import struct
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

DATA = Path(os.environ.get("NOTION_SEARCH_DATA", Path.home() / "Library/Application Support/NotionSearch"))
MODEL_DIR = DATA / "model"  # our own copy, so uninstall can remove it
MODEL = "google/embeddinggemma-2"
DIM = 768
AUDIO = ("audio_tower.", "embed_audio.")
QUERY_LEN = 32
WORKERS, PIECE = 8, 8 * 1024 * 1024  # parallel download connections, and the size of each request


def query_prompt(text):
    return f"task: search result | query: {text}"


def doc_prompt(title, text):
    return f"title: {title or 'none'} | text: {text}"


def fetch_text_weights(dest, progress, before=None):
    """Download the weights without the audio part (600 MB we do not use). We read the safetensors header, request
    the byte ranges of every other tensor (text and image), and write them as a smaller safetensors file.
    progress(done, total) is called for every megabyte, and at once with 0; `before` runs after that first call."""
    from huggingface_hub import hf_hub_url

    url = hf_hub_url(MODEL, "model.safetensors")

    def request(start, end):  # bytes start..end inclusive
        return urllib.request.urlopen(urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"}))

    with request(0, 7) as r:
        (n,) = struct.unpack("<Q", r.read())
    with request(8, 7 + n) as r:
        header = json.loads(r.read())
    meta = header.pop("__metadata__", None)
    keep = sorted((k for k in header if not k.startswith(AUDIO)), key=lambda k: header[k]["data_offsets"][0])
    new, pos = {}, 0
    for k in keep:
        start, end = header[k]["data_offsets"]
        new[k] = {**header[k], "data_offsets": [pos, pos + end - start]}
        pos += end - start
    blob = json.dumps(({"__metadata__": meta} if meta else {}) | new).encode()
    blob += b" " * (-len(blob) % 8)

    progress and progress(0, pos)
    before and before()  # the small files (tokenizer, config) come first, while the bar already shows 0%
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    base = 8 + len(blob)  # where the tensor data starts in the new file
    pieces, cur = [], None  # (source start, source end, offset in the new file); neighbours merge, pieces stay <= 8 MB
    dest_pos = 0
    for k in keep:
        s0, e0 = header[k]["data_offsets"]
        if cur and s0 == cur[1] and e0 - cur[0] <= PIECE:
            cur[1] = e0
        else:
            cur = [s0, e0, dest_pos]
            pieces.append(cur)
        dest_pos += e0 - s0
    done, lock = 0, threading.Lock()

    def fetch(piece):  # one range request, written straight to its place in the file; retried on network errors
        nonlocal done
        s0, e0, off = piece
        for attempt in range(4):
            got = 0
            try:
                with request(8 + n + s0, 8 + n + e0 - 1) as r:
                    while chunk := r.read(256 * 1024):
                        os.pwrite(fd, chunk, base + off + got)
                        got += len(chunk)
                        with lock:
                            done += len(chunk)
                            progress and progress(done, pos)
                return
            except OSError:
                with lock:
                    done -= got
                if attempt == 3:
                    raise
                time.sleep(1 + attempt)

    with open(tmp, "wb") as f:
        fd = f.fileno()
        f.write(struct.pack("<Q", len(blob)) + blob)
        f.flush()
        with ThreadPoolExecutor(WORKERS) as pool:  # several connections at once: one stream is often throttled
            list(pool.map(fetch, pieces))
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
        fetch_text_weights(MODEL_DIR / "model.safetensors", progress,
                           before=lambda: snapshot_download(MODEL, local_dir=MODEL_DIR, ignore_patterns=["model.safetensors"]))
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
