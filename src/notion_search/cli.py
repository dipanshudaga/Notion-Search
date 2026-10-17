"""The `notion-search` command: an arrow-key menu, plus install, update, status, restart, reindex, uninstall and logs."""

import importlib.metadata
import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
import termios
import time
import tty
import urllib.request
from pathlib import Path

from .embedder import DATA, MODEL_DIR

LABEL = "com.notionsearch.service"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library/Logs/NotionSearch.log"
DOMAIN = f"gui/{os.getuid()}"
TTY = sys.stdout.isatty()

B, D, C, G, R, Y, N = ("\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[31m", "\033[33m", "\033[0m") if TTY else ("",) * 7
RAIL = f"{D}│{N}"


def out(text=""):
    print(text, flush=True)


def ok(msg):
    out(f"{RAIL}  {G}✓{N} {msg}")


def fail(msg):
    out(f"{RAIL}  {R}✗{N} {msg}")


def launchctl(*args):
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def service_pid():
    m = re.search(r"^\s*pid = (\d+)", launchctl("print", f"{DOMAIN}/{LABEL}").stdout, re.M)
    return m.group(1) if m else None


def page_count():
    try:
        with sqlite3.connect(DATA / "index.db") as db:
            return db.execute("select count(*) from pages").fetchone()[0]
    except sqlite3.Error:
        return 0


def stop_service():
    launchctl("bootout", f"{DOMAIN}/{LABEL}")


def version():
    return importlib.metadata.version("notion-search")


def state_line():
    if not PLIST.exists():
        return f"{D}○ service not installed{N}"
    if service_pid():
        return f"{G}● running{N} {D}· {page_count()} pages indexed{N}"
    return f"{Y}○ not running{N} {D}· Restart starts it{N}"


# ── live checklist (installer) ────────────────────────────────────────────────

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clip(text, width):
    """Cut text to `width` visible characters, keeping colour codes intact. A line that wraps in a narrow window would
    break the redraw, which moves the cursor up by a fixed number of lines."""
    out, seen, i = [], 0, 0
    while i < len(text):
        code = ANSI.match(text, i)
        if code:
            out.append(code.group())
            i = code.end()
        elif seen >= width - 1:
            return "".join(out) + "…" + N
        else:
            out.append(text[i])
            seen += 1
            i += 1
    return "".join(out)


def bar_str(done, total, width=24):
    n = min(width, width * done // total)
    return f"{G}{'━' * n}{D}{'─' * (width - n)}{N} {100 * done // total:3d}%"


class Checklist:
    """One line per step, redrawn in place; the active step carries a progress bar."""

    def __init__(self, title, names):
        self.names, self.state, self.detail = names, ["pending"] * len(names), [""] * len(names)
        self.pad = max(len(n) for n in names)  # one column of names, as wide as the longest
        self.caption, self.drawn, self.active, self.t0 = "", False, 0, time.time()
        out(f"\n{D}┌{N}  {B}Notion Search{N}  {D}· {title}{N}\n{RAIL}")
        self.draw()

    def draw(self):
        if not TTY:
            return
        cols = shutil.get_terminal_size().columns
        if self.drawn:
            print(f"\033[{len(self.names) + 2}A", end="")
        below = ""  # on a narrow window the active step's bar or spinner moves to the line under the list
        for name, st, det in zip(self.names, self.state, self.detail):
            name = f"{name:<{self.pad}}"
            if st == "active" and det and len(ANSI.sub("", f"{RAIL}  ■ {name} {det}")) > cols - 1:
                below, det = det, ""
            line = {"done": f"{G}✓{N} {name} {D}{det}{N}", "active": f"{G}■{N} {B}{name}{N} {det}",
                    "fail": f"{R}✗{N} {name} {R}{det}{N}"}.get(st, f"{D}□ {name}{N}")
            print(f"\033[K{clip(f'{RAIL}  {line}', cols)}")
        print(f"\033[K{RAIL}\n\033[K{clip(f'{RAIL}  ' + (below or f'{D}{self.caption}{N}'), cols)}", flush=True)
        self.drawn = True

    def start(self, i, caption=""):
        self.active, self.state[i], self.detail[i], self.caption, self.t0 = i, "active", "", caption, time.time()
        if TTY:
            self.draw()
        else:
            out(f"[{i + 1}/{len(self.names)}] {self.names[i]}...")

    def done(self, i, detail=None):
        self.state[i], self.caption = "done", ""
        self.detail[i] = detail or f"{time.time() - self.t0:.0f}s"
        self.draw()

    def failed(self, detail="failed"):
        self.state[self.active], self.detail[self.active] = "fail", detail
        self.draw()

    def progress(self, done, total, unit, scale):
        if total > 0:
            counts = f"{done // scale}/{total // scale} {unit}"
            cols = shutil.get_terminal_size().columns
            inline = cols - (6 + self.pad + 1) - len(counts) - 8  # room beside the name: rail, marker, gaps, percent
            width = 24 if inline >= 24 else max(6, min(24, cols - 6 - len(counts) - 8))  # else it goes under the list
            self.detail[self.active] = f"{bar_str(done, total, width)}  {D}{counts}{N}"
            self.draw()


def run_service(ui, flag, python=None):
    """Run `python -m notion_search.service FLAG`, turning its @say / @progress lines into checklist updates."""
    env = {**os.environ, "NS_PROTOCOL": "1", "PYTHONWARNINGS": "ignore"}
    proc = subprocess.Popen([python or sys.executable, "-u", "-m", "notion_search.service", flag], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    other = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("@say "):
            ui.caption = line[5:]
            ui.draw()
        elif line.startswith("@progress "):
            done, total, unit, scale = line[10:].split()
            ui.progress(int(done), int(total), unit, int(scale))
        else:
            other.append(line)
    if proc.wait() != 0:
        out(f"\n{RAIL}  {R}Something went wrong. Last lines:{N}")
        for line in other[-12:]:
            out(f"{RAIL}    {line}")
        return False
    return True


# ── commands ──────────────────────────────────────────────────────────────────

def render_plist(python):
    """The LaunchAgent. It runs the `notion-search serve` script, so macOS names the background item after the app."""
    program = Path(python).parent / "notion-search"
    args = f"<string>{program}</string><string>serve</string>" if program.exists() else \
           f"<string>{python}</string><string>-u</string><string>-m</string><string>notion_search.service</string>"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict></plist>
"""


def register_service(python):
    """Start the service. If the registration is unchanged and loaded, just restart it: re-registering the same
    LaunchAgent is what makes macOS show its "App Background Activity" notification again."""
    text = render_plist(python)
    loaded = launchctl("print", f"{DOMAIN}/{LABEL}").returncode == 0
    if loaded and PLIST.exists() and PLIST.read_text() == text:
        return launchctl("kickstart", "-k", f"{DOMAIN}/{LABEL}").returncode == 0
    stop_service()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(text)
    return launchctl("bootstrap", DOMAIN, str(PLIST)).returncode == 0


def uv_output(*args):
    try:
        return subprocess.run(["uv", *args], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def running_from_uvx():
    """True when this process runs from uv's throwaway cache (uvx), which a background service must not point at."""
    cache = uv_output("cache", "dir")
    return bool(cache) and sys.prefix.startswith(cache)


def newest_known_version():
    """The newest release on PyPI, or this copy's own version when PyPI is unreachable. A uvx run may use an old cached
    copy of the program, and installing that version permanently would be a downgrade."""
    mine = importlib.metadata.version("notion-search")
    latest = latest_version()
    return latest if latest and version_tuple(latest) > version_tuple(mine) else mine


def install_program(ui, spec=None):
    """Install the notion-search command permanently with `uv tool`, showing a spinner. Returns the new Python path."""
    spec = spec or os.environ.get("NOTION_SEARCH_SPEC") or f"notion-search=={newest_known_version()}"
    proc = subprocess.Popen(["uv", "tool", "install", "--python", "3.12", "--force", "--quiet", "--refresh-package", "notion-search", spec],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    frames, f, t = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", 0, time.time()
    while proc.poll() is None:
        ui.detail[ui.active] = f"{G}{frames[f % 10]}{N} {D}installing packages · {time.time() - t:.0f}s{N}"
        ui.draw()
        f += 1
        time.sleep(0.12)
    if proc.returncode:
        out(f"\n{RAIL}  {R}Something went wrong. Last lines:{N}")
        for line in proc.stdout.read().splitlines()[-12:]:
            out(f"{RAIL}    {line}")
        return None
    return str(Path(uv_output("tool", "dir")) / "notion-search" / "bin" / "python")


def needs_model():
    return not ((MODEL_DIR / "model.safetensors").exists() and (MODEL_DIR / "tokenizer.json").exists())


def needs_index():
    """True when there is no index yet, or it was built by a version with a different layout or model."""
    from .indexer import FINGERPRINT

    if not (DATA / "index.db").exists():
        return True
    try:
        with sqlite3.connect(DATA / "index.db") as db:
            row = db.execute("SELECT v FROM meta WHERE k = 'fingerprint'").fetchone()
    except sqlite3.Error:
        return True
    return not row or row[0] != FINGERPRINT


def plan_steps(temporary, restarting):
    """The steps this run really needs: an update on a working install is often just a service restart."""
    steps = []
    if temporary:
        steps.append(("program", "Installing the notion-search command"))
    if needs_model():
        steps.append(("model", "Downloading the model"))
    if needs_index():
        steps.append(("index", "Indexing pages"))
    steps.append(("service", "Restarting background services" if restarting else "Starting background services"))
    return steps


def do_install():
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        return fail("Notion Search needs a Mac with an Apple Silicon chip.")
    if not Path("/Applications/Notion.app").exists():
        return fail("Install Notion Desktop first. It must be in the Applications folder.")
    restarting = PLIST.exists()
    temporary = running_from_uvx()
    steps = plan_steps(temporary, restarting)
    if any(key == "index" for key, _ in steps):  # the old service must not write the index while it is rebuilt
        stop_service()
        subprocess.run(["pkill", "-f", "notion_search.service"], capture_output=True)
    updating = bool(os.environ.get("NS_UPDATING"))
    ui = Checklist("update" if updating else "installer", [name for _, name in steps])
    python = sys.executable

    for i, (key, _) in enumerate(steps):
        if key == "program":  # started with `uvx notion-search install`: put the command somewhere permanent
            ui.start(i, "uv keeps the program in its own folder")
            python = install_program(ui)
            if not python:
                return ui.failed()
        elif key == "model":
            ui.start(i, "Google EmbeddingGemma 2, downloaded from Hugging Face")
            if not run_service(ui, "--download", python):
                return ui.failed("download failed: check your internet")
        elif key == "index":
            ui.start(i, "Reading your Notion pages")
            if not run_service(ui, "--index", python):
                return ui.failed()
        else:
            ui.start(i, "Setting up the service that starts when you log in")
            if not register_service(python):
                return ui.failed()
        ui.done(i)

    version = importlib.metadata.version("notion-search")
    if updating:
        out(f"{D}└{N}  {G}{B}Updated to {version}.{N} Notion Search is running.")
    else:
        out(f"{D}└{N}  {G}{B}All set.{N} Open Notion and press {B}Cmd+K{N}.")
        out(f"   {D}If Notion was already open, it restarts once so the search can attach.{N}")
        out(f"   {D}Manage it any time with:{N} notion-search")
    if temporary and (bin_dir := uv_output("tool", "dir", "--bin")) and bin_dir not in os.environ.get("PATH", "").split(":"):
        out(f"   {Y}!{N} To use that command, run {B}uv tool update-shell{N} and open a new Terminal window.")
    out()


def latest_version():
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/notion-search/json", timeout=5) as r:
            return json.load(r)["info"]["version"]
    except (OSError, ValueError, KeyError):
        return None


def version_tuple(v):
    return tuple(int(n) for n in re.findall(r"\d+", v))


def do_update():
    """Get the newest release from PyPI if there is one, then run the installer with the new code."""
    current, latest = importlib.metadata.version("notion-search"), latest_version()
    newer = bool(latest) and version_tuple(latest) > version_tuple(current)
    tool = program_remover()
    if newer and tool and tool[0] == "uv" and not running_from_uvx():
        ui = Checklist("update", [f"Updating {current} → {latest}"])
        ui.start(0, "uv installs the new version in its own environment")
        python = install_program(ui, f"notion-search=={latest}")
        if not python:
            return ui.failed()
        ui.done(0)
        out(f"{D}└{N}  {G}Updated.{N} Now refreshing the service with the new version.\n")
        os.environ["NS_UPDATING"] = "1"
        os.execv(python, [python, "-m", "notion_search.cli", "install"])  # the new code runs the setup
    if newer and tool:
        out(f"{RAIL}  {Y}!{N} Version {latest} is available and you have {current}. Upgrade with: {B}pipx upgrade notion-search{N}")
    elif latest:
        out(f"{RAIL}  {G}✓{N} You have the newest version ({current}).")
    else:
        out(f"{RAIL}  {D}Could not check for a new version. Are you offline? Refreshing what is installed.{N}")
    do_install()


def do_status():
    """Whether Notion is linked. The service state is already on screen above (or printed by the command)."""
    if not PLIST.exists():
        return
    if not service_pid():
        return out(f"{RAIL}  {Y}!{N} The search is off, so Notion uses its own. Choose Restart to start it.")
    ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
    notion = "/Applications/Notion.app/Contents/MacOS/Notion"
    if f"{notion} --remote-debugging-port" in ps:
        ok("Notion is linked to Notion Search.")
    elif notion in ps:
        out(f"{RAIL}  {Y}!{N} Notion is open without the search link. The service restarts it in a few seconds.")
    else:
        out(f"{RAIL}  {D}Notion is not open.{N}")


def do_restart():
    if not PLIST.exists():
        return fail("Notion Search is not installed.")
    if register_service(sys.executable):  # restarts it if loaded, starts it if not
        ok("Restarted")
    else:
        fail("The service did not start. Run notion-search logs to see why.")


def do_reindex():
    if not PLIST.exists():
        return fail("Notion Search is not installed.")
    stop_service()
    for f in DATA.glob("index.db*"):
        f.unlink()
    ui = Checklist("rebuilding the index", ["Indexing pages", "Starting background services"])
    try:
        ui.start(0)
        if not run_service(ui, "--index"):
            return ui.failed()
        ui.done(0)
        ui.start(1)
        if not register_service(sys.executable):
            return ui.failed()
        ui.done(1)
    except KeyboardInterrupt:  # the index is saved in one step at the end, so an interrupted run leaves none
        out(f"\n{RAIL}  {Y}Stopped.{N} Nothing was saved. The service is starting and builds the index itself.")
        register_service(sys.executable)
        return out(f"{D}└{N}  This takes about 3 minutes. Until then Notion uses its own search.\n")
    out(f"{D}└{N}  {G}{B}Done.{N} The service is running again.\n")


def program_remover():
    """The command that removes this installed program, or None when it was not installed by uv tool or pipx."""
    tools = uv_output("tool", "dir")
    if tools and sys.prefix.startswith(tools):
        return ["uv", "tool", "uninstall", "notion-search"]
    if "/pipx/venvs/" in sys.prefix:
        return ["pipx", "uninstall", "notion-search"]
    return None


def do_uninstall(keep_data=False):
    """Remove Notion Search. By default everything: the service, index, model, log, old installer folders and the
    notion-search command. keep_data=True removes only the service. uv's shared cache is left alone: other uv
    programs can hold its lock for hours, and `uv cache clean` would wait for them."""
    stop_service()
    PLIST.unlink(missing_ok=True)
    ok("Removed the background service.")
    remover = program_remover()
    if keep_data:
        out(f"{RAIL}  {D}Kept the index, the model and the command. You can install again at any time.{N}")
        return
    import shutil

    for path in (DATA, Path.home() / ".notion-search"):  # the second is where the old one-line installer put files
        shutil.rmtree(path, ignore_errors=True)
    LOG.unlink(missing_ok=True)
    (Path.home() / ".local/bin/notion-search").unlink(missing_ok=True) if not remover else None
    ok("Deleted the index, the model and the log.")
    if remover:  # last on purpose: this deletes the environment this process runs from
        done = subprocess.run(remover, capture_output=True, text=True)
        ok("Removed the notion-search command.") if done.returncode == 0 else fail(f"Could not remove the command. Run this: {' '.join(remover)}")
    else:
        out(f"{RAIL}  {D}Remove the program with the tool that installed it, for example: pip uninstall notion-search{N}")
    out(f"{D}└{N}  {G}{B}Notion Search is removed.{N} Your Notion pages and app are not changed.")


def confirm_uninstall():
    """Ask once, in plain words. Returns True to go ahead."""
    if "--yes" in sys.argv:
        return True
    if not (TTY and sys.stdin.isatty()):
        out("To remove Notion Search without a question, add --yes.")
        return False
    out(f"{RAIL}  This removes the background service, the index, the model and the")
    out(f"{RAIL}  notion-search command. Your Notion pages and app stay as they are.")
    return input(f"{D}└{N}  Remove Notion Search? {D}[y/N]{N} ").strip().lower() in ("y", "yes")


# ── menu (clack-style) ────────────────────────────────────────────────────────

def header():
    print(f"\033[H\033[2J\n{D}┌{N}  {B}Notion Search{N} {D}{version()}{N}\n{RAIL}  {state_line()}\n{RAIL}", flush=True)


def read_key():
    fd = sys.stdin.fileno()
    ch = os.read(fd, 1)
    if ch == b"\x1b":  # an arrow key sends Esc plus two more bytes at once; a lone Esc sends nothing after it
        rest = os.read(fd, 2) if select.select([fd], [], [], 0.05)[0] else b""
        return {b"[A": "up", b"[B": "down"}.get(rest, "esc" if not rest else "")
    return {b"\r": "enter", b"\n": "enter", b"k": "up", b"j": "down", b"\x03": "esc"}.get(ch, "")


def pick(title, options):
    """options: [(label, description)]. Returns the chosen index, or None on esc."""
    sel, fd = 0, sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    print("\033[?25l", end="")
    try:
        tty.setcbreak(fd)
        while True:
            header()
            width = max(10, os.get_terminal_size().columns - 20)  # room for the rail, marker and label column
            print(f"{C}◆{N}  {title}")
            for i, (label, desc) in enumerate(options):
                desc = desc if len(desc) <= width else desc[:width - 1] + "…"
                if i == sel:
                    print(f"{RAIL}  {G}■{N} {B}{label:<11}{N} {D}{desc}{N}")
                else:
                    print(f"{RAIL}  {D}□ {label:<11} {desc}{N}")
            print(f"{D}└{N}  {D}↑↓ move · enter select · esc quit{N}", flush=True)
            key = read_key()
            if key == "up":
                sel = (sel - 1) % len(options)
            elif key == "down":
                sel = (sel + 1) % len(options)
            elif key == "enter":
                return sel
            elif key == "esc":
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        print("\033[?25h", end="")


def wait_key():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    print(f"{RAIL}\n{D}└{N}  {D}Press any key to go back{N}", end="", flush=True)
    try:
        tty.setcbreak(fd)
        os.read(fd, 1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def menu_options():
    """Only what makes sense right now: a full menu once installed, a short one before (or after a partial removal)."""
    if PLIST.exists():
        return [("Update", "Get the newest version and refresh the service"),
                ("Status", "Is it running, and linked to Notion?"),
                ("Restart", "Restart the background service"),
                ("Reindex", "Re-read all your Notion pages from scratch"),
                ("Uninstall", "Remove the service"),
                ("Quit", "Close this menu")]
    options = [("Install", "Set up the search service and index your Notion")]
    if DATA.exists():  # something is left over from an earlier install
        options.append(("Remove", "Delete the index, the model and the command"))
    return options + [("Quit", "Close this menu")]


def menu():
    while True:
        options = menu_options()
        choice = pick("What would you like to do?", options)
        label = options[choice][0] if choice is not None else "Quit"
        if label == "Quit":
            break
        header()
        if label in ("Uninstall", "Remove"):
            sure = pick("Remove Notion Search completely?", [
                ("Yes, remove", ""),
                ("Cancel", "")])
            if sure != 0:
                continue
            header()
            do_uninstall()
            return  # the command is gone: nothing left to show a menu for
        else:
            {"Install": do_install, "Update": do_update, "Status": do_status,
             "Restart": do_restart, "Reindex": do_reindex}[label]()
        wait_key()
    print("\033[H\033[2J", end="")


def serve():
    """Run the background service in the foreground (this is what the LaunchAgent starts)."""
    import asyncio

    from .service import main as service_main

    try:
        asyncio.run(service_main())
    except KeyboardInterrupt:
        os._exit(130)


COMMANDS = {
    "serve": serve,
    "install": do_install, "update": do_update, "status": lambda: (out(f"{B}Notion Search{N} {D}{version()}{N}"), out(state_line()), do_status()), "restart": do_restart, "reindex": do_reindex,
    "uninstall": lambda: do_uninstall(keep_data="--keep-data" in sys.argv) if confirm_uninstall() else out("Nothing was removed."),
    "logs": lambda: os.execvp("tail", ["tail", "-f", str(LOG)]),
}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "menu"
    permanent = Path(uv_output("tool", "dir") or "/nonexistent") / "notion-search" / "bin" / "python"
    if cmd != "install" and PLIST.exists() and permanent.exists() and running_from_uvx():
        os.execv(str(permanent), [str(permanent), "-m", "notion_search.cli", *sys.argv[1:]])  # use the installed copy
    try:
        if cmd == "menu" and TTY and sys.stdin.isatty():
            do_install() if not (PLIST.exists() or DATA.exists()) else menu()  # very first run: just set everything up
        elif cmd in COMMANDS:
            COMMANDS[cmd]()
        else:
            print(__doc__)
            sys.exit(cmd != "menu")
    except KeyboardInterrupt:
        print("\n  Stopped.")
        sys.exit(130)


if __name__ == "__main__":
    main()
