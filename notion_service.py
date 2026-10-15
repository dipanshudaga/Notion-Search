"""Background service: answers Notion Desktop's own search box from the local index.

  * keeps Notion started with the Chromium debug port (restarts it once if you opened it without)
  * hooks each Notion tab over that port and answers /api/v3/search from the local index (~25 ms, offline)
  * re-indexes in the background whenever Notion's local database changes

Notion's app files are never touched, so its signature (and the Keychain) stay as shipped.
`python notion_service.py --download` fetches the model, `--index` builds/updates the index once. Both exit when done.
"""

import asyncio
import base64
import itertools
import os
import json
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import websockets

from notion_indexer import NOTION_DB, snapshot
from search_engine import Engine

CDP_TABS = "http://127.0.0.1:9222/json"
NOTION_BIN = "/Applications/Notion.app/Contents/MacOS/Notion"
DEBUG_FLAG = "--remote-debugging-port=9222"

EXEC = ThreadPoolExecutor(1)  # the engine (MLX + SQLite) lives on this one thread
ids = itertools.count(2)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ── Notion launcher ──────────────────────────────────────────────────────────

def notion_command():
    ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    return next((l for l in ps.splitlines() if l.startswith(NOTION_BIN)), None)


def ensure_debug_port():
    """If Notion runs without the debug port, quit it gracefully and reopen it with the port."""
    cmd = notion_command()
    if not cmd or DEBUG_FLAG in cmd:
        return False
    log("Notion was opened without the search hook: restarting it once")
    subprocess.run(["osascript", "-e", 'tell application "Notion" to quit'], capture_output=True)
    for _ in range(50):
        if not notion_command():
            break
        time.sleep(0.3)
    else:
        log("Notion did not quit; leaving it alone")
        return True
    subprocess.run(["open", "-a", "Notion", "--args", DEBUG_FLAG])
    return True


async def keep_notion_hooked():
    last = 0
    while True:
        if time.time() - last > 60 and await asyncio.to_thread(ensure_debug_port):  # never restart-loop
            last = time.time()
        await asyncio.sleep(2)


# ── Search hook ──────────────────────────────────────────────────────────────

async def answer(ws, paused, engine):
    loop, req = asyncio.get_running_loop(), paused["request"]
    body = None
    if urlparse(req["url"]).path == "/api/v3/search":
        try:
            query = json.loads(req.get("postData") or "{}").get("query", "").strip()
            if query:
                t = time.perf_counter()
                data = await loop.run_in_executor(EXEC, engine.v3, query)
                body = base64.b64encode(json.dumps(data).encode()).decode()
                log(f"'{query}': {len(data['results'])} results in {(time.perf_counter() - t) * 1000:.0f} ms")
        except Exception as e:
            log(f"search failed, letting Notion answer: {e!r}")
    if body is None:
        return await ws.send(json.dumps({"id": next(ids), "method": "Fetch.continueRequest",
                                         "params": {"requestId": paused["requestId"]}}))
    origin = req.get("headers", {}).get("Origin")
    headers = [{"name": "Content-Type", "value": "application/json; charset=utf-8"},
               {"name": "Access-Control-Allow-Origin", "value": origin or "*"}]
    if origin:
        headers.append({"name": "Access-Control-Allow-Credentials", "value": "true"})
    await ws.send(json.dumps({"id": next(ids), "method": "Fetch.fulfillRequest",
                              "params": {"requestId": paused["requestId"], "responseCode": 200,
                                         "responseHeaders": headers, "body": body}}))


async def serve_tab(tab, engine, active):
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Fetch.enable", "params": {
                "patterns": [{"urlPattern": "*api/v3/search*", "requestStage": "Request"}]}}))
            log(f"hooked tab: {tab.get('title', '?')[:40]}")
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("method") == "Fetch.requestPaused":
                    await answer(ws, msg["params"], engine)
    except Exception as e:
        log(f"tab closed: {e!r}"[:120])
    finally:
        active.discard(tab["id"])


def notion_tabs():
    try:
        with urllib.request.urlopen(CDP_TABS, timeout=2) as r:
            return [t for t in json.load(r) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                    and ("notion.so" in t["url"] or "notion.com" in t["url"])]
    except OSError:
        return []  # Notion not running (or not hooked yet)


async def watch_tabs(engine):
    active = set()
    while True:
        for tab in await asyncio.to_thread(notion_tabs):
            if tab["id"] not in active:
                active.add(tab["id"])
                asyncio.create_task(serve_tab(tab, engine, active))
        await asyncio.sleep(1)


# ── Background index sync ────────────────────────────────────────────────────

async def keep_index_fresh(engine):
    loop, seen, last = asyncio.get_running_loop(), 0, 0
    wal = NOTION_DB.with_name(NOTION_DB.name + "-wal")
    while True:
        await asyncio.sleep(5)
        try:
            stamp = max(p.stat().st_mtime for p in (NOTION_DB, wal) if p.exists())
            if stamp == seen or time.time() - last < 10:
                continue
            seen, last = stamp, time.time()
            t = time.perf_counter()
            snap = await asyncio.to_thread(snapshot)
            changed = await loop.run_in_executor(EXEC, engine.refresh, snap)
            if changed:
                log(f"index updated: {changed} pages in {(time.perf_counter() - t) * 1000:.0f} ms")
        except Exception as e:
            log(f"sync failed: {e!r}")


def bar(done, total, unit="chunks", scale=1):
    """Progress bar (a plain line per 10% when not on a terminal)."""
    shown = f"{done // scale}/{total // scale} {unit}"
    if sys.stdout.isatty():
        n = 30 * done // total
        print(f"\r  [{'█' * n}{'░' * (30 - n)}] {100 * done // total:3d}%  {shown}", end="", flush=True)
        if done == total:
            print()
    elif done == total or done * 10 // total != (done - total // 40) * 10 // total:
        print(f"{shown}", flush=True)


async def main():
    indexing = "--index" in sys.argv
    if "--download" in sys.argv:
        from embedder import model_path
        return model_path(lambda m: print(f"  {m}", flush=True),
                          lambda d, t: bar(d, t, "MB", 1_000_000))
    if indexing:
        print("  Loading the model into memory...", flush=True)
    engine = await asyncio.get_running_loop().run_in_executor(
        EXEC, lambda: Engine(bar if indexing else None))  # builds the index on first run
    log(f"ready: {len(engine.pages)} pages, {len(engine.chunk_page)} chunks indexed")
    if indexing:
        return
    await asyncio.gather(keep_notion_hooked(), watch_tabs(engine), keep_index_fresh(engine))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # the engine thread would otherwise run to the end before Python exits
        print("\n  Stopped.", flush=True)
        os._exit(130)
