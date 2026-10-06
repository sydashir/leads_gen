"""Running the pipeline from the web app, and the daily schedule that calls it."""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime

from .. import config, pipeline
from ..store import Store
from . import appdb

STAGES = {
    "start": "Starting",
    "fetch": "Checking the public registries",
    "lookup": "Looking up websites and contacts",
    "send": "Sending to the sheet",
    "finish": "Finishing",
}


def _last_run():
    store = Store(config.DATA / "leads.db")
    try:
        return store.last_run()
    finally:
        store.close()


def scheduler_ready(cfg: dict | None = None) -> bool:
    """Daily runs start once someone owns the app and it has either been run once or has Google connected.
    A brand-new install must not launch a half-hour job nobody asked for."""
    cfg = cfg or config.load()
    if appdb.count_users() == 0:
        return False
    return bool(cfg.get("apps_script_url")) or _last_run() is not None


def next_run_text(cfg: dict, now: datetime | None = None, assume_done: bool = False) -> str:
    """When the next daily run happens. assume_done: the run that is about to be recorded counts for today."""
    if not scheduler_ready(cfg):
        return "after you run it for the first time"
    now = now or datetime.now()
    h, m = cfg["schedule"]["hour"], cfg["schedule"]["minute"]
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    done_today = assume_done or appdb.meta_get("last_scheduled") == now.date().isoformat()
    if not done_today and now < due:
        return f"today {h:02d}:{m:02d}"
    if not done_today:
        return "now (catching up)"
    return f"tomorrow {h:02d}:{m:02d}"


def counts_for_today(cfg: dict, trigger: str, now: datetime | None = None) -> bool:
    """Does a run started now with this trigger use up today's daily run? (the schedule, or a run by hand after the time)"""
    now = now or datetime.now()
    due = now.replace(hour=cfg["schedule"]["hour"], minute=cfg["schedule"]["minute"], second=0, microsecond=0)
    return trigger == "schedule" or (trigger == "manual" and now >= due)


class RunManager:
    """One run at a time. Anyone signed in may start one; the daily schedule uses the same door."""

    def __init__(self, runner=None, sender=None):
        self._runner = runner or (lambda cfg, **kw: pipeline.run(cfg, quiet=True, **kw))
        self._sender = sender or (lambda cfg, **kw: pipeline.send_waiting(cfg, **kw))
        self._lock = threading.Lock()
        self._lines: deque = deque(maxlen=14)
        self._t0 = 0.0
        self.listeners: list = []                       # called as fn(trigger, status, error, summary) when a run ends
        self._send_after = False                        # Google was connected during a run: send once it ends
        self.s = {"running": False, "stage": "", "label": "", "done": 0, "total": 0, "note": "", "started": "",
                  "trigger": "", "last": None}
        self._file_log = None

    # -------------------------------------------------------------- control
    def start(self, trigger: str, *, admin: bool = False, task: str = "run") -> tuple:
        """-> (started, reason, message). task 'send' only sends companies that are waiting for Google."""
        cfg = config.load()
        with self._lock:
            if self.s["running"]:
                return False, "busy", "A run is already in progress."
            cd = int(cfg["app"].get("cooldown_minutes", 0) or 0)
            if trigger == "manual" and cd and not admin:
                ago = self._minutes_since_last_run()
                if ago is not None and ago < cd:         # whatever happened last time, success or crash: no hammering
                    wait = max(1, int(round(cd - ago)))
                    return False, "cooldown", (f"It ran {int(ago)} minute{'s' if int(ago) != 1 else ''} ago. "
                                               f"You can run it again in {wait} minute{'s' if wait != 1 else ''}.")
            self._lines.clear()
            self._t0 = time.time()
            self.s.update(running=True, stage="start", label=STAGES["start"], done=0, total=0, note="",
                          started=datetime.now().isoformat(timespec="seconds"), trigger=trigger)
            threading.Thread(target=self._work, args=(trigger, task), daemon=True, name="run").start()
        return True, "started", "Started."

    def queue_send(self) -> str:
        """Send what is waiting for Google: now, or right after the run in progress. -> 'started' | 'queued'"""
        with self._lock:
            if self.s["running"]:
                self._send_after = True
                return "queued"
        if self.start("connect", admin=True, task="send")[0]:
            return "started"
        with self._lock:                                 # a run began a moment ago
            self._send_after = True
        return "queued"

    def _minutes_since_last_run(self):
        """Minutes since any run ended: the one in the database, or one that crashed before it could record itself."""
        ends = []
        last = _last_run()
        for iso in (last["finished"] if last else "", (self.s["last"] or {}).get("finished", "")):
            try:
                ends.append(datetime.fromisoformat(iso))
            except (TypeError, ValueError):
                pass
        return (datetime.now() - max(ends)).total_seconds() / 60 if ends else None

    def snapshot(self) -> dict:
        with self._lock:
            s = dict(self.s)
            s["lines"] = list(self._lines)
            s["elapsed"] = int(time.time() - self._t0) if s["running"] else 0
            return s

    # --------------------------------------------------------------- worker
    def _log(self, msg: str) -> None:
        msg = (msg or "").rstrip()
        if not msg:
            return
        with self._lock:
            self._lines.append(msg.strip())
        if self._file_log:
            self._file_log(msg)

    def _progress(self, stage: str, done: int, total: int, note: str = "") -> None:
        with self._lock:
            self.s.update(stage=stage, label=STAGES.get(stage, stage), done=done, total=total, note=note)

    def _work(self, trigger: str, task: str) -> None:
        summary, err = None, ""
        try:                                            # everything inside: whatever fails, "running" must be cleared
            cfg = config.load()
            self._file_log = pipeline.Logger(quiet=True)
            with pipeline.run_lock():
                fn = self._sender if task == "send" else self._runner
                summary = fn(cfg, log=self._log, progress=self._progress, trigger=trigger,
                             next_run=next_run_text(cfg, assume_done=counts_for_today(cfg, trigger)))
        except pipeline.Busy:
            err = "A run started from the command line is still going. Try again in a few minutes."
        except BaseException as e:                      # a failed run must never take the server down
            err = f"{type(e).__name__}: {e}"
            self._log(f"The run stopped before it finished: {err}")
        finally:
            status = (summary or {}).get("status", "failed")
            with self._lock:
                self.s["last"] = {"finished": datetime.now().isoformat(timespec="seconds"), "status": status,
                                  "error": err, "summary": summary, "trigger": trigger}
            for fn in list(self.listeners):             # while still "running", so nothing else starts in between
                try:
                    fn(trigger, status, err, summary)
                except Exception:
                    pass
            with self._lock:
                self.s.update(running=False, stage="", label="", done=0, total=0, note="")
                again, self._send_after = self._send_after, False
            if again:
                self.start("connect", admin=True, task="send")


class Scheduler(threading.Thread):
    """Wakes every 20 seconds. Once the daily time has passed and today's update has not happened, it starts one.
    'Happened' means a run FINISHED: if the server stops in the middle, it picks up again when it is back."""

    def __init__(self, manager: RunManager):
        super().__init__(daemon=True, name="scheduler")
        self.manager = manager
        self._halt = threading.Event()
        self._retry_at = 0.0
        self._resend_at = 0.0               # when to try sending companies that are waiting for Google again
        self._pending_day = ""
        manager.listeners.append(self._finished)

    def run(self) -> None:
        while not self._halt.wait(20):
            try:
                self.tick()
            except Exception:                           # an unreadable config must not end the schedule for good
                pass

    def stop(self) -> None:
        self._halt.set()

    def _finished(self, trigger: str, status: str, error: str, summary: dict | None = None) -> None:
        ok = status in ("ok", "partial") and not error
        if ok and trigger == "schedule":
            appdb.meta_set("last_scheduled", self._pending_day or datetime.now().date().isoformat())
        elif ok and trigger == "manual" and counts_for_today(config.load(), "manual"):   # a run by hand after the daily time counts too
            appdb.meta_set("last_scheduled", datetime.now().date().isoformat())
        elif not ok and trigger == "schedule":
            self._retry_at = time.time() + 600           # offline or failed: try again in ten minutes
        if any(str(x).startswith("sheet:") for x in ((summary or {}).get("errors") or [])):
            self._resend_at = time.time() + 600          # Google could not be reached: whatever is waiting goes out later

    def _waiting_for_google(self, cfg: dict) -> bool:
        if not cfg.get("apps_script_url"):
            return False
        store = Store(config.DATA / "leads.db")
        try:
            return store.counts()["ready"] > 0
        finally:
            store.close()

    def _maybe_resend(self, cfg: dict) -> str:
        """Companies that qualified but never reached the sheet (Google was down, or the app was stopped before the
        send) are sent as soon as nothing else is going on; after a failure, no more often than every ten minutes."""
        if time.time() < self._resend_at or not self._waiting_for_google(cfg):
            return ""
        started, _, _ = self.manager.start("connect", admin=True, task="send")
        if started:
            self._resend_at = time.time() + 600
            return "sending"
        return ""

    @staticmethod
    def _due(now: datetime) -> datetime:
        cfg = config.load()
        return now.replace(hour=cfg["schedule"]["hour"], minute=cfg["schedule"]["minute"], second=0, microsecond=0)

    def _ran_since(self, due: datetime) -> bool:
        last = _last_run()
        if not last or last["summary"].get("status") not in ("ok", "partial"):
            return False
        try:
            return datetime.fromisoformat(last["finished"]) >= due
        except ValueError:
            return False

    def tick(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        cfg = config.load()
        if not scheduler_ready(cfg):
            return "not ready"
        today = now.date().isoformat()
        if appdb.meta_get("last_scheduled") == today:
            return self._maybe_resend(cfg) or "done today"
        due = self._due(now)
        if now < due or time.time() < self._retry_at:
            return self._maybe_resend(cfg) or "waiting"
        if self._ran_since(due):                         # someone already ran it since the daily time
            appdb.meta_set("last_scheduled", today)
            return self._maybe_resend(cfg) or "done today"
        self._pending_day = today
        started, reason, _ = self.manager.start("schedule")
        return "started" if started else reason          # busy: try again on the next tick
