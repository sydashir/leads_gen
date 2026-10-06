/* Hybrid Leads front end: plain JavaScript, no build step. */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const csrf = ($("meta[name=csrf-token]") || {}).content || "";

  async function api(path, body) {
    const opts = { method: body === undefined ? "GET" : "POST", credentials: "same-origin", headers: { "X-CSRF-Token": csrf } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, opts); } catch (e) { return { ok: false, status: 0, data: { error: "Could not reach the app. Is it still running?" } }; }
    let data = {};
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (res.status === 401) { location.href = "/login"; }
    return { ok: res.ok, status: res.status, data };
  }
  const say = (el, text, kind) => { el.textContent = text || ""; el.className = "msg" + (kind ? " " + kind : ""); };
  const dur = (s) => (s >= 60 ? Math.floor(s / 60) + "m " + (s % 60) + "s" : s + "s");
  /* numbers read the same on every machine: 1,158, whatever the browser's language */
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const plural = (n, one, many) => fmt(n) + " " + (n === 1 ? one : many);
  /* a busy button keeps keyboard focus (unlike disabled) and simply ignores clicks */
  const setBusy = (btn, on) => { btn.setAttribute("aria-disabled", on ? "true" : "false"); };
  const isBusy = (btn) => btn.getAttribute("aria-disabled") === "true";

  /* ------------------------------------------------ sign-in forms */
  $$(".auth-form form").forEach((f) => {
    f.addEventListener("submit", () => {
      const b = $("button[type=submit]", f);
      if (b) { b.dataset.label = b.textContent; b.disabled = true; b.textContent = "One moment…"; }
    });
  });
  window.addEventListener("pageshow", (e) => {       // back button: the page comes back as it was left, so undo the "One moment"
    if (e.persisted) $$(".auth-form button[type=submit]").forEach((b) => { b.disabled = false; if (b.dataset.label) b.textContent = b.dataset.label; });
  });

  /* ------------------------------------------------ dashboard */
  const runBtn = $("#run");
  if (runBtn) {
    const panel = $("#runpanel"), label = $("#run-label"), meta = $("#run-meta"), bar = $("#run-bar"), log = $("#run-log");
    const note = $("#run-note");
    let watching = false, timer = null, wasRunning = false;

    const announce = (text) => { $("#run-announce").textContent = text || ""; };   // spoken once, not on every poll
    const notice = (text, kind) => {
      $("span", note).textContent = text || "";
      note.hidden = !text;
      note.className = "notice" + (kind ? " " + kind : "");
    };
    const setRunning = (on) => {
      setBusy(runBtn, on);
      $("span", runBtn).textContent = on ? "Running…" : "Run now";
      $$("[data-run]").forEach((b) => setBusy(b, on));
    };
    const summaryText = (s) => {
      const bits = [fmt(s.added || 0) + " added", fmt(s.pushed || 0) + " sent to the sheet", fmt(s.held || 0) + " held back"];
      if (s.seconds) bits.push(dur(s.seconds));
      return bits.join(" · ");
    };

    async function refreshStats() {
      const r = await fetch("/fragment/stats", { credentials: "same-origin" });
      if (r.ok) {
        $("#stats").innerHTML = await r.text();
        $$("[data-run]").forEach((b) => setBusy(b, false));
      }
    }

    function finished(run) {
      const last = run.last || {}, s = last.summary || {}, errs = (s.errors || []).join(" · ");
      let kind = "", head, text;
      if (!run.last) { kind = "warn"; head = "The run was interrupted"; text = "The app was restarted. Whatever was not finished is picked up on the next run."; }
      else if (last.error) { kind = "bad"; head = "The run stopped before it finished"; text = "Run it again. If it stops again, send this to your admin: " + last.error; }
      else if (last.status === "offline") {
        kind = "bad"; head = "No internet connection";
        text = last.trigger === "schedule" ? "Nothing was changed. It will try again in about ten minutes." : "Nothing was changed. Check the connection, then run it again.";
      }
      else if (last.status === "failed") { kind = "bad"; head = "The run failed"; text = errs || "Nothing was added."; }
      else if (last.status === "partial") { kind = "warn"; head = "Finished with a problem"; text = summaryText(s) + (errs ? " · " + errs : ""); }
      else { head = "Finished"; text = summaryText(s); }
      panel.hidden = false;
      panel.className = "runpanel done" + (kind ? " " + kind : "");
      label.textContent = head;
      meta.textContent = text;
      announce(head + ". " + text);
    }

    function show(run, next, updated, problem) {
      const sub = $("#sub");
      if (sub && next) {
        sub.textContent = run.running ? "Running now"
          : (updated ? "Updated " + updated + " · " : "") + (problem ? problem + " · " : "") + "Next run " + next;
      }
      if (run.running) {
        if (!wasRunning) announce("Run started");
        watching = true; wasRunning = true;
        panel.hidden = false; panel.className = "runpanel";
        label.textContent = run.label || "Working";
        const bits = [];
        if (run.total) bits.push(fmt(run.done) + " of " + fmt(run.total));
        if (run.note) bits.push(run.note);
        bits.push(dur(run.elapsed || 0));
        meta.textContent = bits.join(" · ");
        const i = $("i", bar);
        if (run.total && run.stage !== "start") { bar.classList.remove("indet"); i.style.width = Math.max(2, Math.round((run.done / run.total) * 100)) + "%"; }
        else { bar.classList.add("indet"); i.style.width = ""; }
        log.textContent = (run.lines || []).join("\n");
        setRunning(true);
      } else {
        setRunning(false);
        if (wasRunning) {                                  // a run we were watching just ended
          wasRunning = false;
          finished(run);
          log.textContent = (run.lines || []).join("\n");
          refreshStats();
        } else if (!watching) { panel.hidden = true; }
      }
    }

    async function poll() {
      clearTimeout(timer);
      const { ok, data } = await api("/api/status");
      let delay = document.hidden ? 60000 : 15000;
      if (ok) { show(data.run, data.next_run, data.updated, data.problem); if (data.run.running) delay = 1500; }
      timer = setTimeout(poll, delay);
    }

    async function startRun() {
      if (isBusy(runBtn)) return;
      notice("");
      setRunning(true);
      const { ok, data, status } = await api("/api/run", {});
      if (!ok && status !== 409) {                         // 409: a run is already going; just follow it
        setRunning(false);
        notice(data.message || data.error || "Could not start the run.", status === 429 ? "" : "bad");
        return;
      }
      watching = true; wasRunning = true;                  // even a run that fails at once gets its message shown
      poll();
    }
    runBtn.addEventListener("click", startRun);
    document.addEventListener("click", (e) => {
      const b = e.target.closest("[data-run]");
      if (b) startRun();
      const dl = $("#dl");
      if (dl && dl.open && !e.target.closest("#dl")) dl.open = false;
      if (dl && e.target.closest("#dl .pop a")) setTimeout(() => { dl.open = false; $("summary", dl).focus(); }, 50);
    });
    /* Tabbing out of the open Download menu closes it, so it never sits over the tiles. Only a Tab key counts: a mouse click
       on the menu can move focus to the page behind it (Safari does not focus links on click), and must not close it. */
    let tabbing = false;
    document.addEventListener("keydown", (e) => {
      tabbing = e.key === "Tab";
      const dl = $("#dl");
      if (e.key === "Escape" && dl && dl.open) { dl.open = false; $("summary", dl).focus(); }
    }, true);
    document.addEventListener("pointerdown", () => { tabbing = false; }, true);
    const dlMenu = $("#dl");
    if (dlMenu) {
      dlMenu.addEventListener("focusout", (e) => { if (tabbing && e.relatedTarget && !dlMenu.contains(e.relatedTarget)) dlMenu.open = false; });
    }
    document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
    poll();

    /* ------------------------------------------ sheet preview */
    const dlg = $("#preview"), body = $("#pv-body"), openLink = $("#pv-open"), live = $("#pv-live");
    let loadTimer = null;
    const pvSay = (text) => { live.textContent = text || ""; };      // the dialog's own live region: it exists before the text arrives
    const closePreview = () => { clearTimeout(loadTimer); body.replaceChildren(); pvSay(""); if (dlg.open) dlg.close(); };

    const HEADS = ["Company", "Website", "Email", "Phone", "Address", "Registered"];
    const KEYS = ["company", "website", "email", "phone", "address", "registered"];
    const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];   // the same words as the dashboard, in every browser language
    function table(rows) {
      const t = document.createElement("table");
      const thead = t.createTHead().insertRow();
      HEADS.forEach((h) => { const th = document.createElement("th"); th.scope = "col"; th.textContent = h; thead.appendChild(th); });
      const tb = t.createTBody();
      rows.forEach((r) => {
        const tr = tb.insertRow();
        KEYS.forEach((k) => {
          let v = r[k] || "";
          if (k === "website") v = v.replace(/^https?:\/\/(www\.)?/, "").replace(/\/$/, "");
          if (k === "registered") {
            const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
            if (m && MONTHS[+m[2] - 1]) v = (+m[3]) + " " + MONTHS[+m[2] - 1] + " " + m[1];
          }
          const td = tr.insertCell();
          td.textContent = v;
          if (v) td.title = v;                                // the full value on hover, even when the column is narrow
        });
      });
      return t;
    }

    function plainTable(data) {
      const box = document.createElement("div");
      box.className = "pv-table";
      /* the rows scroll, so they must be reachable by keyboard (Safari does not focus a scroller by itself) and have a name */
      box.tabIndex = 0;
      box.setAttribute("role", "region");
      box.setAttribute("aria-label", "Sheet rows");
      const n = data.rows.length;
      const note = document.createElement("div");
      note.className = "note";
      const why = !data.connected ? "The Google Sheet is not connected yet, so this is the same list in a plain table"
                                  : "Link sharing is off, so the Google Sheet cannot be shown here. This is the same list in a plain table";
      const count = n < data.total ? "the newest " + fmt(n) + " of " + fmt(data.total) + " companies. Download the file for all of them" : plural(data.total, "company", "companies");
      note.textContent = why + " (" + count + ").";
      const inner = document.createElement("div");           // as wide as the table, so the note can stay in view while the columns scroll sideways
      inner.className = "pv-in";
      inner.appendChild(note);
      if (n) inner.appendChild(table(data.rows));
      else { const none = document.createElement("div"); none.className = "pv-wait"; none.textContent = "Nothing is listed yet. Run it once."; inner.appendChild(none); }
      box.appendChild(inner);
      pvSay(n ? "Preview loaded. " + note.textContent : "Preview loaded. Nothing is listed yet.");
      return box;
    }

    $("#open-preview").addEventListener("click", async () => {
      body.replaceChildren();
      const wait = document.createElement("div");
      wait.className = "pv-wait"; wait.textContent = "Loading…";
      body.appendChild(wait);
      pvSay("Loading the preview");
      if (!dlg.open) dlg.showModal();
      const { ok, data } = await api("/api/preview");
      if (!ok) { wait.textContent = data.error || "Could not load the preview."; pvSay(wait.textContent); return; }
      openLink.hidden = !data.sheet_url;
      if (data.sheet_url) openLink.href = data.sheet_url;
      $("#pv-sub").textContent = data.connected && data.linked
        ? "Read-only view of the Google Sheet. It updates after every run."
        : data.connected ? "The sheet is private, so this is the same list in a table."
        : "Google is not connected yet. This is the list the sheet will receive.";
      if (data.connected && data.linked) {
        const f = document.createElement("iframe");
        f.title = "Google Sheet preview";
        f.referrerPolicy = "no-referrer";
        f.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox");
        f.addEventListener("load", () => { clearTimeout(loadTimer); f.classList.add("ready"); wait.remove(); pvSay("The Google Sheet preview has loaded"); });
        f.src = data.embed_url;
        body.appendChild(f);
        wait.textContent = "Loading the sheet from Google…";
        pvSay(wait.textContent);
        loadTimer = setTimeout(() => {
          wait.textContent = "Taking longer than usual. If nothing appears, open it in Google Sheets with the button above.";
          pvSay(wait.textContent);
        }, 9000);
      } else {
        body.replaceChildren(plainTable(data));
      }
    });
    $("#pv-close").addEventListener("click", closePreview);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) closePreview(); });
    dlg.addEventListener("close", () => { clearTimeout(loadTimer); body.replaceChildren(); pvSay(""); });
  }

  /* ------------------------------------------------ sign-in: put the shared login into the form */
  const useLogin = $("#use-login");
  if (useLogin) {
    useLogin.addEventListener("click", () => {
      $("#email").value = $("#hint-email").textContent.trim();
      $("#password").value = $("#hint-password").textContent.trim();
      $("#password").focus();
    });
  }

  /* ------------------------------------------------ settings */
  const gConnect = $("#g-connect");
  if (gConnect) {
    $("#copy-script").addEventListener("click", async (e) => {
      const ta = $("#script");
      try { await navigator.clipboard.writeText(ta.value); e.target.textContent = "Copied"; }
      catch (err) { ta.select(); document.execCommand("copy"); e.target.textContent = "Copied"; }
      setTimeout(() => { e.target.textContent = "Copy script"; }, 2000);
    });
    gConnect.addEventListener("click", async () => {
      const msg = $("#g-msg"), url = $("#g-url").value.trim();
      if (!url) { say(msg, "Paste the Web app URL first.", "bad"); return; }
      gConnect.disabled = true; say(msg, "Connecting and building the sheet. This can take half a minute…");
      msg.scrollIntoView({ block: "nearest", behavior: "smooth" });
      const { ok, data } = await api("/api/settings/google", { url, link: $("#g-link").checked });
      gConnect.disabled = false;
      if (!ok) { say(msg, data.error || "Could not connect.", "bad"); msg.scrollIntoView({ block: "nearest", behavior: "smooth" }); return; }
      const base = data.queued
        ? "Connected. The " + plural(data.waiting, "company", "companies") + " that were waiting will be sent as soon as the current run ends."
        : data.sending
          ? "Connected. Sending " + plural(data.waiting, "company", "companies") + " that were waiting…"
          : "Connected. The sheet is ready.";
      say(msg, data.warning ? base + " " + data.warning : base, "good");
      msg.scrollIntoView({ block: "nearest", behavior: "smooth" });
      setTimeout(() => location.reload(), data.warning ? 6000 : data.sending ? 1800 : 900);
    });
    $("#g-link").addEventListener("change", async (e) => {
      const msg = $("#g-link-msg"), box = e.target;
      say(msg, "Saving…");
      const { ok, data } = await api("/api/settings/sharing", { link: box.checked });
      if (!ok) { box.checked = !box.checked; say(msg, data.error || "Could not change that.", "bad"); return; }
      say(msg, box.checked ? "Anyone with the link can view the sheet." : "The sheet is private.", "good");
    });
    $("#s-save").addEventListener("click", async () => {
      const msg = $("#s-msg");
      const { ok, data } = await api("/api/settings/schedule", { time: $("#s-time").value });
      if (!ok) { say(msg, data.error || "Could not save.", "bad"); return; }
      $("#s-next").textContent = "Next run " + data.next_run;
      say(msg, "Saved.", "good");
    });
    $("#r-save").addEventListener("click", async () => {
      const msg = $("#r-msg");
      const require = ["website"];
      $$("[data-req]").forEach((c) => { if (c.checked) require.push(c.dataset.req); });
      const sources = {};
      $$("[data-src]").forEach((c) => { sources[c.dataset.src] = c.checked; });
      const { ok, data } = await api("/api/settings/rules", { require, require_it_signal: $("#r-it").checked, skip_established: $("#r-old").checked, contact_either: $("#r-either").checked, sources });
      if (!ok) { say(msg, data.error || "Could not save.", "bad"); return; }
      const one = data.released === 1;
      const more = data.released
        ? " " + plural(data.released, "company that was", "companies that were") + " held back or dropped now " + (one ? "meets" : "meet") + " the rules and " + (one ? "is" : "are") + " listed."
          + (data.sending ? " " + (one ? "It is" : "They are") + " being sent to the sheet." : "")
        : "";
      say(msg, "Saved. Every run uses these rules." + more, "good");
    });
  }
})();
