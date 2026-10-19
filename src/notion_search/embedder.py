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
BUILD_CACHE, QUERY_CACHE = 512 * 1024 * 1024, 32 * 1024 * 1024  # MLX buffer cache: without a limit builds ran 3 to 5x slower
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


def save_table(mx, emb, dest):
    """Write the quantized token-embedding table (half the model: 262k words x 768 dims) to files that can be memory-mapped."""
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    np.save(tmp / "weight.npy", np.array(emb["weight"]))
    for name in ("scales", "biases"):  # bfloat16 has no numpy type: float32 holds it exactly
        np.save(tmp / f"{name}.npy", np.array(emb[name].astype(mx.float32)))
    (tmp / "quant.json").write_text(json.dumps({"group_size": emb.group_size, "bits": emb.bits}))
    shutil.rmtree(dest, ignore_errors=True)
    os.replace(tmp, dest)


def lazy_embedding(nn, mx, folder):
    """A token-embedding layer that reads its rows from a memory-mapped file: only the rows of the tokens in use are ever
    in RAM (about 30 for a query), so the table costs no memory. Same maths as QuantizedEmbedding: identical vectors."""
    quant = json.loads((folder / "quant.json").read_text())
    weight, scales, biases = (np.load(folder / f"{n}.npy", mmap_mode="r") for n in ("weight", "scales", "biases"))

    class LazyEmbedding(nn.Module):
        def __call__(self, x):
            ids = np.array(x).reshape(-1)
            rows = lambda a: mx.array(np.ascontiguousarray(a[ids]))
            out = mx.dequantize(rows(weight), rows(scales).astype(mx.bfloat16), rows(biases).astype(mx.bfloat16),
                                group_size=quant["group_size"], bits=quant["bits"])
            return out.reshape(*x.shape, -1)

    return LazyEmbedding()


class Embedder:
    def __init__(self):
        import mlx.core as mx
        import mlx.nn as nn
        import sentencepiece
        from mlx_vlm.embedding_loader import load_embedding_model
        from mlx_vlm.utils import load_config

        path = model_path(lambda m: None)
        config = load_config(path)
        config["audio_config"] = config["vision_config"] = None  # text part only
        self.mx = mx
        self.model = load_embedding_model(Path(path), config=config)
        # SentencePiece reads the 4.7 MB tokenizer.model; Hugging Face's tokenizer.json costs ~300 MB of RAM for the same ids
        self.tokenizer = sentencepiece.SentencePieceProcessor(model_file=str(Path(path) / "tokenizer.model"))
        folder = MODEL_DIR / "embed_table"
        lazy = None
        try:
            if folder.exists():
                lazy = lazy_embedding(nn, mx, folder)
                self.model.language_model.embed_tokens = lazy  # before the model is evaluated: the bf16 table is never read
        except Exception:  # damaged file, or mlx_vlm changed its layout: load the whole model, as before
            shutil.rmtree(folder, ignore_errors=True)
            lazy = None
        nn.quantize(self.model, group_size=128, bits=8)  # 8-bit weights: 546 -> 280 MB, vectors differ by <0.0005 cosine
        mx.eval(self.model.parameters())
        if lazy is None:  # first start: keep the table on disk from now on (the next start loads half the memory)
            try:
                save_table(mx, self.model.language_model.embed_tokens, folder)
                self.model.language_model.embed_tokens = lazy_embedding(nn, mx, folder)
            except Exception:
                shutil.rmtree(folder, ignore_errors=True)
        mx.clear_cache()  # the freed bf16 weights would otherwise stay cached on the GPU
        mx.set_cache_limit(QUERY_CACHE)
        self.embed(["warm up"] * 2, max_length=QUERY_LEN, fixed=True)  # compile Metal kernels now
        self.version = f"mlx-{mx.__version__}"

    def trim(self):
        """Hand back the GPU buffers MLX keeps for reuse (up to the cache limit after a bulk embed, ~500 MB)."""
        self.mx.clear_cache()

    def encode(self, texts, max_length, fixed):
        """Token ids with BOS and EOS, cut to max_length (EOS kept), right-padded: to max_length, or to a multiple of 64
        (few distinct shapes = few kernel compiles). Same ids as the Hugging Face tokenizer on 510 real texts."""
        ids = []
        for t in texts:
            i = self.tokenizer.encode(t, add_bos=True, add_eos=True)
            ids.append(i[:max_length - 1] + [1] if len(i) > max_length else i)
        width = max_length if fixed else -(-max(map(len, ids)) // 64) * 64
        tokens, mask = np.zeros((len(ids), width), np.int64), np.zeros((len(ids), width), np.int64)
        for n, i in enumerate(ids):
            tokens[n, :len(i)], mask[n, :len(i)] = i, 1
        return {"input_ids": tokens, "attention_mask": mask}

    def embed(self, texts, max_length=512, fixed=False, progress=None):
        """fixed=True pads to max_length: MLX recompiles kernels for every new input shape (0.7-9 s a query)."""
        out = []
        bulk = len(texts) > 32  # an index build reuses big buffers; a query needs only a few
        if bulk:
            self.mx.set_cache_limit(BUILD_CACHE)
        for i in range(0, len(texts), 16):
            if progress:
                progress(i, len(texts))
            tok = self.encode(texts[i:i + 16], max_length, fixed)
            res = self.model(**{k: self.mx.array(v) for k, v in tok.items()})
            vecs = np.array(res.text_embeds.astype(self.mx.float32))
            out.append(vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9))
        if progress:
            progress(len(texts), len(texts))
        if bulk:
            self.mx.set_cache_limit(QUERY_CACHE)
            self.trim()
        return np.vstack(out)
