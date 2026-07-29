import {
  $,
  api,
  esc,
  escAttr,
  fmtDate,
  fmtMoney,
  linkTo,
  onConnectionChange,
} from '/static/common.js';
import { refreshSetup } from '/static/setup.js';
import { initConfigForm } from '/static/config.js';

const BEST_EMPTY = '<tr><td class="muted">Nothing found yet. Run a scan and anything worth a look will appear here.</td></tr>';
const LOG_MAX_LINES = 1000;

// --- connection banner --------------------------------------------------------

onConnectionChange((offline) => {
  $('#offline').hidden = !offline;
});

// --- scanner ------------------------------------------------------------------

const MODE_LABEL = { loop: 'watching', once: 'scanning', free: 'test scan' };

async function refreshStatus() {
  const s = await api('/api/status');
  const dot = $('#dot');
  const t = $('#statustext');
  dot.className = 'dot' + (s.stopping ? ' stop' : s.running ? ' on' : '');
  t.textContent = s.stopping
    ? 'stopping — finishing the current listing, up to 30 seconds'
    : s.running
      ? MODE_LABEL[s.mode] || 'running'
      : 'idle';
  t.className = s.error ? 'err' : 'muted';
  if (s.error) t.textContent += ' — last error: ' + s.error;

  // What this scan has actually cost so far, measured (not estimated).
  const spend = $('#spend');
  const scan = s.spend && s.spend.scan;
  if (scan && scan.evals) {
    spend.hidden = false;
    spend.textContent =
      scan.cost_known
        ? `${scan.evals} checked · $${scan.cost.toFixed(3)} this scan`
        : `${scan.evals} checked`;
  } else {
    spend.hidden = true;
  }

  showProgress(s);
}

/**
 * A determinate bar for a scan that can run a quarter of an hour.
 *
 * Before this the only feedback was a green dot, which is indistinguishable
 * from "wedged" when a single listing takes 50 seconds.
 */
function showProgress(s) {
  const bar = $('#progressbar');
  const p = s.progress;
  if (!s.running || !p) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  const of = p.of || 0;
  // Count whole items already done, plus how far through the current one.
  const perItem = p.item_count > 0 ? 1 / p.item_count : 1;
  const withinItem = of > 0 ? Math.min(1, p.checked / of) : 0;
  const fraction = Math.min(
    1,
    Math.max(0, (p.item_index - 1) * perItem + withinItem * perItem),
  );
  $('#progressfill').style.width = (fraction * 100).toFixed(1) + '%';

  const where =
    p.item_count > 1 ? ` (${p.item_index} of ${p.item_count}: ${p.item})` : ` — ${p.item}`;
  const counted = of ? `Checked ${p.checked} of ${of}` : `Found ${p.found}`;
  $('#progresswhat').textContent =
    `${p.message}${where}. ${counted}` +
    (p.matched ? `, ${p.matched} worth a look so far.` : '.');
  $('#progresseta').textContent = s.eta || '';
}

/** What a scan is likely to cost, shown before the click rather than after. */
async function refreshCost() {
  const bar = $('#costbar');
  let p;
  try {
    p = await api('/api/pricing');
  } catch {
    return;
  }
  bar.hidden = false;
  if (!p.known) {
    $('#costtext').textContent =
      p.reason === 'settings are not valid'
        ? 'Fix your settings to see what a scan would cost.'
        : `Cost unknown for the AI model “${p.model}”.`;
    costEstimate = null;
    return;
  }
  costEstimate = p.max_cost;
  const mins = Math.round(p.poll_interval_seconds / 60);
  $('#costtext').textContent =
    `A scan checks up to ${p.max_listings_checked} listings ` +
    `(${p.items} × ${p.max_evaluations_per_item}) and may cost about ` +
    `$${p.max_cost.toFixed(2)}. Usually much less — listings already checked are skipped. ` +
    `“Keep watching” repeats that about every ${mins} minutes until you stop it.`;
}

let costEstimate = null;
const CONFIRM_ABOVE = 0.25;

async function scanner(action) {
  // Only confirm when it's actually worth interrupting for; a dialog on every
  // click just teaches people to dismiss dialogs.
  const isFree = action.includes('free=1');
  if (!isFree && !action.includes('stop') && costEstimate != null) {
    const firstEver = !localStorage.getItem('dr.hasScanned');
    if (costEstimate > CONFIRM_ABOVE || firstEver) {
      const what = action.includes('loop') ? 'each round of watching' : 'this scan';
      if (!confirm(`This may cost about $${costEstimate.toFixed(2)} for ${what}. Continue?`)) {
        return;
      }
    }
    localStorage.setItem('dr.hasScanned', '1');
  }
  await api('/api/scanner/' + action, { method: 'POST' });
  refreshStatus();
}

// --- seen listings --------------------------------------------------------------

function seenRow(r) {
  // A null rating means the listing was filtered out before the AI ever saw it
  // — the price was outside your range, or it matched an excluded word. It does
  // NOT mean "failed to scrape": an evaluation error never reaches the store at
  // all (pipeline.py), so the old "not fully scraped / not evaluated" tooltip
  // conflated two things, one of which cannot happen.
  const score =
    r.rating != null
      ? `<span class="rate" title="How well the AI thought this matches what you described">${r.rating} of 5</span>`
      : '<span class="muted" title="Outside your price range, or it matched a word you excluded">Skipped</span>';
  const pills =
    (r.matched ? '<span class="pill match" title="The AI thinks this is what you\'re looking for">Match</span> ' : '') +
    (r.images_analyzed ? '<span class="pill" title="The AI looked at the listing\'s photos too">Photos checked</span> ' : '');
  return (
    '<tr><td class="muted nowrap">' + fmtDate(r.first_seen_ts) + '</td>' +
    '<td class="nowrap">' + score + '</td>' +
    '<td class="nowrap">' + fmtMoney(r.last_price) + '</td>' +
    '<td>' + pills + linkTo(r.url, r.title || r.listing_id) + '</td>' +
    '<td><button class="del tiny" ' +
    'data-item="' + escAttr(r.item_name) + '" ' +
    'data-id="' + escAttr(r.listing_id) + '" ' +
    'data-title="' + escAttr(r.title || r.listing_id) + '">Forget</button></td></tr>'
  );
}

let seenExpanded = false;
const SEEN_COLLAPSED = 10;

async function loadSeen() {
  const j = await api('/api/seen');
  const rows = j.rows || [];
  const shown = seenExpanded ? rows : rows.slice(0, SEEN_COLLAPSED);
  $('#seen').innerHTML =
    shown.map(seenRow).join('') ||
    '<tr><td class="muted">Nothing checked yet.</td></tr>';
  const more = $('#seenmore');
  more.hidden = seenExpanded || rows.length <= SEEN_COLLAPSED;
  more.textContent = `Show all ${rows.length}`;
}

async function clearSeen() {
  if (
    !confirm(
      'Forget every listing deal-radar has checked?\n\n' +
        'They can turn up again in a future scan — and be paid for again.',
    )
  ) {
    return;
  }
  const j = await api('/api/seen/clear', { method: 'POST' });
  $('#seenmsg').textContent = `Forgot ${j.deleted || 0} listing(s).`;
  loadSeen();
  loadBest();
}

/** Auto-loaded, not hidden behind a button: it's the thing you came to see. */
async function loadBest() {
  const j = await api('/api/seen/best?limit=8');
  $('#best').innerHTML = (j.rows || []).map(seenRow).join('') || BEST_EMPTY;
}

document.addEventListener('click', async (e) => {
  const b = e.target.closest && e.target.closest('button.del');
  if (!b) return;
  // Deleting is irreversible for that row and lets the listing be re-scanned
  // (and re-charged for), so it gets the same confirmation as "Clear all".
  const what = b.dataset.title || 'this listing';
  if (!confirm('Forget “' + what + '”? It can turn up again in a future scan.')) return;
  await api('/api/seen/delete', {
    method: 'POST',
    body: { item_name: b.dataset.item, listing_id: b.dataset.id },
  });
  loadSeen();
});

// --- message drafts -------------------------------------------------------------

// The five states a draft can be in, in words rather than database values.
const DRAFT_STATUS = {
  pending: 'Waiting for you',
  sending: 'Sending…',
  sent: 'Sent',
  failed: "Didn't send",
  dismissed: 'Skipped',
};

function draftCard(r) {
  const card = document.createElement('div');
  card.className = 'draft';
  const asking = fmtMoney(r.asking_price);
  const offer =
    r.offer_price != null ? asking + ' &rarr; offer $' + r.offer_price : asking + ' (asking price)';
  card.innerHTML =
    '<span class="muted">' + fmtDate(r.created_ts) + '</span> <b>' + esc(r.item_name) + '</b> — ' +
    linkTo(r.url, r.title) + ' · ' + offer +
    (r.status === 'sending' ? ' · <span class="muted">sending…</span>' : '') +
    (r.error ? ' · <span class="err">' + esc(r.error) + '</span>' : '');
  const ta = document.createElement('textarea');
  ta.value = r.message; // DOM property: safe for quotes/angle brackets
  card.appendChild(ta);
  if (r.status !== 'sending') {
    const row = document.createElement('div');
    row.className = 'draft-actions';
    const send = document.createElement('button');
    send.className = 'primary';
    send.textContent = r.status === 'failed' ? 'Retry send' : 'Approve & send';
    send.onclick = () => draftAction(r.id, 'approve', ta.value);
    const dis = document.createElement('button');
    dis.textContent = 'Dismiss';
    dis.onclick = () => draftAction(r.id, 'dismiss');
    row.appendChild(send);
    row.appendChild(dis);
    card.appendChild(row);
  }
  return card;
}

async function loadDrafts() {
  const active = document.activeElement;
  if (active && active.closest && active.closest('#drafts')) return; // don't clobber an edit
  const j = await api('/api/drafts');
  // Hidden entirely when messaging is off — an empty panel is just noise.
  $('#draftsec').hidden = !(j.rows || []).length && !j.messaging_enabled;
  const el = $('#drafts');
  el.textContent = '';
  const rows = j.rows || [];
  if (!rows.length) {
    el.innerHTML =
      '<span class="muted">Nothing waiting. When something matches, the message ' +
      'deal-radar writes for you will appear here.</span>';
    return;
  }
  for (const r of rows) {
    if (r.status === 'pending' || r.status === 'failed' || r.status === 'sending') {
      el.appendChild(draftCard(r));
    } else {
      const line = document.createElement('div');
      line.className = 'muted';
      line.innerHTML =
        '<span class="tag">' + esc(DRAFT_STATUS[r.status] || r.status) + '</span> ' +
        fmtDate(r.updated_ts) + ' ' +
        esc(r.item_name) + ': ' + esc(r.title);
      el.appendChild(line);
    }
  }
}

async function draftAction(id, action, message) {
  try {
    await api('/api/drafts/' + id + '/' + action, {
      method: 'POST',
      body: message !== undefined ? { message } : {},
    });
  } catch (err) {
    alert(err.message);
  }
  loadDrafts();
}

// --- live log ---------------------------------------------------------------------

const logEl = $('#log');
const logDot = $('#logdot');
const logLines = [];
const PROBLEM = /\b(ERROR|WARNING|CRITICAL|Traceback|failed)\b/;

function renderLog() {
  const onlyProblems = $('#logproblems').checked;
  const lines = onlyProblems ? logLines.filter((l) => PROBLEM.test(l)) : logLines;
  logEl.textContent = lines.join('\n') + (lines.length ? '\n' : '');
  logEl.scrollTop = logEl.scrollHeight;
}

function appendLog(line) {
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  logLines.push(line);
  // Unbounded growth made a long-running loop chew memory; drop the oldest
  // fifth at a time rather than one line per append.
  if (logLines.length > LOG_MAX_LINES) {
    logLines.splice(0, Math.floor(LOG_MAX_LINES * 0.2));
    renderLog();
    return;
  }
  if ($('#logproblems').checked && !PROBLEM.test(line)) return;
  logEl.textContent += line + '\n';
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}

async function startLogStream() {
  // Fetch the backlog once, then stream from that point. The server also
  // honours Last-Event-ID, so an automatic reconnect resumes instead of
  // replaying the whole ring buffer (which used to duplicate the pane).
  let last = 0;
  try {
    const j = await api('/api/logs');
    for (const item of j.lines || []) {
      appendLog(item.line);
      last = item.seq;
    }
  } catch {
    /* the stream below will retry */
  }
  const source = new EventSource('/api/logs/stream?after=' + last);
  source.onmessage = (e) => appendLog(JSON.parse(e.data).line);
  source.onopen = () => logDot.className = 'dot on';
  source.onerror = () => logDot.className = 'dot stop';
}

// --- polling ------------------------------------------------------------------------

// One shared tick instead of three independent setIntervals, so a background
// tab stops polling entirely and a dead server backs off instead of hammering.
const POLLS = [
  { fn: refreshStatus, every: 3000, next: 0 },
  { fn: loadDrafts, every: 10000, next: 0 },
  { fn: loadSeen, every: 15000, next: 0 },
  { fn: loadBest, every: 20000, next: 0 },
  { fn: refreshSetup, every: 30000, next: 0 },
];
let backoff = 1;

async function tick() {
  if (document.visibilityState !== 'visible') return;
  const now = Date.now();
  let failed = false;
  for (const poll of POLLS) {
    if (now < poll.next) continue;
    try {
      await poll.fn();
    } catch {
      failed = true;
    }
    poll.next = now + poll.every * backoff;
  }
  backoff = failed ? Math.min(backoff * 2, 10) : 1;
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    for (const poll of POLLS) poll.next = 0; // refresh immediately on return
    tick();
  }
});

// --- wiring -----------------------------------------------------------------------

for (const b of document.querySelectorAll('[data-scanner]')) {
  b.addEventListener('click', () => scanner(b.dataset.scanner));
}
$('#clearseen').addEventListener('click', clearSeen);
$('#seenmore').addEventListener('click', () => {
  seenExpanded = true;
  loadSeen();
});
$('#retry').addEventListener('click', () => {
  for (const poll of POLLS) poll.next = 0;
  tick();
});
$('#clearlog').addEventListener('click', () => {
  logLines.length = 0;
  renderLog();
});
$('#logproblems').addEventListener('change', renderLog);

refreshCost().catch(() => {});
initConfigForm().catch((e) => console.error(e));
startLogStream();
tick();
setInterval(tick, 1000);
