// The setup wizard: what's missing, why it matters, and how to fix it.
//
// Readiness is re-derived from live checks on every load — a stored "done"
// flag would leave someone stuck if their sign-in expired or their key was
// revoked. What *is* remembered is only when a slow check last passed.

import { $, api, esc } from '/static/common.js';

const ICONS = { ok: '✓', warn: '!', fail: '✕', unknown: '?' };
let lastSignature = '';

/** Actions the wizard can offer, keyed by the `action` a check reports. */
const ACTIONS = {
  save_api_key: renderKeyForm,
  test_api_key: renderKeyTest,
  facebook_login: renderFacebookLogin,
  facebook_check: renderFacebookCheck,
  test_notify: renderTestNotify,
  open_settings: renderOpenSettings,
};

export async function refreshSetup() {
  let data;
  try {
    data = await api('/api/setup/status');
  } catch {
    return; // the offline banner already covers this
  }
  render(data);
  return data;
}

function render(data) {
  const wizard = $('#wizard');
  const strip = $('#setupstrip');
  const blocking = data.checks.filter((c) => c.blocking && c.state === 'fail');
  const attention = data.checks.filter((c) => c.state === 'warn' || c.state === 'fail');

  // Blocking failures take over the page: nothing here can work yet, and the
  // old UI's answer was a line of grey text three seconds after a failed click.
  document.body.classList.toggle('setup-required', data.setup_required);
  wizard.hidden = !data.setup_required;
  if (data.setup_required) {
    wizard.innerHTML =
      '<h2>Let’s get deal-radar working</h2>' +
      '<p class="lede">' +
      blocking.length +
      (blocking.length === 1 ? ' thing needs' : ' things need') +
      ' sorting out before deal-radar can look for anything.</p>' +
      '<div class="checks"></div>';
    fillChecks(wizard.querySelector('.checks'), data.checks, data);
    return;
  }

  // Everything essential works: shrink to a dismissible strip.
  const signature = attention.map((c) => c.id + ':' + c.state).join('|');
  const dismissed = localStorage.getItem('dr.setupDismissed');
  if (!attention.length) {
    strip.hidden = true;
    return;
  }
  // A *new* problem re-shows the strip even if an older one was dismissed.
  if (dismissed === signature) {
    strip.hidden = true;
    lastSignature = signature;
    return;
  }
  lastSignature = signature;
  strip.hidden = false;
  strip.innerHTML =
    '<span>' +
    attention.length +
    (attention.length === 1 ? ' thing needs' : ' things need') +
    ' attention.</span> ' +
    '<button class="tiny" id="reviewsetup">Review</button> ' +
    '<button class="tiny" id="dismisssetup">Not now</button>' +
    '<div class="checks" hidden></div>';
  $('#reviewsetup').onclick = () => {
    const box = strip.querySelector('.checks');
    box.hidden = !box.hidden;
    if (!box.hidden) fillChecks(box, data.checks, data);
  };
  $('#dismisssetup').onclick = () => {
    localStorage.setItem('dr.setupDismissed', lastSignature);
    strip.hidden = true;
  };
}

function fillChecks(container, checks, data) {
  container.textContent = '';
  for (const check of checks) {
    if (check.state === 'ok' && !check.action) {
      container.appendChild(row(check, data, /* compact */ true));
    } else {
      container.appendChild(row(check, data, false));
    }
  }
}

function row(check, data, compact) {
  const el = document.createElement('div');
  el.className = 'check check-' + check.state;
  const head = document.createElement('div');
  head.className = 'check-head';
  head.innerHTML =
    '<span class="check-icon">' + ICONS[check.state] + '</span>' +
    '<b>' + esc(check.label) + '</b>' +
    '<span class="muted"> — ' + esc(check.detail) + '</span>';
  el.appendChild(head);
  if (compact) return el;

  if (check.fix) {
    const fix = document.createElement('div');
    fix.className = 'check-fix';
    fix.textContent = check.fix;
    el.appendChild(fix);
  }
  if (check.copyable) {
    el.appendChild(commandBox(check.copyable));
  }
  const build = ACTIONS[check.action];
  if (build) el.appendChild(build(check, data));
  return el;
}

function commandBox(command) {
  const box = document.createElement('div');
  box.className = 'command';
  const code = document.createElement('code');
  code.textContent = command;
  const copy = document.createElement('button');
  copy.className = 'tiny';
  copy.textContent = 'Copy';
  copy.onclick = async () => {
    try {
      await navigator.clipboard.writeText(command);
      copy.textContent = 'Copied';
      setTimeout(() => (copy.textContent = 'Copy'), 1500);
    } catch {
      copy.textContent = 'Select it and copy';
    }
  };
  const recheck = document.createElement('button');
  recheck.className = 'tiny';
  recheck.textContent = 'Re-check';
  recheck.onclick = refreshSetup;
  box.append(code, copy, recheck);
  return box;
}

/** A row of buttons plus a shared status line for one action. */
function actionRow(...buttons) {
  const wrap = document.createElement('div');
  wrap.className = 'check-actions';
  const status = document.createElement('span');
  status.className = 'muted action-status';
  for (const b of buttons) wrap.appendChild(b);
  wrap.appendChild(status);
  wrap.status = status;
  return wrap;
}

function button(label, { primary = false } = {}) {
  const b = document.createElement('button');
  b.textContent = label;
  if (primary) b.className = 'primary';
  return b;
}

async function run(status, busyText, fn) {
  status.className = 'muted action-status';
  status.textContent = busyText;
  try {
    const result = await fn();
    status.className = 'ok action-status';
    status.textContent = (result && result.message) || 'Done.';
    return result;
  } catch (err) {
    status.className = 'err action-status';
    status.textContent = err.message;
    return null;
  }
}

// --- per-action UI -------------------------------------------------------------

function renderKeyForm(check) {
  const wrap = document.createElement('div');
  wrap.className = 'check-actions';
  const input = document.createElement('input');
  input.type = 'password';
  input.placeholder = 'sk-ant-…';
  input.autocomplete = 'off';
  input.spellcheck = false;
  const save = button('Save key', { primary: true });
  const status = document.createElement('span');
  status.className = 'muted action-status';
  const help = document.createElement('div');
  help.className = 'check-help';
  help.textContent =
    'Get one from console.anthropic.com. It is stored on this computer only, in a ' +
    'file called .env next to your settings, readable only by you — and it is ' +
    'already excluded from version control.';
  save.onclick = async () => {
    const result = await run(status, 'Saving…', () =>
      api('/api/setup/api-key', { method: 'POST', body: { key: input.value } }),
    );
    if (result) {
      input.value = '';
      status.textContent = 'Saved (ends in ' + result.hint + '). Checking it works…';
      await run(status, 'Checking…', () =>
        api('/api/setup/api-key/test', { method: 'POST', body: {} }),
      );
      refreshSetup();
    }
  };
  wrap.append(input, save, status);
  const box = document.createElement('div');
  box.append(wrap, help);
  return box;
}

function renderKeyTest(check) {
  const quick = button('Check it works');
  const deep = button('Full test (costs about $0.00001)');
  const forget = button('Forget this key');
  const wrap = actionRow(quick, deep, forget);
  quick.onclick = () =>
    run(wrap.status, 'Checking…', () =>
      api('/api/setup/api-key/test', { method: 'POST', body: {} }),
    ).then(refreshSetup);
  deep.onclick = () =>
    run(wrap.status, 'Making one tiny request…', () =>
      api('/api/setup/api-key/test', { method: 'POST', body: { deep: true } }),
    ).then(refreshSetup);
  forget.onclick = async () => {
    if (!confirm('Forget the saved key? You will need to paste it again.')) return;
    await run(wrap.status, 'Removing…', () => api('/api/setup/api-key', { method: 'DELETE' }));
    refreshSetup();
  };
  return wrap;
}

function renderFacebookCheck(check) {
  const go = button('Check it now');
  const wrap = actionRow(go);
  go.onclick = async () => {
    wrap.status.className = 'muted action-status';
    wrap.status.textContent = 'Opening Facebook in the background — about 20 seconds…';
    try {
      await api('/api/setup/facebook/check', { method: 'POST', body: {} });
    } catch (err) {
      wrap.status.className = 'err action-status';
      wrap.status.textContent = err.message;
      return;
    }
    const poll = setInterval(async () => {
      const s = await api('/api/setup/facebook/check');
      if (s.busy) return;
      clearInterval(poll);
      const result = s.result || {};
      wrap.status.className = (result.ok ? 'ok' : 'err') + ' action-status';
      wrap.status.textContent = result.message || s.error || 'Could not check.';
      refreshSetup();
    }, 2000);
  };
  return wrap;
}

function renderFacebookLogin(check, data) {
  const go = button('Sign in to Facebook', { primary: true });
  const done = button("I'm signed in", { primary: true });
  const cancel = button('Cancel');
  const wrap = actionRow(go, done, cancel);
  done.hidden = true;
  cancel.hidden = true;

  if (data && data.can_open_browser === false) {
    go.disabled = true;
    wrap.status.textContent =
      'This computer can’t open a browser window, so signing in here isn’t possible.';
    return wrap;
  }

  let poll = null;
  const showState = (s) => {
    wrap.status.className = (s.state === 'error' ? 'err' : 'muted') + ' action-status';
    wrap.status.textContent = s.error || s.message || '';
    go.hidden = s.busy;
    done.hidden = s.state !== 'waiting';
    cancel.hidden = !s.busy;
    if (!s.busy && poll) {
      clearInterval(poll);
      poll = null;
      refreshSetup();
    }
  };

  go.onclick = async () => {
    try {
      const r = await api('/api/setup/facebook/login', { method: 'POST', body: {} });
      showState(r.status);
    } catch (err) {
      wrap.status.className = 'err action-status';
      wrap.status.textContent = err.message;
      return;
    }
    poll = setInterval(async () => showState(await api('/api/setup/facebook/login')), 1500);
  };
  done.onclick = async () =>
    showState((await api('/api/setup/facebook/login/finish', { method: 'POST', body: {} })).status);
  cancel.onclick = async () =>
    showState((await api('/api/setup/facebook/login/cancel', { method: 'POST', body: {} })).status);
  return wrap;
}

function renderTestNotify(check) {
  const go = button('Send a test alert');
  const wrap = actionRow(go);
  go.onclick = () =>
    run(wrap.status, 'Sending…', () => api('/api/setup/test-notify', { method: 'POST', body: {} }));
  return wrap;
}

function renderOpenSettings(check) {
  const go = button('Open settings', { primary: true });
  const wrap = actionRow(go);
  go.onclick = () => {
    document.body.classList.remove('setup-required');
    $('#wizard').hidden = true;
    $('#configsec').scrollIntoView({ behavior: 'smooth' });
    // Focus the first control in the settings form, whatever it happens to be.
    $('#configform').querySelector('input, textarea, select, button')?.focus();
  };
  return wrap;
}
