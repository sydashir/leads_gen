"""Keep the web app running on a Mac: a LaunchAgent that starts it at login and restarts it if it crashes."""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

LABEL = "com.itleads.web"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
CARRIED_ENV = ("ITLEADS_HOST", "ITLEADS_PORT", "ITLEADS_TRUSTED_PROXY", "ITLEADS_SECURE_COOKIES")


def available() -> bool:
    return shutil.which("launchctl") is not None


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def python_path() -> str:
    venv = config.ROOT / ".venv" / "bin" / "python"
    return str(venv if venv.exists() else sys.executable)


def build_plist() -> dict:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin", "ITLEADS_HOME": str(config.ROOT)}
    env.update({k: os.environ[k] for k in CARRIED_ENV if os.environ.get(k)})      # settings given when installing stay
    return {
        "Label": LABEL,
        "ProgramArguments": [python_path(), "-m", "itleads", "serve"],
        "WorkingDirectory": str(config.ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},          # restart after a crash; a deliberate clean exit stays stopped
        "ThrottleInterval": 10,
        "StandardOutPath": str(config.LOGS / "web.out.log"),
        "StandardErrorPath": str(config.LOGS / "web.err.log"),
        "EnvironmentVariables": env,
        # no ProcessType: "Background" makes macOS throttle CPU and disk, so the app started 3-10x slower and the
        # daily lookup crawled (measured); the default priority is what a normal program gets.
    }


def installed() -> bool:
    return available() and _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0


def install() -> None:
    if not available():
        raise RuntimeError("launchctl is not available: the background service is a macOS feature")
    config.ensure_dirs()
    data = plistlib.dumps(build_plist())                  # built first: nothing is touched if that fails
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    old = PLIST.read_bytes() if PLIST.exists() else None
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    PLIST.write_bytes(data)
    _launchctl("enable", f"{_domain()}/{LABEL}")          # a service switched off earlier would refuse to load
    r = _launchctl("bootstrap", _domain(), str(PLIST))
    if r.returncode == 0:
        return
    back = ""
    if old is not None:                                   # put back what worked before, and say whether it came back
        PLIST.write_bytes(old)
        again = _launchctl("bootstrap", _domain(), str(PLIST))
        back = " The previous service was loaded again." if again.returncode == 0 else " The previous service could not be loaded again either."
    else:
        PLIST.unlink(missing_ok=True)
    why = (r.stderr.strip() or "launchctl could not load the service")
    if "Input/output error" in why or "5:" in why:
        why += " (macOS may be blocking it: allow it under System Settings > General > Login Items & Extensions)"
    raise RuntimeError(why + "." + back)


def uninstall() -> None:
    if not available():
        return
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    PLIST.unlink(missing_ok=True)
