"""Background service: answers Notion Desktop's own search box from the local index.

  * keeps Notion started with the Chromium debug port (restarts it once if you opened it without)
  * hooks each Notion tab over that port and answers /api/v3/search from the local index (~25 ms, offline)
  * re-indexes in the background whenever Notion's local database changes

Notion's app files are never touched, so its signature (and the Keychain) stay as shipped.
`python -m notion_search.service --download` fetches the model, `--index` builds/updates the index once. Both exit when done.
"""

import asyncio
import base64
import itertools
import os
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import websockets

from .engine import Engine
from .indexer import NOTION_DB, snapshot

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

def page_id_from_url(url):
    """The page open in Notion, from the request's Referer: .../Title-<32 hex> or a peek view (?p=<32 hex>)."""
    if not url:
        return None
    u = urlparse(url)
    found = re.search(r"[0-9a-f]{32}", (parse_qs(u.query).get("p") or [""])[0]) or re.search(r"[0-9a-f]{32}", u.path)
    h = found.group() if found else None
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}" if h else None


BYPASS = "x-ns-bypass"  # marks our own call to Notion's search so the hook lets it through
CLOUD_WAIT = 1.0  # seconds to wait for Notion's own answer; offline or slow, the local answer goes alone
NO_JS_HEADERS = {"host", "cookie", "origin", "referer", "user-agent", "content-length", "connection", "accept-encoding"}


async def cloud_search(ws, pending, req):
    """Notion's own answer to this query, fetched through the tab (it carries your login), or None."""
    headers = {k: v for k, v in req.get("headers", {}).items()
               if k.lower() not in NO_JS_HEADERS and not k.lower().startswith("sec-")}
    headers[BYPASS] = "1"
    js = ("fetch('/api/v3/search',{method:'POST',credentials:'include',headers:%s,body:%s})"
          ".then(r=>r.ok?r.text():null).catch(()=>null)") % (json.dumps(headers), json.dumps(req["postData"]))
    mid, fut = next(ids), asyncio.get_running_loop().create_future()
    pending[mid] = fut
    try:
        await ws.send(json.dumps({"id": mid, "method": "Runtime.evaluate",
                                  "params": {"expression": js, "awaitPromise": True, "returnByValue": True}}))
        text = (await asyncio.wait_for(fut, CLOUD_WAIT)).get("result", {}).get("result", {}).get("value")
        return json.loads(text) if text else None
    except (asyncio.TimeoutError, ValueError, OSError):
        return None
    finally:
        pending.pop(mid, None)


async def answer(ws, paused, engine, pending):
    loop, req = asyncio.get_running_loop(), paused["request"]
    headers = req.get("headers", {})
    if BYPASS in {k.lower() for k in headers}:  # our own call: send it on, without the marker
        return await ws.send(json.dumps({"id": next(ids), "method": "Fetch.continueRequest", "params": {
            "requestId": paused["requestId"],
            "headers": [{"name": k, "value": v} for k, v in headers.items() if k.lower() != BYPASS]}}))
    body = None
    if urlparse(req["url"]).path == "/api/v3/search":
        try:
            query = json.loads(req.get("postData") or "{}").get("query", "").strip()
            if query:
                t = time.perf_counter()
                current = page_id_from_url(headers.get("Referer") or headers.get("referer"))
                cloud = asyncio.create_task(cloud_search(ws, pending, req))  # runs while the local search does
                data = await loop.run_in_executor(EXEC, engine.v3, query, 12, current)
                extra = 0
                if (found := await cloud):
                    mine = {r["id"] for r in data["results"]}
                    data = await loop.run_in_executor(EXEC, engine.merge, data, found)
                    extra = sum(r["id"] not in mine for r in data["results"])
                body = base64.b64encode(json.dumps(data).encode()).decode()
                log(f"'{query}': {len(data['results'])} results in {(time.perf_counter() - t) * 1000:.0f} ms"
                    + (f" (Notion added {extra} not on this Mac yet)" if extra > 0 else ""))
        except Exception as e:
            log(f"search failed, letting Notion answer: {e!r}")
    if body is None:
        return await ws.send(json.dumps({"id": next(ids), "method": "Fetch.continueRequest",
                                         "params": {"requestId": paused["requestId"]}}))
    origin = headers.get("Origin")
    out = [{"name": "Content-Type", "value": "application/json; charset=utf-8"},
           {"name": "Access-Control-Allow-Origin", "value": origin or "*"}]
    if origin:
        out.append({"name": "Access-Control-Allow-Credentials", "value": "true"})
    await ws.send(json.dumps({"id": next(ids), "method": "Fetch.fulfillRequest",
                              "params": {"requestId": paused["requestId"], "responseCode": 200,
                                         "responseHeaders": out, "body": body}}))


async def serve_tab(tab, engine):
    pending = {}  # id of a command we sent -> future for its reply
    try:
        async with websockets.connect(tab["webSocketDebuggerUrl"], max_size=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Fetch.enable", "params": {
                "patterns": [{"urlPattern": "*api/v3/search*", "requestStage": "Request"}]}}))
            log(f"hooked tab: {tab.get('title', '?')[:40]}")
            async for raw in ws:
                msg = json.loads(raw)
                if (fut := pending.get(msg.get("id"))) and not fut.done():
                    fut.set_result(msg)
                elif msg.get("method") == "Fetch.requestPaused":
                    asyncio.create_task(answer(ws, msg["params"], engine, pending))  # not awaited: it waits on replies read here
    except Exception as e:
        log(f"tab closed: {e!r}"[:120])


def notion_tabs():
    try:
        with urllib.request.urlopen(CDP_TABS, timeout=2) as r:
            return [t for t in json.load(r) if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
                    and ("notion.so" in t["url"] or "notion.com" in t["url"])]
    except OSError:
        return []  # Notion not running (or not hooked yet)


async def watch_tabs(engine):
    tasks, tick = {}, time.time()
    loop = asyncio.get_running_loop()
    while True:
        now, slept = time.time(), time.time() - tick > 5  # a long gap between ticks: the Mac was asleep
        tick = now
        if slept:  # sockets made before sleep are dead but look open: drop them, and page the model back in
            log("woke from sleep")
            for task in tasks.values():
                task.cancel()
            tasks.clear()
            loop.run_in_executor(EXEC, engine.warm)
        tasks = {tid: t for tid, t in tasks.items() if not t.done()}
        for tab in await asyncio.to_thread(notion_tabs):
            if tab["id"] not in tasks:
                tasks[tab["id"]] = asyncio.create_task(serve_tab(tab, engine))
        await asyncio.sleep(1)


async def keep_warm(engine):
    """A search after a few idle minutes took 2 to 9 s on an 8 GB Mac: macOS had compressed or swapped out the model's
    memory. A tiny search every 20 s keeps those pages in use (about 25 ms of GPU time)."""
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(20)
        await loop.run_in_executor(EXEC, engine.warm)


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


# ── Self-update ──────────────────────────────────────────────────────────────

RELEASE_AGE = 3600  # a release must be on PyPI this many seconds before it is installed (a bad one gets pulled quickly)
LABEL = "com.notionsearch.service"


def release_to_install():
    """The newest version on PyPI if it is newer than this one and has been out for an hour, else None."""
    with urllib.request.urlopen("https://pypi.org/pypi/notion-search/json", timeout=10) as r:
        info = json.load(r)
    latest = info["info"]["version"]
    files = info["releases"].get(latest) or []
    if not files:
        return None
    up = datetime.fromisoformat(files[0]["upload_time_iso_8601"].replace("Z", "+00:00"))
    as_tuple = lambda v: tuple(int(x) for x in re.findall(r"\d+", v))
    old = (datetime.now(timezone.utc) - up).total_seconds() >= RELEASE_AGE
    from importlib.metadata import version
    return latest if old and as_tuple(latest) > as_tuple(version("notion-search")) else None


def install_release(latest):
    """Install it the way Update does. Only for a copy installed by `uv tool`; returns True when it went in."""
    home = Path.home()
    uv = shutil.which("uv", path=os.pathsep.join(
        [os.environ.get("PATH", ""), str(home / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]))  # launchd's PATH is bare
    if not uv:
        return False
    tools = subprocess.run([uv, "tool", "dir"], capture_output=True, text=True).stdout.strip()
    if not tools or Path(sys.prefix).resolve() != (Path(tools) / "notion-search").resolve():
        return False  # installed some other way (pipx, a checkout): leave it to the user
    done = subprocess.run([uv, "tool", "install", "--python", "3.12", "--force", "--quiet", "--refresh-package",
                           "notion-search", f"notion-search=={latest}"], capture_output=True, text=True, timeout=900)
    return done.returncode == 0


async def keep_updated():
    """Every 6 hours: if a release is more than an hour old, install it and restart on the new code."""
    await asyncio.sleep(600)
    while True:
        try:
            latest = await asyncio.to_thread(release_to_install)
            if latest and await asyncio.to_thread(install_release, latest):
                log(f"updated to {latest}, restarting")
                subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"])
        except Exception as e:
            log(f"update check failed: {e!r}"[:120])
        await asyncio.sleep(6 * 3600)


PROTOCOL = bool(os.environ.get("NS_PROTOCOL"))  # set by the installer, which draws the progress itself


def say(message):
    print(f"@say {message}" if PROTOCOL else f"  {message}", flush=True)


def bar(done, total, unit="chunks", scale=1):
    """Progress bar (a plain line per 10% when not on a terminal)."""
    if PROTOCOL:
        return print(f"@progress {done} {total} {unit} {scale}", flush=True)
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
        from .embedder import model_path
        return model_path(say, lambda d, t: bar(d, t, "MB", 1_000_000))
    if indexing:
        say("Loading the model into memory")
    engine = await asyncio.get_running_loop().run_in_executor(
        EXEC, lambda: Engine(bar if indexing else None))  # builds the index on first run
    log(f"ready: {len(engine.pages)} pages, {len(engine.chunk_page)} chunks indexed")
    if indexing:
        return
    await asyncio.gather(keep_notion_hooked(), watch_tabs(engine), keep_index_fresh(engine), keep_warm(engine), keep_updated())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # the engine thread would otherwise run to the end before Python exits
        print("\n  Stopped.", flush=True)
        os._exit(130)
