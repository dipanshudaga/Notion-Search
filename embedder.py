"""EmbeddingGemma 2 on MLX (Apple Silicon GPU).

MLX streams are per-thread: build the Embedder and call it from ONE thread only.
Prompts are the model card's: queries "task: search result | query: ...", documents "title: ... | text: ...".
"""

from pathlib import Path

import numpy as np

MODEL = "google/embeddinggemma-2"
DIM = 768
QUERY_LEN = 32


def query_prompt(text):
    return f"task: search result | query: {text}"


def doc_prompt(title, text):
    return f"title: {title or 'none'} | text: {text}"


def model_path():
    from huggingface_hub import snapshot_download

    try:
        return snapshot_download(MODEL, local_files_only=True)
    except Exception:
        return snapshot_download(MODEL)  # first run only (~300 MB)


class Embedder:
    def __init__(self):
        import mlx.core as mx
        from mlx_vlm.embedding_loader import load_embedding_model
        from mlx_vlm.utils import load_config
        from transformers import AutoTokenizer

        path = model_path()
        config = load_config(path)
        config["audio_config"] = config["vision_config"] = None  # text part only
        self.mx = mx
        self.model = load_embedding_model(Path(path), config=config)
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        mx.eval(self.model.parameters())
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
