"""Reads Notion Desktop's local database and keeps a hybrid index (FTS5 + EmbeddingGemma vectors) in sync.

One path for everything: snapshot() reads Notion, store() embeds only the pages whose text changed
(first run = every page, later runs = a handful). Nothing here touches Notion's files except to read them.
"""

import hashlib
import json
import os
import sqlite3
from collections import namedtuple
from pathlib import Path

from .embedder import DATA, MODEL, doc_prompt

NOTION_DB = Path.home() / "Library/Application Support/Notion/notion.db"
INDEX_DB = DATA / "index.db"
FINGERPRINT = f"{MODEL} prompts1 chunks1500 len512 q8g128 labels1 props1"  # change = every vector is recomputed on next start
PAGE_TYPES = {"page", "collection_view_page"}
CHUNK_CHARS = 1500  # a long page becomes several chunks, each embedded on its own

Snapshot = namedtuple("Snapshot", "docs blocks cols spaces thin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY, title TEXT, snippet TEXT, sig TEXT);
CREATE TABLE IF NOT EXISTS chunks(page_id TEXT, n INTEGER, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(id UNINDEXED, title, crumbs, body, labels, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def _json(raw, default):
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def rich(segs):
    """Plain text of rich text: [["Hel"], ["lo", [["b"]]]] -> "Hello"."""
    return "".join(s[0] for s in segs or [] if isinstance(s, list) and s and s[0] != "‣").strip()


def title(props):
    return rich((props or {}).get("title"))


def block_text(b, blocks):
    """A block's own text. Table rows keep one value per column id, read in the table's column order."""
    if b["type"] != "table_row":
        return title(b["properties"])
    order = (blocks.get(b["parent_id"]) or {}).get("format", {}).get("table_block_column_order") or list(b["properties"])
    return " | ".join(t for t in (rich(b["properties"].get(c)) for c in order) if t)


def read_notion():
    """Alive, non-trashed rows of Notion's db as recordMap-shaped values. Read-only: never locks Notion."""
    db = sqlite3.connect(f"file:{NOTION_DB}?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    spaces = {r["id"]: {"id": r["id"], "version": int(r["version"] or 1), "name": r["name"], "permissions": []}
              for r in db.execute("SELECT * FROM space")}
    teams = {r["id"] for r in db.execute("SELECT id FROM team")}
    cols = {}
    for r in db.execute("SELECT * FROM collection WHERE alive = 1"):
        if r["moved_to_trash_time"] is None:
            cols[r["id"]] = {"id": r["id"], "version": int(r["version"] or 1), "alive": True,
                             "name": _json(r["name"], [[r["id"]]]),
                             "parent_id": r["parent_id"], "parent_table": r["parent_table"],
                             "schema": _json(r["schema"], {})}
    blocks, gone = {}, set()  # gone: deleted blocks, so a missing child is told apart from one Notion never loaded
    for r in db.execute("SELECT id FROM block WHERE alive = 0"):
        gone.add(r[0])
    for r in db.execute("SELECT * FROM block WHERE alive = 1"):
        if r["moved_to_trash_time"] is not None or r["archived_time"] is not None:
            continue
        v = {"id": r["id"], "version": int(r["version"] or 1), "type": r["type"], "alive": True,
             "properties": _json(r["properties"], {}), "content": _json(r["content"], []),
             "parent_id": r["parent_id"], "parent_table": r["parent_table"], "space_id": r["space_id"],
             "created_time": r["created_time"], "last_edited_time": r["last_edited_time"]}
        if r["format"]:
            v["format"] = _json(r["format"], {})
        blocks[r["id"]] = v
    db.close()
    return spaces, teams, cols, blocks, gone


def _live(blocks, cols, spaces, teams):
    """Predicate: does this block's parent chain reach a real space? (Imported-then-deleted trees don't.)"""
    memo = {}

    def live(bid):
        path, cur, ok = [], ("block", bid), False
        while True:
            if cur in memo:
                ok = memo[cur]
                break
            if cur in path:  # cycle
                break
            path.append(cur)
            table, id_ = cur
            if table == "space":
                ok = id_ in spaces
                break
            if table == "team":
                ok = id_ in teams
                break
            rec = (blocks if table == "block" else cols).get(id_) if table in ("block", "collection") else None
            if rec is None:
                break
            cur = (rec["parent_table"], rec["parent_id"])
        memo.update(dict.fromkeys(path, ok))
        return ok

    return live


def _crumbs(page, blocks, cols):
    out, table, id_ = [], page["parent_table"], page["parent_id"]
    for _ in range(8):
        if table == "block" and id_ in blocks:
            rec = blocks[id_]
            name = title(rec["properties"]) if rec["type"] in PAGE_TYPES else ""
        elif table == "collection" and id_ in cols:
            rec = cols[id_]
            name = "".join(s[0] for s in rec["name"] if isinstance(s, list) and s)
        else:
            break
        if name and (not out or out[-1] != name):
            out.append(name)
        table, id_ = rec["parent_table"], rec["parent_id"]
    return " / ".join(reversed(out))


PROP_TYPES = {"select", "multi_select", "status", "text", "url", "email", "phone_number", "number"}


def props(page, cols):
    """A database row's text properties as [(name, value)], e.g. [("Author", "Voltaire")]. Notion keys them by short ids."""
    schema = (cols.get(page["parent_id"]) or {}).get("schema", {}) if page["parent_table"] == "collection" else {}
    out = []
    for key, spec in schema.items():
        value = rich(page["properties"].get(key)) if spec.get("type") in PROP_TYPES else ""
        if value:
            out.append((spec.get("name", ""), value))
    return out


def walk(page, blocks, cols):
    """Every text block under the page, in reading order, as (block_id, text); a database row's properties come first
    with no block id. Child pages are their own documents."""
    for name, value in props(page, cols):
        yield None, f"{name}: {value}"
    seen, stack = set(), list(reversed(page["content"]))
    while stack:
        bid = stack.pop()
        b = blocks.get(bid)
        if not b or bid in seen or b["type"] in PAGE_TYPES:
            continue
        seen.add(bid)
        text = block_text(b, blocks)
        if text:
            yield bid, text
        stack.extend(reversed(b["content"]))


def _lines(page, blocks, cols):
    """The page's text blocks in reading order, plus the short ones (toggle and heading titles, property values: labels)."""
    out = [text for _, text in walk(page, blocks, cols)]
    labels = [v for _, v in props(page, cols)] + [t for t in out if len(t) <= 50 and len(t.split()) <= 6 and "http" not in t]
    return out, labels


def _chunks(lines):
    """Pack lines into chunks of about CHUNK_CHARS, never splitting a line unless it is longer than a chunk."""
    out, cur, size = [], [], 0
    for line in lines:
        for i in range(0, len(line), CHUNK_CHARS):
            piece = line[i:i + CHUNK_CHARS]
            if cur and size + len(piece) > CHUNK_CHARS:
                out.append("\n".join(cur))
                cur, size = [], 0
            cur.append(piece)
            size += len(piece) + 1
    if cur:
        out.append("\n".join(cur))
    return out or [""]  # an empty page still has its title and path to find


def snapshot():
    spaces, teams, cols, blocks, gone = read_notion()
    live = _live(blocks, cols, spaces, teams)
    docs = {}
    for b in blocks.values():
        name = title(b["properties"])
        if b["type"] not in PAGE_TYPES or not name or not live(b["id"]):
            continue
        crumbs = _crumbs(b, blocks, cols)
        lines, labels = _lines(b, blocks, cols)
        body = "\n".join(lines)
        docs[b["id"]] = {"id": b["id"], "title": name, "crumbs": crumbs, "body": body, "labels": "\n".join(labels), "chunks": _chunks(lines),
                         "snippet": body[:240] or crumbs or "Empty page",
                         "sig": hashlib.sha1(f"{name}\0{crumbs}\0{body}".encode()).hexdigest()}
    # pages Notion lists but has not loaded the text of (it saves a page's blocks when you open the page)
    thin = {pid for pid in docs if any(k not in blocks and k not in gone for k in blocks[pid]["content"])}
    return Snapshot(docs, blocks, cols, spaces, thin)


def open_index():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(INDEX_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT v FROM meta WHERE k = 'fingerprint'").fetchone()
    if not row or row[0] != FINGERPRINT:
        with conn:
            conn.execute("DROP TABLE fts")  # its columns may have changed
            for table in ("pages", "chunks"):
                conn.execute(f"DELETE FROM {table}")
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('fingerprint', ?)", (FINGERPRINT,))
    return conn


def store(conn, docs, embedder, progress=None):
    """Bring the index in line with docs. Returns how many pages were added, changed or removed."""
    have = dict(conn.execute("SELECT id, sig FROM pages"))
    gone = have.keys() - docs.keys()
    todo = [d for d in docs.values() if have.get(d["id"]) != d["sig"]]
    jobs = [(d, c) for d in todo for c in d["chunks"]]
    vecs = embedder.embed([doc_prompt(d["title"], f"{d['crumbs']}\n{c}" if d["crumbs"] else c) for d, c in jobs], progress=progress) if jobs else []
    with conn:
        for pid in gone | {d["id"] for d in todo}:
            for table, col in (("pages", "id"), ("chunks", "page_id"), ("fts", "id")):
                conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (pid,))
        for d in todo:
            conn.execute("INSERT INTO pages VALUES (?, ?, ?, ?)", (d["id"], d["title"], d["snippet"], d["sig"]))
            conn.execute("INSERT INTO fts VALUES (?, ?, ?, ?, ?)", (d["id"], d["title"], d["crumbs"], d["body"], d["labels"]))
        counts = {}
        for (d, c), v in zip(jobs, vecs):
            n = counts[d["id"]] = counts.get(d["id"], -1) + 1
            conn.execute("INSERT INTO chunks VALUES (?, ?, ?, ?)", (d["id"], n, c, v.astype("float32").tobytes()))
    return len(todo) + len(gone)
