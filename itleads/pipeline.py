"""One run: fetch new filings, find websites and contacts, push the complete ones to the sheet."""
from __future__ import annotations

import concurrent.futures as cf
import fcntl
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from . import config, enrich, sheet, sources, util
from .store import Store, row_hash

MAX_RUN_SECONDS = 3 * 3600          # a stuck run must not block tomorrow's
TRANSIENT_LIMIT = 3                 # lookups in a row that end in network trouble before a company is judged as it stands


class Busy(Exception):
    pass


@contextmanager
def run_lock():
    config.ensure_dirs()
    f = open(config.DATA / "run.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise Busy("another run is still going")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


class Logger:
    def __init__(self, quiet: bool = False):
        config.ensure_dirs()
        self.path = config.LOGS / f"run-{date.today().isoformat()}.log"
        self.quiet = quiet

    def __call__(self, msg: str = "") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        with open(self.path, "a") as f:
            f.write(f"{stamp}  {msg}\n")
        if not self.quiet:
            print(msg, flush=True)


def source_window(cfg: dict, store: Store, src, today: date, override=None) -> date:
    """Each source remembers its own last good fetch. A failed source, or a Mac that slept for a week, simply
    gets a longer window next time. `lag` covers feeds that publish in weekly or monthly batches."""
    if override:
        return override
    floor = today - timedelta(days=cfg["backfill_days"])
    last = store.source_last_ok(src.key)
    if last is None:
        return floor
    return max(floor, min(last - timedelta(days=src.lag), today - timedelta(days=src.lag)))


def online(wait: int = 0) -> bool:
    """True when the internet answers; with `wait`, keep trying that many seconds (a Mac that just woke up)."""
    end = time.monotonic() + wait
    while True:
        if util.reachable(fresh=True):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(15)


def fetch_all(srcs: list, windows: dict, today: date, log, progress=None) -> dict:
    out: dict = {}
    done = [0]

    def one(s):
        t = time.time()
        try:
            recs = s.fetch(windows[s.key], today)
            return s.key, recs, None, time.time() - t
        except Exception as e:
            return s.key, None, f"{type(e).__name__}: {str(e)[:160]}", time.time() - t

    with cf.ThreadPoolExecutor(max(len(srcs), 1)) as ex:
        for key, recs, err, secs in ex.map(one, srcs):
            if err:
                log(f"  {key:8} FAILED  {err}")
            else:
                log(f"  {key:8} {len(recs):5} filings since {windows[key]}  ({secs:.0f}s)")
            out[key] = (recs, err)
            done[0] += 1
            if progress:
                progress("fetch", done[0], len(srcs), key)
    return out


def enrich_pending(store: Store, cfg: dict, today: date, log, limit=None, deadline: float = None, progress=None) -> dict:
    todo = store.due(today)
    if limit:
        todo = todo[:limit]
    stats = {"checked": 0, "ready": 0, "held": 0, "dropped": 0, "errors": 0, "crashed": 0}
    if not todo:
        return stats
    log(f"Looking up websites and contacts for {len(todo)} companies...")

    def work(item):
        try:
            return item, enrich.enrich_one(item["raw"]), "", ""
        except util.Transient as e:
            return item, None, "transient", str(e)
        except Exception as e:
            return item, None, "crash", f"{type(e).__name__}: {e}"

    t0 = time.time()
    ex = cf.ThreadPoolExecutor(cfg["workers"])
    futs = [ex.submit(work, it) for it in todo]
    try:
        remaining = None if deadline is None else max(deadline - time.monotonic(), 1)
        for n, fut in enumerate(cf.as_completed(futs, timeout=remaining), 1):
            item, e, kind, msg = fut.result()
            state = None
            try:
                if kind == "transient" and (not util.reachable(max_age=3) or store.add_strike(item["id"]) < TRANSIENT_LIMIT):
                    stats["errors"] += 1      # no attempt is used up: it is retried next run (a few times, then judged;
                                              # while the internet is down it is never judged)
                elif kind:
                    if kind == "crash":       # a page or name the parsers could not handle: a miss, never an endless retry
                        stats["crashed"] += 1
                        log(f"  lookup of {item['id']} failed: {msg}")
                    e = enrich.empty_result()
                    e["error"] = util.clean_text(msg, 300)
                    state, missing = "held", ["lookup error" if kind == "crash" else "website"]
                else:
                    state, missing = enrich.qualify(item["raw"], e, cfg)
                    if state == "ready" and e.get("domain") and store.domain_owner(e["domain"], item["id"]):
                        state, missing = "dropped", ["same website as another company"]
                if state:
                    if state == "dropped":
                        e["dropped_because"] = missing
                    e["missing"] = missing
                    store.record_attempt(item["id"], e, state, today)
                    stats["checked"] += 1
                    stats["held" if state in ("held", "new") else state] += 1
            except Exception as err:          # storing or judging one company failed: count it as a miss, carry on
                stats["crashed"] += 1
                log(f"  could not record {item['id']}: {type(err).__name__}: {err}")
                try:
                    bad = enrich.empty_result()
                    bad.update(error=util.clean_text(str(err), 300), missing=["lookup error"])
                    store.record_attempt(item["id"], bad, "held", today)
                except Exception:
                    pass
            if progress:
                progress("lookup", n, len(todo), f"{stats['ready']} added so far")
            if n % 25 == 0 or n == len(todo):
                rate = n / max(time.time() - t0, 1)
                log(f"  {n}/{len(todo)} done  ({rate:.1f}/s)  added so far: {stats['ready']}")
    except cf.TimeoutError:
        log("  Time limit reached; the rest will be picked up next run.")
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    if stats["errors"]:
        log(f"  {stats['errors']} lookups hit a network problem and will be retried next run.")
    if stats["crashed"]:
        log(f"  {stats['crashed']} lookups failed on an unexpected page and were counted as misses.")
    return stats


def requalify_pass(store: Store, cfg: dict, today: date, log=None) -> dict:
    """The rules changed (for example Phone is no longer required) or the checks got stricter (a placeholder phone, a function
    mailbox, a contact who is not a person): every company that is checked, listed or sent is judged again on what the lookup
    already found, with no new trip to the network.
      - held or dropped, now qualifying: listed (released);
      - waiting to be sent: cleaned of what today's checks refuse, or taken back to held (looked at tomorrow) or dropped;
      - already sent: cleaned and kept (it stays sent, nothing is sent again), or, when it no longer qualifies, back to held
        with today as its next look, so the next daily run looks it up again with today's code; the change is logged with the
        company's name.
    A second pass over the same rows changes nothing. One company that cannot be read never stops the rest.
    Returns the counts: released, cleaned (kept, with its stored values tidied), demoted (waiting ones taken back), unlisted
    (sent ones taken back), errors."""
    log = log or (lambda *_a, **_k: None)
    st = {"released": 0, "cleaned": 0, "demoted": 0, "unlisted": 0, "errors": 0}

    def failed(it, err):
        st["errors"] += 1
        log(f"  could not judge {it.get('id')} again: {type(err).__name__}: {err}")

    for it in store.checked_not_listed():
        try:
            e = it["enrich"]
            state, _ = enrich.qualify(it["raw"], e, cfg)
            if state != "ready" or (e.get("domain") and store.domain_owner(e["domain"], it["id"])):
                continue
            e["missing"] = []
            e.pop("dropped_because", None)
            store.mark_ready(it["id"], e, today)
        except sqlite3.Error:
            raise
        except Exception as err:
            failed(it, err)
            continue
        st["released"] += 1
    for it in store.ready() + store.pushed():          # tighter rules also apply to companies not yet sent, and to those sent
        sent = it["state"] == "pushed"
        try:
            e = it["enrich"]
            cleaned = enrich.clean_stored(e, it["raw"])         # what today's checks refuse is taken out first
            state, missing = enrich.qualify(it["raw"], cleaned, cfg)
            if state != "ready":
                state = "held" if sent else state              # a sent company is looked up again, not dropped
                cleaned["missing"] = missing
                if state == "dropped":
                    cleaned["dropped_because"] = missing
                if not store.demote(it["id"], state, cleaned, today):
                    continue
                st["unlisted" if sent else "demoted"] += 1
                log(f"  {it['raw'].get('name') or it['id']}: no longer meets the rules ({', '.join(map(str, missing))}); "
                    + ("was sent to the sheet, back to held and looked up again on the next run" if sent else
                       "back to " + state))
            elif cleaned != e:
                store.save_enrich(it["id"], cleaned)
                st["cleaned"] += 1
        except sqlite3.Error:
            raise
        except Exception as err:
            failed(it, err)
    return st


def requalify(store: Store, cfg: dict, today: date, log=None) -> int:
    """requalify_pass, returning only how many companies held back earlier now qualify."""
    return requalify_pass(store, cfg, today, log)["released"]


def push(store: Store, bridge, today: date, log, progress=None) -> dict:
    items = store.ready()
    out = {"pushed": 0, "updated": 0, "skipped": 0}
    if not items:
        return out
    log(f"Sending {len(items)} companies to the sheet...")
    try:
        for i in range(0, len(items), 80):
            batch = items[i:i + 80]
            rows = [sheet.sheet_row(it) for it in batch]
            res = bridge.upsert(rows, today.isoformat())
            for it, row in zip(batch, rows):
                store.mark_pushed(it["id"], row_hash(row), today)
            out["pushed"] += res["inserted"]
            out["updated"] += res["updated"]
            out["skipped"] += res.get("skipped", 0)
            if progress:
                progress("send", min(i + 80, len(items)), len(items), "")
    except sheet.BridgeError as e:
        e.partial = dict(out)                 # the batches that did arrive are in the sheet; report them
        raise
    return out


def run(cfg: dict, *, dry_run: bool = False, since: date | None = None, only: list | None = None,
        limit: int | None = None, quiet: bool = False, next_run: str = "", log=None, progress=None,
        trigger: str = "manual") -> dict:
    store = Store(config.DATA / "leads.db")
    try:
        return _run(cfg, store, dry_run=dry_run, since=since, only=only, limit=limit, quiet=quiet, next_run=next_run,
                    log=log, progress=progress, trigger=trigger)
    finally:
        store.close()


def send_waiting(cfg: dict, *, log=None, progress=None, trigger: str = "connect", next_run: str = "", **_) -> dict:
    """Send the companies that were added while Google was not connected (or could not be reached)."""
    log = log or Logger(True)
    progress = progress or (lambda *a, **k: None)
    started = datetime.now()
    store = Store(config.DATA / "leads.db")
    summary = {"status": "ok", "errors": [], "sources": {}, "trigger": trigger, "added": 0, "pushed": 0}
    try:
        if not cfg.get("apps_script_url"):
            log("Google is not connected, so nothing was sent.")
            return summary
        bridge = sheet.Bridge(cfg["apps_script_url"], cfg["token"])
        try:
            bridge.ping()
            sheet.remember_version(bridge)
            progress("send", 0, 0, "")
            rq = requalify_pass(store, cfg, date.today(), log)  # waiting companies are judged by today's checks before they go
            summary["added"] = rq["released"]                   # those it releases are sent just below
            summary["rechecked"] = rq
            pushed = push(store, bridge, date.today(), log, progress)
            summary["pushed"] = pushed["pushed"]
            held = store.counts()["held"]
            summary["held"] = held
            summary["seconds"] = int((datetime.now() - started).total_seconds())
            if pushed["pushed"] or pushed["updated"]:
                bridge.log({"started": started.strftime("%d %b %Y, %H:%M"), "seconds": summary["seconds"],
                            "added": pushed["pushed"], "held": held, "updated": pushed["updated"], "sources": "",
                            "status": "ok", "errors": ""},
                           held, sum(1 for v in cfg["sources"].values() if v), next_run)
            log(f"Sent {pushed['pushed']} companies to the sheet.")
        except sheet.BridgeError as e:
            summary["status"] = "partial"
            summary["pushed"] = (getattr(e, "partial", None) or {}).get("pushed", summary["pushed"])
            summary["errors"].append(f"sheet: {e}")
            log(f"Could not reach the sheet: {e}")
        if summary["pushed"] or summary["errors"]:         # a send with nothing to send leaves no entry in the history
            store.log_run(started.isoformat(), datetime.now().isoformat(), summary)
        return summary
    finally:
        store.close()


def _run(cfg: dict, store: Store, *, dry_run: bool = False, since: date | None = None, only: list | None = None,
        limit: int | None = None, quiet: bool = False, next_run: str = "", log=None, progress=None,
        trigger: str = "manual") -> dict:
    log = log or Logger(quiet)
    progress = progress or (lambda *a, **k: None)
    started = datetime.now()
    deadline = time.monotonic() + MAX_RUN_SECONDS
    today = date.today()
    enabled = {k: v for k, v in cfg["sources"].items() if v and (not only or k in only)}
    srcs = sources.build(enabled)
    summary = {"status": "ok", "errors": [], "sources": {}, "trigger": trigger}
    bridge = None
    if not dry_run and cfg.get("apps_script_url"):          # without Google the leads simply wait, ready to be sent
        bridge = sheet.Bridge(cfg["apps_script_url"], cfg["token"])
    progress("start", 0, 0, "")

    if not online(wait=0 if not quiet else 120):
        log("No internet connection; nothing was changed. The next run will catch up.")
        summary.update(status="offline", errors=["no internet connection"])
        return summary                         # not written to the history: an offline night would fill it with empty rows

    sheet_ok = True
    if bridge is not None:
        try:
            bridge.ping()
            sheet.remember_version(bridge)
        except sheet.BridgeError as e:
            sheet_ok = False
            summary["errors"].append(f"sheet: {e}")
            summary["status"] = "partial"
            log(f"The sheet is not reachable ({e}). Fetching continues; rows are sent once it is back.")

    windows = {s.key: source_window(cfg, store, s, today, since) for s in srcs}
    log(f"{started:%d %b %Y %H:%M}  registries: {', '.join(s.key for s in srcs)}")
    fetched = fetch_all(srcs, windows, today, log, progress)
    added = 0
    failed = 0
    for key, (recs, err) in fetched.items():
        if err:
            failed += 1
            summary["errors"].append(f"{key}: {err}")
            continue
        new = sum(1 for r in recs if store.add(r, today))
        summary["sources"][key] = new
        added += new
        if not dry_run:
            store.set_source_ok(key, today)           # only now does this source's window move forward
    if failed and failed == len(srcs):
        summary["status"] = "failed"
    elif failed:
        summary["status"] = "partial"
    log(f"New filings stored: {added}" + (f"  ({', '.join(f'{k} {v}' for k, v in summary['sources'].items())})"
                                          if summary["sources"] else ""))

    rq = {} if dry_run else requalify_pass(store, cfg, today, log)
    released = rq.get("released", 0)
    if released:
        log(f"{released} companies held back earlier now meet the rules and were added.")
    if rq.get("unlisted"):
        log(f"{rq['unlisted']} companies already sent no longer meet the rules; they are held back and looked up again today.")
    if rq:
        summary["rechecked"] = rq
    st = enrich_pending(store, cfg, today, log, limit, deadline, progress)
    summary["enrich"] = st
    summary["added"] = st["ready"] + released
    if st["errors"] >= 10 and st["errors"] > 0.05 * (st["checked"] + st["errors"]):   # many lookups ran into trouble
        summary["errors"].append(f"{st['errors']} lookups could not finish (the internet dropped or a Mac went to sleep); "
                                 "they are retried on the next run")
        summary["status"] = "partial" if summary["status"] == "ok" else summary["status"]
    pushed = {"pushed": 0, "updated": 0, "skipped": 0}
    if bridge is not None and sheet_ok:
        try:
            pushed = push(store, bridge, today, log, progress)
        except sheet.BridgeError as e:
            sheet_ok = False
            pushed = getattr(e, "partial", None) or pushed
            summary["errors"].append(f"sheet: {e}")
            summary["status"] = "partial"
            log(f"Could not reach the sheet: {e}")
    if pushed.get("skipped"):
        summary["errors"].append(f"{pushed['skipped']} rows skipped by the sheet script")
    counts = store.counts()
    waiting = store.held_breakdown()
    held = counts["held"]
    secs = int((datetime.now() - started).total_seconds())
    summary.update(pushed=pushed["pushed"], updated=pushed["updated"], held=held, not_looked_up=waiting["not_looked_up"],
                   seconds=secs)
    if bridge is not None and sheet_ok:
        try:
            bridge.log({"started": started.strftime("%d %b %Y, %H:%M"), "seconds": secs, "added": pushed["pushed"],
                        "held": held, "updated": pushed["updated"],
                        "sources": "  ".join(f"{k.upper()} {v}" for k, v in summary["sources"].items()),
                        "status": summary["status"], "errors": "; ".join(summary["errors"])[:300]},
                       held, len(srcs), next_run)
        except sheet.BridgeError as e:
            summary["errors"].append(f"sheet: {e}")
            summary["status"] = "partial"
            log(f"Could not update the dashboard: {e}")
    if not dry_run:
        store.log_run(started.isoformat(), datetime.now().isoformat(), summary)
    log(f"Done in {secs}s: {summary['added']} added, {pushed['pushed']} sent to the sheet, {held} held back, "
        f"status {summary['status']}.")
    progress("finish", 0, 0, "")
    return summary
