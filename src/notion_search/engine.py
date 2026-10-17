"""Hybrid search (FTS5 words + EmbeddingGemma meaning) answering in Notion's own /api/v3/search format.

Everything runs on ONE thread (MLX requirement, and it keeps SQLite simple): build the Engine there, call it there.
"""

import re
import time
from functools import lru_cache

import numpy as np

from .embedder import DIM, QUERY_LEN, Embedder, query_prompt
from .indexer import open_index, snapshot, store, walk

HL_OPEN, HL_CLOSE = "<gzkNfoUU>", "</gzkNfoUU>"  # Notion's own highlight markers; <b> would show up as text
COVER_BONUS, TITLE_BONUS = 0.05, 0.03  # pages holding ALL the query words (title counts extra) beat partial matches
CURRENT_BONUS = 0.05                    # the page you are on, when it already matches the query
LABEL_EXACT, LABEL_ALL = 0.08, 0.03    # a short block (toggle/heading title) that IS the query, or holds all its words
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
            self.text = {r[0]: r[1:] for r in self.conn.execute("SELECT id, title, crumbs, body, labels FROM fts")}
            rows = self.conn.execute("SELECT page_id, text, vec FROM chunks").fetchall()
            self.chunk_page = [r[0] for r in rows]
            self.chunk_text = [r[1] for r in rows]
            self.vecs = (np.vstack([np.frombuffer(r[2], np.float32) for r in rows])
                         if rows else np.zeros((0, DIM), np.float32))
            self.qvec.cache_clear()
            self.hay = [f"{t}\n{c}\n{b}".lower() for t, c, b, _ in self.text.values()]
            self.df = lru_cache(512)(lambda term: sum(term in h for h in self.hay) or 1)  # pages holding a word
        self.snap = snap
        return changed

    # ── retrieval ────────────────────────────────────────────────────────────

    @staticmethod
    def terms(text):
        return re.findall(r"\w+", text.lower())

    def words(self, terms, limit=25):
        """Pages containing every word (prefix match), then, for several words, pages containing any, best first."""
        sql = "SELECT id FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 0, 8, 3, 1, 5) LIMIT ?"
        hits = {}
        for joiner in (" ", " OR "):
            for (pid,) in self.conn.execute(sql, (joiner.join(f'"{t}"*' for t in terms), limit)):
                hits.setdefault(pid, None)
            if len(terms) == 1:
                break
        return list(hits)[:limit]

    def warm(self):
        """Run the model and read every vector once, on a query that is never cached, so macOS keeps their memory resident."""
        self.warm_count = getattr(self, "warm_count", 0) + 1
        vec = self.embedder.embed([query_prompt(f"warm up {self.warm_count}")], max_length=QUERY_LEN, fixed=True)[0]
        if len(self.chunk_page):
            self.vecs @ vec

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

    def search(self, text, limit=12, current=None):
        """[(page_id, snippet)], best first. `current` is the id of the page open in Notion."""
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
            title, crumbs, body, labels = self.text[pid]
            found = lambda hay: sum(bool(re.search(r"\b" + re.escape(t), hay)) for t in terms) / len(terms)
            scores[pid] += (COVER_BONUS * found(f"{title}\n{crumbs}\n{body}".lower()) ** 2
                            + TITLE_BONUS * found(title.lower()))
            kind = self.label_match(labels, terms)[0]
            scores[pid] += LABEL_EXACT if kind == "exact" else LABEL_ALL if kind == "all" else 0
            if pid == current:
                scores[pid] += CURRENT_BONUS
        best = sorted((p for p in scores if p in self.pages), key=scores.get, reverse=True)[:limit]
        return [(pid, *self.snippet(pid, terms, chunks.get(pid))) for pid in best]

    @staticmethod
    def label_match(labels, terms):
        """(kind, label): the page's best short block for these terms. kind is "exact", "all" or None."""
        best = (None, "")
        for label in labels.split("\n"):
            words = re.findall(r"\w+", label.lower())
            if words == terms:
                return "exact", label
            if words and best[0] is None and all(any(w.startswith(t) for w in words) for t in terms):
                best = ("all", label)
        return best

    def snippet(self, pid, terms, chunk):
        """The single block of the page that best matches the query (one line, never a run of neighbours, which on a
        page like a password list would print unrelated secrets). Empty when no block holds a query word."""
        pat = re.compile("|".join(r"\b" + re.escape(t) for t in terms), re.I)
        best, best_key, best_id = None, None, None
        for bid, line in walk(self.snap.blocks[pid], self.snap.blocks, self.snap.cols):
            words = {m.group().lower() for m in pat.finditer(line)}
            if words:  # a word few pages have beats a common one; among equals the shorter, more specific block wins
                key = (sum(1 / self.df(w) for w in words), -len(line))
                if best_key is None or key > best_key:
                    best, best_key, best_id = line, key, bid
        if best is None:  # the page matched by title or by meaning: show no body text (Notion does the same)
            return "", None
        if len(best) > 160:  # a long block: show the stretch around the first match
            at = pat.search(best).start()
            start = max(0, at - 50)
            best = ("..." if start else "") + best[start:start + 160] + "..."
        return best, best_id

    def merge(self, data, cloud, limit=12):
        """Add Notion's own hits for pages whose text this Mac does not have: pages listed by title only (their blocks
        load when you open them) or missing from the local database. Pages with local text are answered locally."""
        mine = {r["id"]: i for i, r in enumerate(data["results"])}
        fresh = []
        for r in cloud.get("results", []):
            pid = r.get("id")
            if pid in self.pages and pid not in self.snap.thin:
                continue
            if pid in mine:  # a thin page we matched by title: Notion's hit also shows the text that matched
                data["results"][mine[pid]] = r
            else:
                fresh.append(r)
        if fresh:  # ponytail: fixed slots (after the first 6 local hits), no score blending; tune if it reads oddly
            data["results"][6:6] = fresh[:4]
        data["results"] = data["results"][:limit]
        for table, rows in (cloud.get("recordMap") or {}).items():
            if isinstance(rows, dict):
                have = data["recordMap"].setdefault(table, {})
                for key, row in rows.items():
                    have.setdefault(key, row)
        data["total"] = len(data["results"])
        return data

    # ── Notion's response format ─────────────────────────────────────────────

    @staticmethod
    def highlight(text, terms):
        """Wrap whole words that start with a query word, as Notion's own search does."""
        if not terms:
            return text
        pat = "|".join(r"\b" + re.escape(t) + r"\w*" for t in terms)
        return re.sub(f"({pat})", f"{HL_OPEN}\\1{HL_CLOSE}", text, flags=re.I)

    def v3(self, text, limit=12, current=None):
        """What /api/v3/search returns. Notion's UI crashes without trackEventProperties and renders titles
        only from the recordMap, so every result's block (and its parent) travels with it."""
        snap, results, terms = self.snap, [], self.terms(text)
        record_map = {"block": {}, "collection": {}, "space": {}}
        default_space = next(iter(snap.spaces), None)
        for pid, snip, bid in self.search(text, limit, current):
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
            hl_title = self.highlight(self.pages[pid][0], terms)
            hl_text = self.highlight(snip, terms)
            # highlightBlockId is how Notion's cloud tells the client which block to scroll to and highlight on open;
            # like the cloud, send a title highlight only when the title matched, or the client skips that block
            hits = [{"highlightedText": hl_text, "highlightBlockId": bid, "relevanceRankES": 0}] if bid else []
            results.append({
                "id": pid, "table": "block", "spaceId": space_id, "score": 1000 - len(results),
                "sources": ["es"], "analytics": {"pageViews": {}, "uniquePageViews": {}}, "badges": [],
                "isSurrogateCollection": False, "collectionId": None,
                "highlight": {**({"title": hl_title} if HL_OPEN in hl_title else {}), "text": hl_text},
                **({"highlightBlockId": bid} if bid else {}),
                "highlights": {**({"titleHighlight": hl_title} if HL_OPEN in hl_title else {}), "textHighlights": hits},  # newer shape Notion's cloud sends
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
        for pid, *_ in found:
            print("  -", engine.pages[pid][0])
