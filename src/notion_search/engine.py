"""Hybrid search (FTS5 words + EmbeddingGemma meaning) answering in Notion's own /api/v3/search format.

Everything runs on ONE thread (MLX requirement, and it keeps SQLite simple): build the Engine there, call it there.
"""

import re
import time
from functools import lru_cache

import numpy as np

from .embedder import DIM, QUERY_LEN, Embedder, query_prompt
from .indexer import open_index, snapshot, store

HL_OPEN, HL_CLOSE = "<gzkNfoUU>", "</gzkNfoUU>"  # Notion's own highlight markers; <b> would show up as text
COVER_BONUS, TITLE_BONUS = 0.05, 0.03  # pages holding ALL the query words (title counts extra) beat partial matches
# A meaning match must stand out this many std-devs from the query's scores over all pages (DigUp's idea; it replaces
# a fixed cosine cutoff). Measured on 836 pages: real queries peak at z 4.6-5.5, gibberish at 3.5.
Z_GATE = 4.0
RRF_K = 40.0


class Engine:
    def __init__(self, progress=None):
        self.embedder = Embedder()
        self.conn = open_index()
        self.snap = None
        self.qvec = lru_cache(256)(lambda q: self.embedder.embed([query_prompt(q)], max_length=QUERY_LEN, fixed=True)[0])
        self.refresh(snapshot(), progress)
        self.search("warm up")  # first query otherwise pays ~250 ms for cold caches

    def refresh(self, snap, progress=None):
        """Index what changed in Notion, then swap in the new snapshot. Returns the number of pages changed."""
        changed = store(self.conn, snap.docs, self.embedder, progress)
        if changed or self.snap is None:
            self.pages = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT id, title, snippet FROM pages")}
            self.text = {r[0]: r[1:] for r in self.conn.execute("SELECT id, title, crumbs, body FROM fts")}
            rows = self.conn.execute("SELECT page_id, text, vec FROM chunks").fetchall()
            self.chunk_page = [r[0] for r in rows]
            self.chunk_text = [r[1] for r in rows]
            self.vecs = (np.vstack([np.frombuffer(r[2], np.float32) for r in rows])
                         if rows else np.zeros((0, DIM), np.float32))
            self.qvec.cache_clear()
            self.hay = [f"{t}\n{c}\n{b}".lower() for t, c, b in self.text.values()]
            self.df = lru_cache(512)(lambda term: sum(term in h for h in self.hay) or 1)  # pages holding a word
        self.snap = snap
        return changed

    # ── retrieval ────────────────────────────────────────────────────────────

    @staticmethod
    def terms(text):
        return re.findall(r"\w+", text.lower())

    def words(self, terms, limit=25):
        """Pages containing every word (prefix match), then, for several words, pages containing any, best first."""
        sql = "SELECT id FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 0, 8, 3, 1) LIMIT ?"
        hits = {}
        for joiner in (" ", " OR "):
            for (pid,) in self.conn.execute(sql, (joiner.join(f'"{t}"*' for t in terms), limit)):
                hits.setdefault(pid, None)
            if len(terms) == 1:
                break
        return list(hits)[:limit]

    def meaning(self, text, limit=25):
        """Pages whose best chunk stands out for this query: [(page_id, that chunk's text)], best first."""
        if not len(self.chunk_page):
            return []
        sims = self.vecs @ self.qvec(text)
        z = (sims - sims.mean()) / (sims.std() + 1e-9)
        best = {}
        for i in np.argsort(-z)[:200]:
            if z[i] < Z_GATE or len(best) >= limit:
                break
            best.setdefault(self.chunk_page[i], self.chunk_text[i])
        return list(best.items())

    def search(self, text, limit=12):
        """[(page_id, snippet)], best first."""
        terms = self.terms(text)
        if not terms:
            return []
        scores, chunks = {}, {}
        for rank, pid in enumerate(self.words(terms)):
            scores[pid] = 1.2 / (RRF_K + rank)
        for rank, (pid, chunk) in enumerate(self.meaning(text)):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
            chunks[pid] = chunk
        for pid in scores:
            title, crumbs, body = self.text[pid]
            found = lambda hay: sum(bool(re.search(r"\b" + re.escape(t), hay)) for t in terms) / len(terms)
            scores[pid] += (COVER_BONUS * found(f"{title}\n{crumbs}\n{body}".lower()) ** 2
                            + TITLE_BONUS * found(title.lower()))
        best = sorted((p for p in scores if p in self.pages), key=scores.get, reverse=True)[:limit]
        return [(pid, self.snippet(pid, terms, chunks.get(pid))) for pid in best]

    def snippet(self, pid, terms, chunk):
        """~200 characters of the page around where the query words cluster (else the best-matching chunk)."""
        body = self.text[pid][2]
        pat = re.compile("|".join(r"\b" + re.escape(t) for t in terms), re.I)
        found = [(m.start(), m.group().lower()) for m in pat.finditer(body)]
        if not found:
            return (chunk or self.pages[pid][1])[:240]
        rarity = {t: 1 / self.df(t) for _, t in found}  # a word few pages have beats a common one
        at = max((p for p, _ in found),
                 key=lambda p: sum(rarity[t] for t in {w for q, w in found if p <= q < p + 200}))
        start = max(0, at - 40)
        return ("..." if start else "") + body[start:start + 200].replace("\n", " ") + "..."

    # ── Notion's response format ─────────────────────────────────────────────

    @staticmethod
    def highlight(text, terms):
        """Wrap whole words that start with a query word, as Notion's own search does."""
        if not terms:
            return text
        pat = "|".join(r"\b" + re.escape(t) + r"\w*" for t in terms)
        return re.sub(f"({pat})", f"{HL_OPEN}\\1{HL_CLOSE}", text, flags=re.I)

    def v3(self, text, limit=12):
        """What /api/v3/search returns. Notion's UI crashes without trackEventProperties and renders titles
        only from the recordMap, so every result's block (and its parent) travels with it."""
        snap, results, terms = self.snap, [], self.terms(text)
        record_map = {"block": {}, "collection": {}, "space": {}}
        default_space = next(iter(snap.spaces), None)
        for pid, snip in self.search(text, limit):
            block = snap.blocks.get(pid)
            if not block:
                continue
            space_id = block["space_id"] or default_space
            record_map["block"][pid] = {"role": "editor", "value": block}
            parent = block["parent_id"]
            if block["parent_table"] == "collection" and parent in snap.cols:
                record_map["collection"][parent] = {"role": "editor", "value": snap.cols[parent]}
            elif block["parent_table"] == "block" and parent in snap.blocks:
                record_map["block"][parent] = {"role": "editor", "value": snap.blocks[parent]}
            if space_id in snap.spaces:
                record_map["space"][space_id] = {"role": "editor", "value": snap.spaces[space_id]}
            name = self.pages[pid][0]
            hl_title = self.highlight(name, terms)
            results.append({
                "id": pid, "table": "block", "spaceId": space_id, "score": 1000 - len(results),
                "sources": [], "isSurrogateCollection": False, "collectionId": None,
                "highlight": {"title": hl_title, "text": self.highlight(snip, terms)},
                "highlights": {"titleHighlight": hl_title, "textHighlights": []},  # newer shape Notion's cloud sends
            })
        return {"results": results, "recordMap": record_map, "total": len(results), "trackEventProperties": {}}


if __name__ == "__main__":  # python -m notion_search.engine "query" ...: quick look at what comes back, and how fast
    import sys

    engine = Engine()
    for q in sys.argv[1:] or ["travel"]:
        t = time.perf_counter()
        found = engine.search(q)
        ms = (time.perf_counter() - t) * 1000
        print(f"\n{q!r}: {len(found)} results in {ms:.1f} ms")
        for pid, _ in found:
            print("  -", engine.pages[pid][0])
