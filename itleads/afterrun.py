"""Run a command of your own after each finished run, for example to refresh the read-only copy on Vercel.

Set "after_run" in config.json (for example "/Users/you/it-leads/deploy/vercel/deploy.sh"). It is read from that file only,
never from the web pages, and run without a shell. It starts only after a run that really refreshed the list."""
from __future__ import annotations

import shlex
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from . import config

TIMEOUT = 900                                                  # seconds; a stuck command must not hold anything
EXTRA_PATH = ("/usr/local/bin", "/opt/homebrew/bin", str(Path.home() / ".npm-global" / "bin"), "/usr/bin", "/bin")
_state = threading.Lock()
_running = False                                               # one command at a time
_again = False                                                 # a run ended while the command was still going

def command(cfg: dict) -> list:
    raw = cfg.get("after_run") or ""
    if isinstance(raw, str):
        return shlex.split(raw)
    return [str(part) for part in raw]


def environment() -> dict:
    """A background service has a bare PATH; tools such as vercel and node live elsewhere."""
    import os
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(dict.fromkeys([*env.get("PATH", "").split(os.pathsep), *EXTRA_PATH]))
    return env


def _record(text: str) -> None:
    try:
        config.ensure_dirs()
        with open(config.LOGS / "after-run.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')}  {text.rstrip()}\n")
    except OSError:
        pass


def _once(cmd: list) -> None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=environment(), cwd=str(config.ROOT))
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
        _record(f"{' '.join(cmd)}: " + ("done" if r.returncode == 0 else f"exit {r.returncode}") + (f"\n{tail}" if r.returncode else ""))
    except subprocess.TimeoutExpired:
        _record(f"{' '.join(cmd)}: stopped after {TIMEOUT} seconds")
    except OSError as e:
        _record(f"{' '.join(cmd)}: could not start ({e})")


def _work(cmd: list) -> None:
    global _running, _again
    try:
        while True:
            _once(cmd)
            with _state:
                if not _again:
                    _running = False
                    return
                _again = False
    except BaseException:
        with _state:
            _running = False
        raise


def listener(trigger: str, status: str, error: str = "", summary: dict | None = None) -> None:
    """Called when a run ends (see RunManager.listeners). Never blocks the app and never raises."""
    global _running, _again
    if status not in ("ok", "partial"):                        # a failed or offline run changed nothing worth publishing
        return
    try:
        cmd = command(config.load())
    except (config.ConfigError, ValueError):
        return
    if not cmd:
        return
    with _state:
        if _running:
            _again = True                                      # one more pass after the current one, not a pile of them
            return
        _running, _again = True, False
    threading.Thread(target=_work, args=(cmd,), daemon=True, name="after-run").start()
