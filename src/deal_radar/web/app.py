"""FastAPI app for the local control UI: config editor, matches, live logs, scanner control."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from .. import paths
from ..config.loader import validate_config_text
from ..errors import ConfigError
from ..logging import LogBuffer, attach_log_buffer, get_logger
from ..messaging.store import SqliteDraftStore
from .controller import ScannerController
from .sender import MessageSender

log = get_logger("web.app")


def create_app(
    *,
    config_path: str = "config.yaml",
    controller: ScannerController | None = None,
    log_buffer: LogBuffer | None = None,
    sender: MessageSender | None = None,
) -> FastAPI:
    """Build the web app. Inject ``controller``/``log_buffer``/``sender`` in tests."""
    app = FastAPI(title="deal-radar")
    cfg_path = Path(config_path)
    buffer = log_buffer if log_buffer is not None else attach_log_buffer()

    if controller is None:
        from .runner import build_jobs

        loop_job, once_job = build_jobs(str(cfg_path))
        controller = ScannerController(loop_job, once_job)
    ctl: ScannerController = controller  # non-None for use inside the closures below

    if sender is None:
        from .sender import build_send_fn

        sender = MessageSender(
            build_send_fn(str(cfg_path)), lambda: SqliteDraftStore(paths.db_path())
        )
    snd: MessageSender = sender

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/api/config", response_class=PlainTextResponse)
    def get_config() -> str:
        return cfg_path.read_text(encoding="utf-8") if cfg_path.is_file() else ""

    @app.post("/api/config")
    async def save_config(request: Request) -> JSONResponse:
        text = (await request.body()).decode("utf-8")
        try:
            validate_config_text(text)
        except ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        cfg_path.write_text(text, encoding="utf-8")
        log.info("config saved via web UI (%d bytes)", len(text))
        return JSONResponse({"ok": True})

    @app.get("/api/config/summary")
    def config_summary() -> dict[str, Any]:
        if not cfg_path.is_file():
            return {"error": "no config file"}
        try:
            cfg = validate_config_text(cfg_path.read_text(encoding="utf-8"))
        except (ConfigError, OSError) as exc:
            return {"error": str(exc)}
        return {
            "items": [
                {
                    "name": item.name,
                    "enabled": item.enabled,
                    "price_min": item.price_min,
                    "price_max": item.price_max,
                    "min_rating": item.effective_min_rating(cfg.ai),
                    "phrases": item.search_phrases,
                }
                for item in cfg.items
            ]
        }

    @app.get("/api/seen")
    def seen(limit: int = 50) -> dict[str, Any]:
        from ..dedup.sqlite_store import SqliteSeenStore

        db = paths.db_path()
        if not db.is_file():
            return {"rows": []}
        with SqliteSeenStore(db) as store:
            rows = store.list_seen()
        rows.sort(key=lambda r: str(r.get("first_seen_ts") or ""), reverse=True)
        return {"rows": rows[:limit]}

    @app.get("/api/drafts")
    def drafts(limit: int = 50) -> dict[str, Any]:
        db = paths.db_path()
        if not db.is_file():
            return {"rows": [], "sending": snd.is_busy()}
        with SqliteDraftStore(db) as store:
            rows = store.list_drafts(limit=limit)
        return {"rows": rows, "sending": snd.is_busy()}

    @app.post("/api/drafts/{draft_id}/approve")
    async def approve_draft(draft_id: int, request: Request) -> JSONResponse:
        try:
            body = json.loads((await request.body()) or b"{}")
        except json.JSONDecodeError:
            body = {}
        with SqliteDraftStore(paths.db_path()) as store:
            draft = store.get(draft_id)
            if draft is None:
                return JSONResponse({"ok": False, "error": "unknown draft"}, status_code=404)
            if draft["status"] not in ("pending", "failed"):
                return JSONResponse(
                    {"ok": False, "error": f"draft is {draft['status']}"}, status_code=409
                )
            if snd.is_busy():
                return JSONResponse(
                    {"ok": False, "error": "another send is in progress"}, status_code=409
                )
            text = str(body.get("message") or "").strip() or str(draft["message"])
            store.set_status(draft_id, "sending", message=text)
            draft["message"] = text
        if not snd.send(draft, text):
            with SqliteDraftStore(paths.db_path()) as store:
                store.set_status(draft_id, str(draft["status"]))
            return JSONResponse(
                {"ok": False, "error": "another send is in progress"}, status_code=409
            )
        log.info("draft #%d approved via web UI", draft_id)
        return JSONResponse({"ok": True, "status": snd.status()})

    @app.post("/api/drafts/{draft_id}/dismiss")
    def dismiss_draft(draft_id: int) -> JSONResponse:
        with SqliteDraftStore(paths.db_path()) as store:
            draft = store.get(draft_id)
            if draft is None:
                return JSONResponse({"ok": False, "error": "unknown draft"}, status_code=404)
            if draft["status"] not in ("pending", "failed"):
                return JSONResponse(
                    {"ok": False, "error": f"draft is {draft['status']}"}, status_code=409
                )
            store.set_status(draft_id, "dismissed")
        return JSONResponse({"ok": True})

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return ctl.status()

    @app.post("/api/scanner/start")
    def start(mode: str = "loop") -> dict[str, Any]:
        chosen = mode if mode in ("loop", "once") else "loop"
        started = ctl.start(chosen)
        return {"started": started, "status": ctl.status()}

    @app.post("/api/scanner/stop")
    def stop() -> dict[str, Any]:
        ctl.stop()
        return {"ok": True, "status": ctl.status()}

    @app.get("/api/logs")
    def logs(after: int = 0) -> dict[str, Any]:
        pairs = buffer.since(after) if after else buffer.recent()
        return {"lines": [{"seq": s, "line": ln} for s, ln in pairs]}

    @app.get("/api/logs/stream")
    async def logs_stream(request: Request, after: int = 0) -> StreamingResponse:
        async def gen() -> AsyncIterator[str]:
            last = after
            while not await request.is_disconnected():
                for seq, line in buffer.since(last):
                    last = seq
                    yield f"data: {json.dumps({'seq': seq, 'line': line})}\n\n"
                await asyncio.sleep(0.7)

        return StreamingResponse(gen(), media_type="text/event-stream")

    app.state.controller = ctl
    app.state.log_buffer = buffer
    app.state.sender = snd
    return app


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>deal-radar</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 14px/1.5 system-ui, sans-serif; background: #0f1115; color: #e6e6e6; }
  header { padding: 12px 18px; background: #171a21; border-bottom: 1px solid #2a2f3a;
           display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  h1 { font-size: 16px; margin: 0; font-weight: 600; }
  main { padding: 18px; display: grid; gap: 18px; grid-template-columns: 1fr 1fr; }
  section { background: #171a21; border: 1px solid #2a2f3a; border-radius: 8px; padding: 14px; }
  section h2 { font-size: 13px; margin: 0 0 10px; color: #9aa4b2; text-transform: uppercase; letter-spacing: .04em; }
  #logsec, #configsec, #draftsec { grid-column: 1 / -1; }
  .draft { border: 1px solid #2a2f3a; border-radius: 6px; padding: 10px; margin-bottom: 10px; }
  .draft textarea { height: 64px; margin-top: 6px; }
  .tag { font-size: 11px; padding: 1px 6px; border-radius: 4px; background: #2a3140; }
  button { background: #2a3140; color: #e6e6e6; border: 1px solid #3a4353; border-radius: 6px;
           padding: 6px 12px; cursor: pointer; font: inherit; }
  button:hover { background: #333c4d; }
  button.primary { background: #2f6f4f; border-color: #3a8a63; }
  button.danger { background: #7a2f38; border-color: #9a3d48; }
  textarea { width: 100%; height: 340px; background: #0b0d11; color: #d8dee9; border: 1px solid #2a2f3a;
             border-radius: 6px; padding: 10px; font: 12px/1.5 ui-monospace, monospace; resize: vertical; }
  #log { height: 300px; overflow: auto; background: #0b0d11; border: 1px solid #2a2f3a; border-radius: 6px;
         padding: 10px; font: 12px/1.45 ui-monospace, monospace; white-space: pre-wrap; }
  .dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; background: #555; }
  .dot.on { background: #4ade80; } .dot.stop { background: #fbbf24; }
  .muted { color: #8892a0; } .err { color: #f87171; } .ok { color: #4ade80; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { text-align: left; padding: 4px 8px; border-bottom: 1px solid #232833; }
  a { color: #7ab7ff; } .rate { color: #fbbf24; font-weight: 600; }
  #savemsg { margin-left: 10px; }
</style>
</head>
<body>
<header>
  <h1>deal-radar</h1>
  <span class="dot" id="dot"></span><span id="statustext" class="muted">…</span>
  <span style="flex:1"></span>
  <button class="primary" onclick="scanner('start?mode=loop')">Start loop</button>
  <button onclick="scanner('start?mode=once')">Scan now</button>
  <button class="danger" onclick="scanner('stop')">Stop</button>
</header>
<main>
  <section id="configsec">
    <h2>Config (config.yaml)</h2>
    <textarea id="config" spellcheck="false"></textarea>
    <div style="margin-top:8px"><button class="primary" onclick="saveConfig()">Validate &amp; save</button>
      <span id="savemsg"></span></div>
  </section>
  <section>
    <h2>Recent listings</h2>
    <table><tbody id="seen"></tbody></table>
  </section>
  <section>
    <h2>Items</h2>
    <div id="items" class="muted">…</div>
  </section>
  <section id="draftsec">
    <h2>Message drafts</h2>
    <div id="drafts" class="muted">…</div>
  </section>
  <section id="logsec">
    <h2>Live log</h2>
    <div id="log"></div>
  </section>
</main>
<script>
const $ = s => document.querySelector(s);
async function refreshStatus() {
  const s = await (await fetch('/api/status')).json();
  const dot = $('#dot'), t = $('#statustext');
  dot.className = 'dot' + (s.stopping ? ' stop' : s.running ? ' on' : '');
  t.textContent = s.stopping ? 'stopping…' : s.running ? ('running (' + s.mode + ')') : 'idle';
  t.className = s.error ? 'err' : 'muted';
  if (s.error) t.textContent += ' — last error: ' + s.error;
}
async function scanner(action) {
  await fetch('/api/scanner/' + action, {method: 'POST'});
  refreshStatus();
}
async function loadConfig() { $('#config').value = await (await fetch('/api/config')).text(); }
async function saveConfig() {
  const msg = $('#savemsg'); msg.textContent = 'saving…'; msg.className = 'muted';
  const r = await fetch('/api/config', {method: 'POST', body: $('#config').value});
  const j = await r.json();
  if (j.ok) { msg.textContent = 'saved ✓ (applies on next scan start)'; msg.className = 'ok'; loadSummary(); }
  else { msg.textContent = 'invalid: ' + j.error.split('\\n')[0]; msg.className = 'err'; }
}
async function loadSeen() {
  const j = await (await fetch('/api/seen')).json();
  $('#seen').innerHTML = (j.rows || []).map(r =>
    '<tr><td class="muted">' + (r.first_seen_ts
      ? new Date(r.first_seen_ts * 1000).toLocaleDateString(undefined, {month: 'short', day: 'numeric'})
      : '?') + '</td>' +
    '<td class="rate">' + (r.rating != null ? (r.rating * 2) + '/10'
      : '<span class="muted" title="not fully scraped / not evaluated">1/10</span>') + '</td>' +
    '<td>' + (r.last_price != null ? '$' + Math.round(r.last_price) : '?') + '</td>' +
    '<td>' + (r.images_analyzed ? '<span title="AI looked through the photos">📷</span> ' : '') +
    '<a href="' + r.url + '" target="_blank" rel="noopener">' +
    (r.title || r.listing_id).replace(/</g,'&lt;') + '</a></td></tr>').join('')
    || '<tr><td class="muted">no listings recorded yet</td></tr>';
}
async function loadSummary() {
  const j = await (await fetch('/api/config/summary')).json();
  if (j.error) { $('#items').innerHTML = '<span class="err">' + j.error.split('\\n')[0] + '</span>'; return; }
  $('#items').innerHTML = j.items.map(i =>
    '<div>' + (i.enabled ? '' : '(disabled) ') + '<b>' + i.name.replace(/</g,'&lt;') + '</b> — $' +
    (i.price_min ?? 0) + '–' + (i.price_max ?? '∞') + ', min ' + i.min_rating + '/5</div>').join('');
}
function fmtDate(ts) {
  return ts ? new Date(ts * 1000).toLocaleDateString(undefined, {month: 'short', day: 'numeric'}) : '?';
}
function draftCard(r) {
  const esc = s => String(s == null ? '' : s).replace(/</g, '&lt;');
  const card = document.createElement('div');
  card.className = 'draft';
  const asking = r.asking_price != null ? '$' + Math.round(r.asking_price) : '$?';
  const offer = r.offer_price != null ? asking + ' &rarr; offer $' + r.offer_price : asking + ' (asking price)';
  card.innerHTML = '<span class="muted">' + fmtDate(r.created_ts) + '</span> <b>' + esc(r.item_name) +
    '</b> — <a href="' + r.url + '" target="_blank" rel="noopener">' + esc(r.title) + '</a> · ' + offer +
    (r.status === 'sending' ? ' · <span class="muted">sending…</span>' : '') +
    (r.error ? ' · <span class="err">' + esc(r.error) + '</span>' : '');
  const ta = document.createElement('textarea');
  ta.value = r.message;  // DOM property: safe for quotes/angle brackets
  card.appendChild(ta);
  if (r.status !== 'sending') {
    const row = document.createElement('div');
    row.style.marginTop = '6px';
    const send = document.createElement('button');
    send.className = 'primary';
    send.textContent = r.status === 'failed' ? 'Retry send' : 'Approve & send';
    send.onclick = () => draftAction(r.id, 'approve', ta.value);
    const dis = document.createElement('button');
    dis.textContent = 'Dismiss';
    dis.style.marginLeft = '8px';
    dis.onclick = () => draftAction(r.id, 'dismiss');
    row.appendChild(send); row.appendChild(dis);
    card.appendChild(row);
  }
  return card;
}
async function loadDrafts() {
  const active = document.activeElement;
  if (active && active.closest && active.closest('#drafts')) return; // don't clobber an edit
  const j = await (await fetch('/api/drafts')).json();
  const el = $('#drafts');
  el.textContent = '';
  const rows = j.rows || [];
  if (!rows.length) { el.innerHTML = '<span class="muted">no drafts yet — enable messaging in the config</span>'; return; }
  for (const r of rows) {
    if (r.status === 'pending' || r.status === 'failed' || r.status === 'sending') {
      el.appendChild(draftCard(r));
    } else {
      const line = document.createElement('div');
      line.className = 'muted';
      line.innerHTML = '<span class="tag">' + r.status + '</span> ' + fmtDate(r.updated_ts) + ' ' +
        String(r.item_name).replace(/</g, '&lt;') + ': ' + String(r.title).replace(/</g, '&lt;');
      el.appendChild(line);
    }
  }
}
async function draftAction(id, action, message) {
  const opts = {method: 'POST'};
  if (message !== undefined) {
    opts.headers = {'Content-Type': 'application/json'};
    opts.body = JSON.stringify({message});
  }
  const r = await fetch('/api/drafts/' + id + '/' + action, opts);
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    alert(j.error || 'request failed');
  }
  loadDrafts();
}
const log = $('#log');
new EventSource('/api/logs/stream').onmessage = e => {
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
  log.textContent += JSON.parse(e.data).line + '\\n';
  if (atBottom) log.scrollTop = log.scrollHeight;
};
loadConfig(); loadSeen(); loadSummary(); loadDrafts(); refreshStatus();
setInterval(refreshStatus, 3000); setInterval(loadSeen, 15000); setInterval(loadDrafts, 10000);
</script>
</body>
</html>
"""
