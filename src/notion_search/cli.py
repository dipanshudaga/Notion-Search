"""The `notion-search` command: an arrow-key menu, plus install, status, restart, reindex, uninstall and logs."""

import importlib.metadata
import os
import re
import sqlite3
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path

from .embedder import DATA

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


def state_line():
    if not PLIST.exists():
        return f"{D}○ service not installed{N}"
    if service_pid():
        return f"{G}● running{N} {D}· {page_count()} pages indexed{N}"
    return f"{Y}○ installed, not running{N}"


# ── live checklist (installer) ────────────────────────────────────────────────

def bar_str(done, total, width=24):
    n = min(width, width * done // total)
    return f"{G}{'━' * n}{D}{'─' * (width - n)}{N} {100 * done // total:3d}%"


class Checklist:
    """One line per step, redrawn in place; the active step carries a progress bar."""

    def __init__(self, title, names):
        self.names, self.state, self.detail = names, ["pending"] * len(names), [""] * len(names)
        self.caption, self.drawn, self.active, self.t0 = "", False, 0, time.time()
        out(f"\n{D}┌{N}  {B}Notion Search{N}  {D}· {title}{N}\n{RAIL}")
        self.draw()

    def draw(self):
        if not TTY:
            return
        if self.drawn:
            print(f"\033[{len(self.names) + 2}A", end="")
        for name, st, det in zip(self.names, self.state, self.detail):
            name = f"{name:<30}"
            line = {"done": f"{G}✓{N} {name} {D}{det}{N}", "active": f"{G}■{N} {B}{name}{N} {det}",
                    "fail": f"{R}✗{N} {name} {R}{det}{N}"}.get(st, f"{D}□ {name}{N}")
            print(f"\033[K{RAIL}  {line}")
        print(f"\033[K{RAIL}\n\033[K{RAIL}  {D}{self.caption}{N}", flush=True)
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
            self.detail[self.active] = f"{bar_str(done, total)}  {D}{done // scale}/{total // scale} {unit}{N}"
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

def write_plist(python):
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>-u</string><string>-m</string><string>notion_search.service</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict></plist>
""")


def uv_output(*args):
    try:
        return subprocess.run(["uv", *args], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def running_from_uvx():
    """True when this process runs from uv's throwaway cache (uvx), which a background service must not point at."""
    cache = uv_output("cache", "dir")
    return bool(cache) and sys.prefix.startswith(cache)


def install_program(ui):
    """Install the notion-search command permanently with `uv tool`, showing a spinner. Returns the new Python path."""
    spec = os.environ.get("NOTION_SEARCH_SPEC") or f"notion-search=={importlib.metadata.version('notion-search')}"
    proc = subprocess.Popen(["uv", "tool", "install", "--python", "3.12", "--force", "--quiet", spec],
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


def do_install():
    if sys.platform != "darwin" or os.uname().machine != "arm64":
        return fail("Needs a Mac with Apple Silicon (MLX).")
    if not Path("/Applications/Notion.app").exists():
        return fail("Install Notion Desktop first (/Applications/Notion.app).")
    stop_service()
    subprocess.run(["pkill", "-f", "notion_search.service"], capture_output=True)
    temporary = running_from_uvx()
    names = ["Downloading the model", "Indexing pages", "Starting background services"]
    ui = Checklist("installer", (["Installing the notion-search command"] if temporary else []) + names)
    step, python = 0, sys.executable

    if temporary:  # started with `uvx notion-search install`: put the command and its environment somewhere permanent
        ui.start(step, "uv puts the program in its own environment")
        python = install_program(ui)
        if not python:
            return ui.failed()
        ui.done(step)
        step += 1

    ui.start(step, "Google EmbeddingGemma 2, downloaded from Hugging Face")
    if not run_service(ui, "--download", python):
        return ui.failed("download failed: check your internet")
    ui.done(step)

    ui.start(step + 1)
    if not run_service(ui, "--index", python):
        return ui.failed()
    ui.done(step + 1)

    ui.start(step + 2, "Registering the service that starts at every login")
    write_plist(python)
    if launchctl("bootstrap", DOMAIN, str(PLIST)).returncode:
        return ui.failed()
    ui.done(step + 2)

    out(f"{D}└{N}  {G}{B}All set.{N} Open Notion and press {B}Cmd+K{N}.")
    out(f"   {D}If Notion was already open, it restarts once so the search can attach.{N}")
    out(f"   {D}Manage it any time with:{N} notion-search")
    if temporary and (bin_dir := uv_output("tool", "dir", "--bin")) and bin_dir not in os.environ.get("PATH", "").split(":"):
        out(f"   {Y}!{N} To use that command, run {B}uv tool update-shell{N} and open a new Terminal window.")
    out()


def do_status():
    out(f"{RAIL}  {state_line()}")
    if PLIST.exists():
        ps = subprocess.run(["ps", "-axo", "command="], capture_output=True, text=True).stdout
        notion = "/Applications/Notion.app/Contents/MacOS/Notion"
        if f"{notion} --remote-debugging-port" in ps:
            ok("Notion is running with the search hook")
        elif notion in ps:
            out(f"{RAIL}  {Y}!{N} Notion is open without the hook; the service restarts it within seconds")
        else:
            out(f"{RAIL}  {D}Notion is not open{N}")
        if LOG.exists():
            out(RAIL)
            for line in LOG.read_text(errors="ignore").splitlines()[-6:]:
                out(f"{RAIL}  {D}{line}{N}")


def do_restart():
    if not PLIST.exists():
        return fail("Not installed.")
    launchctl("kickstart", "-k", f"{DOMAIN}/{LABEL}")
    ok("Restarted")


def do_reindex():
    if not PLIST.exists():
        return fail("Not installed.")
    stop_service()
    for f in DATA.glob("index.db*"):
        f.unlink()
    ui = Checklist("rebuilding the index", ["Indexing pages", "Starting background services"])
    ui.start(0)
    if not run_service(ui, "--index"):
        return ui.failed()
    ui.done(0)
    ui.start(1)
    if launchctl("bootstrap", DOMAIN, str(PLIST)).returncode:
        return ui.failed()
    ui.done(1)
    out(f"{D}└{N}  {G}{B}Done.{N} The service is running again.\n")


def program_remover():
    """The command that removes this installed program, or None when it was not installed by uv tool or pipx."""
    tools = uv_output("tool", "dir")
    if tools and sys.prefix.startswith(tools):
        return ["uv", "tool", "uninstall", "notion-search"]
    if "/pipx/venvs/" in sys.prefix:
        return ["pipx", "uninstall", "notion-search"]
    return None


def do_uninstall(purge=False):
    """Remove the service. With purge, also the index, the model and the notion-search command itself."""
    stop_service()
    PLIST.unlink(missing_ok=True)
    ok("Service removed (Notion itself was never modified)")
    remover = program_remover()
    if not purge:
        out(f"{RAIL}  {D}Kept the index and model: install again anytime.{N}")
        if remover:
            out(f"{RAIL}  {D}To remove the command too, choose Everything, or run:{N} {' '.join(remover)}")
        return
    import shutil

    shutil.rmtree(DATA, ignore_errors=True)
    LOG.unlink(missing_ok=True)
    ok("Index, model and log deleted")
    if remover:  # last on purpose: this deletes the environment this process runs from
        done = subprocess.run(remover, capture_output=True, text=True)
        ok("The notion-search command removed") if done.returncode == 0 else fail(f"Could not remove the command: run {' '.join(remover)}")
    else:
        out(f"{RAIL}  {D}Remove the program with the tool you installed it with (pip uninstall notion-search).{N}")
    out(f"{D}└{N}  {G}{B}Notion Search is gone.{N} Notion itself was never modified.")


# ── menu (clack-style) ────────────────────────────────────────────────────────

def header():
    print(f"\033[H\033[2J\n{D}┌{N}  {B}Notion Search{N}\n{RAIL}  {state_line()}\n{RAIL}", flush=True)


def read_key():
    ch = os.read(sys.stdin.fileno(), 1)
    if ch == b"\x1b":
        rest = os.read(sys.stdin.fileno(), 2)
        return {b"[A": "up", b"[B": "down"}.get(rest, "esc")
    return {b"\r": "enter", b"\n": "enter", b"k": "up", b"j": "down", b"q": "esc", b"\x03": "esc"}.get(ch, "")


def pick(title, options):
    """options: [(label, description)]. Returns the chosen index, or None on q / esc."""
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
                    print(f"{RAIL}  {G}■{N} {B}{label:<10}{N} {D}{desc}{N}")
                else:
                    print(f"{RAIL}  {D}□ {label:<10} {desc}{N}")
            print(f"{D}└{N}  {D}↑↓ move · enter select · q quit{N}", flush=True)
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


def menu():
    while True:
        first = ("Update", "Reinstall the service and refresh the index") if PLIST.exists() \
            else ("Install", "Set up the search service and index your Notion")
        choice = pick("What would you like to do?", [
            first,
            ("Status", "Is it running, and is Notion hooked?"),
            ("Restart", "Restart the background service"),
            ("Reindex", "Re-read all your Notion pages from scratch"),
            ("Uninstall", "Remove the service (Notion is never touched)"),
            ("Quit", "Close this menu")])
        if choice in (None, 5):
            break
        header()
        if choice == 4:
            sub = pick("What should be removed?", [
                ("Service", "The background service only; keep the index and model"),
                ("Everything", "Also the index, the model and the notion-search command"),
                ("Cancel", "Go back")])
            if sub in (None, 2):
                continue
            header()
            do_uninstall(purge=sub == 1)
            if sub == 1:  # the command is gone: nothing left to show a menu for
                return
        else:
            [do_install, do_status, do_restart, do_reindex][choice]()
        wait_key()
    print("\033[H\033[2J", end="")


COMMANDS = {
    "install": do_install, "status": do_status, "restart": do_restart, "reindex": do_reindex,
    "uninstall": lambda: do_uninstall("--purge" in sys.argv),
    "logs": lambda: os.execvp("tail", ["tail", "-f", str(LOG)]),
}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "menu"
    try:
        if cmd == "menu" and TTY and sys.stdin.isatty():
            menu()
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
