"""EmbeddingGemma 2 on MLX (Apple Silicon GPU).

MLX streams are per-thread: build the Embedder and call it from ONE thread only.
Prompts are the model card's: queries "task: search result | query: ...", documents "title: ... | text: ...".
"""

import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np

DATA = Path(os.environ.get("NOTION_SEARCH_DATA", Path.home() / "Library/Application Support/NotionSearch"))
MODEL_DIR = DATA / "model"  # our own copy, so uninstall can remove it
MODEL = "google/embeddinggemma-2"
DIM = 768
QUERY_LEN = 32


def query_prompt(text):
    return f"task: search result | query: {text}"


def doc_prompt(title, text):
    return f"title: {title or 'none'} | text: {text}"


def model_path(say=print, progress=None):
    """Folder holding the model. Downloads it (about 1.5 GB) on the first run; progress(done, total) counts bytes."""
    if (MODEL_DIR / "model.safetensors").exists():
        say("Already on this Mac")
        return str(MODEL_DIR)
    from huggingface_hub import snapshot_download

    try:  # reuse a copy from the shared Hugging Face cache instead of downloading again
        cached = snapshot_download(MODEL, local_files_only=True)
    except Exception:
        cached = None
    if cached:
        say("Copying from the Hugging Face cache")
        shutil.copytree(cached, MODEL_DIR, dirs_exist_ok=True)
    else:
        say("Downloading about 1.5 GB (once)")
        if not progress:
            snapshot_download(MODEL, local_dir=MODEL_DIR)
            return str(MODEL_DIR)
        from huggingface_hub import HfApi

        total = sum(f.size or 0 for f in HfApi().model_info(MODEL, files_metadata=True).siblings)
        failure = []

        def fetch():
            try:
                snapshot_download(MODEL, local_dir=MODEL_DIR)
            except BaseException as e:
                failure.append(e)

        worker = threading.Thread(target=fetch, daemon=True)  # daemon: Ctrl+C must not wait for it
        worker.start()
        while worker.is_alive():
            progress(sum(f.stat().st_size for f in MODEL_DIR.rglob("*") if f.is_file()), total)
            time.sleep(0.5)
        if failure:
            raise failure[0]
        progress(total, total)
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
