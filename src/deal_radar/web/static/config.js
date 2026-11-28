// The guided settings form.
//
// Renders generically from the spec the server sends, so all the wording lives
// in Python where it can be unit-tested. Editing produces a draft mapping; a
// save sends the whole mapping and the etag it was loaded with, and the server
// works out the minimal patch - which is what lets a hand-written config keep
// its comments.
//
// The raw YAML editor is still here, under Advanced. Switching to it is a mode
// change on one document, not a second copy.

import { $, api, esc } from './common.js';

let spec = null; // from /api/config/formspec
let state = null; // from /api/config/form
let draft = null; // the mapping being edited
let etag = null;
let dirty = false;
let rawMode = false;

const byPath = new Map(); // spec path -> field spec

// --- small helpers ---------------------------------------------------------------

const clone = (v) => JSON.parse(JSON.stringify(v));

/** Read/write a nested value by path array, creating objects as needed. */
function getIn(obj, path) {
  let cur = obj;
  for (const step of path) {
    if (cur == null) return undefined;
    cur = cur[step];
  }
  return cur;
}

function setIn(obj, path, value) {
  let cur = obj;
  for (const step of path.slice(0, -1)) {
    if (cur[step] == null || typeof cur[step] !== 'object') cur[step] = {};
    cur = cur[step];
  }
  const last = path[path.length - 1];
  // Absent means "use the default"; storing null would write `key: null`.
  if (value === undefined) delete cur[last];
  else cur[last] = value;
}

/** The spec path for a concrete data path: items.0.name -> items.*.name */
function specPathFor(path) {
  return path.map((p) => (typeof p === 'number' ? '*' : p)).join('.');
}

function markDirty() {
  dirty = true;
  $('#formsave').disabled = false;
  $('#formstatus').textContent = 'unsaved changes';
  $('#formstatus').className = 'muted';
  scheduleValidate();
}

// --- validation ------------------------------------------------------------------

let validateTimer = null;
let inFlight = null;

function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(runValidate, 400);
}

async function runValidate() {
  if (inFlight) inFlight.abort();
  const controller = new AbortController();
  inFlight = controller;
  try {
    const result = await api('/api/config/validate', {
      method: 'POST',
      body: { config: draft },
    });
    showErrors(result.errors || [], result.warnings || []);
  } catch {
    /* the offline banner covers this */
  } finally {
    if (inFlight === controller) inFlight = null;
  }
}

function showErrors(errors, warnings) {
  for (const el of document.querySelectorAll('.field-error')) el.remove();
  for (const el of document.querySelectorAll('[data-loc]')) {
    el.classList.remove('invalid');
    el.removeAttribute('aria-invalid');
  }
  const summary = $('#formerrors');
  summary.textContent = '';
  summary.hidden = !errors.length && !warnings.length;

  for (const error of errors) {
    // The renderer bound every control with the same path it read its value
    // from, so a pydantic loc maps to a control by construction.
    const loc = (error.loc || []).join('.');
    const control = document.querySelector(`[data-loc="${CSS.escape(loc)}"]`);
    if (control) {
      control.classList.add('invalid');
      control.setAttribute('aria-invalid', 'true');
      const note = document.createElement('div');
      note.className = 'field-error';
      note.textContent = error.msg;
      control.closest('.field')?.appendChild(note);
    }
    summary.appendChild(summaryLine('err', error.msg, loc, error.detail));
  }
  for (const warning of warnings) {
    summary.appendChild(summaryLine('warn', warning.message, (warning.loc || []).join('.')));
  }
  $('#formsave').disabled = errors.length > 0 || !dirty;
}

function summaryLine(kind, message, loc, detail) {
  const row = document.createElement('div');
  row.className = 'summary-line ' + kind;
  const text = document.createElement(loc ? 'button' : 'span');
  text.textContent = message;
  if (loc) {
    text.className = 'linky';
    text.onclick = () => {
      const control = document.querySelector(`[data-loc="${CSS.escape(loc)}"]`);
      if (!control) return;
      control.closest('details')?.setAttribute('open', '');
      control.scrollIntoView({ behavior: 'smooth', block: 'center' });
      control.focus();
    };
  }
  row.appendChild(text);
  if (detail && detail !== message) {
    const more = document.createElement('details');
    const sum = document.createElement('summary');
    sum.textContent = 'technical detail';
    const pre = document.createElement('pre');
    pre.textContent = detail;
    more.append(sum, pre);
    row.appendChild(more);
  }
  return row;
}

// --- field rendering ----------------------------------------------------------------

// Widgets that need the whole row: free text, tag lists, and anything with a
// row of buttons in it. Toggles take a full row too, as a label-left /
// switch-right settings row.
const WIDE_WIDGETS = ['textarea', 'topic', 'chips', 'rating', 'marketplace-checkboxes', 'toggle'];

/** `$ [ 120 ]` / `[ 40 ] km`: the unit sits beside the number, not in the help. */
function withAffix(fieldSpec, control) {
  if (!(fieldSpec.prefix || fieldSpec.unit) || control.tagName !== 'INPUT') return control;
  const box = document.createElement('div');
  box.className = 'affix';
  if (fieldSpec.prefix) {
    const pre = document.createElement('span');
    pre.className = 'affix-text';
    pre.textContent = fieldSpec.prefix;
    box.appendChild(pre);
  }
  box.appendChild(control);
  if (fieldSpec.unit) {
    const post = document.createElement('span');
    post.className = 'affix-text';
    post.textContent = fieldSpec.unit;
    box.appendChild(post);
  }
  box.onclick = (e) => {
    if (e.target === box || e.target.className === 'affix-text') control.focus();
  };
  return box;
}

function fieldShell(fieldSpec, path, control, { inline = false } = {}) {
  const wrap = document.createElement('div');
  const wide = WIDE_WIDGETS.includes(fieldSpec.widget) || fieldSpec.warning;
  wrap.className = 'field' + (inline ? ' field-inline' : '') + (wide ? ' field-wide' : '');
  const label = document.createElement('label');
  label.className = 'field-label';
  label.textContent = fieldSpec.label;
  label.htmlFor = control.id;
  wrap.appendChild(label);
  wrap.appendChild(withAffix(fieldSpec, control));
  if (fieldSpec.warning) {
    const warn = document.createElement('div');
    warn.className = 'field-warning';
    warn.textContent = fieldSpec.warning;
    wrap.appendChild(warn);
  }
  if (fieldSpec.help) {
    const help = document.createElement('div');
    help.className = 'field-help';
    help.textContent = fieldSpec.help;
    wrap.appendChild(help);
  }
  if (fieldSpec.example) {
    const details = document.createElement('details');
    details.className = 'example';
    const summary = document.createElement('summary');
    summary.textContent = 'Show an example';
    const pre = document.createElement('pre');
    pre.textContent = fieldSpec.example;
    const use = document.createElement('button');
    use.className = 'tiny';
    use.type = 'button';
    use.textContent = 'Use this as a starting point';
    use.onclick = () => {
      control.value = fieldSpec.example;
      control.dispatchEvent(new Event('input'));
    };
    details.append(summary, pre, use);
    wrap.appendChild(details);
  }
  return wrap;
}

let controlSeq = 0;

function renderField(path, fieldSpec) {
  const value = getIn(draft, path);
  const loc = path.join('.');
  const id = 'ctl' + ++controlSeq;
  let control;

  switch (fieldSpec.widget) {
    case 'toggle': {
      control = document.createElement('input');
      control.type = 'checkbox';
      control.checked = value ?? fieldSpec.default ?? false;
      control.onchange = () => {
        setIn(draft, path, control.checked);
        markDirty();
      };
      break;
    }
    case 'textarea': {
      control = document.createElement('textarea');
      control.rows = fieldSpec.rows || 6;
      control.value = value ?? '';
      if (fieldSpec.placeholder) control.placeholder = fieldSpec.placeholder;
      control.oninput = () => {
        setIn(draft, path, control.value || undefined);
        markDirty();
      };
      break;
    }
    case 'chips':
      control = chipsControl(path, value || []);
      break;
    case 'rating':
      control = ratingControl(path, value, fieldSpec);
      break;
    case 'select': {
      control = document.createElement('select');
      for (const option of fieldSpec.options || []) {
        const el = document.createElement('option');
        el.value = String(option.value);
        el.textContent = option.label;
        control.appendChild(el);
      }
      const current = value ?? fieldSpec.default;
      if (![...control.options].some((o) => o.value === String(current))) {
        const el = document.createElement('option');
        el.value = String(current);
        el.textContent = `${current} (custom)`;
        control.appendChild(el);
      }
      control.value = String(current);
      control.onchange = () => {
        const raw = control.value;
        setIn(draft, path, /^-?\d+$/.test(raw) ? Number(raw) : raw);
        markDirty();
      };
      break;
    }
    case 'combo': {
      control = document.createElement('input');
      control.setAttribute('list', 'list-' + id);
      control.value = value ?? fieldSpec.default ?? '';
      const list = document.createElement('datalist');
      list.id = 'list-' + id;
      for (const option of fieldSpec.options || []) {
        const el = document.createElement('option');
        el.value = option.value;
        el.label = option.label;
        list.appendChild(el);
      }
      control.oninput = () => {
        setIn(draft, path, control.value || undefined);
        markDirty();
      };
      control.appendChild(list);
      break;
    }
    case 'marketplace-checkboxes':
      control = marketplaceControl(path, value || []);
      break;
    case 'money':
    case 'number':
    case 'percent': {
      control = document.createElement('input');
      control.type = 'number';
      if (fieldSpec.min != null) control.min = fieldSpec.min;
      if (fieldSpec.max != null) control.max = fieldSpec.max;
      control.value = value ?? '';
      // An unset field shows what it actually falls back to. Saying "no limit"
      // for something that defaults to 25 is worse than saying nothing.
      control.placeholder = fieldSpec.required
        ? ''
        : fieldSpec.default != null
          ? String(fieldSpec.default)
          : 'no limit';
      control.oninput = () => {
        // Empty means "not set", never 0 - writing 0 for a blank price cap
        // would silently reject every listing.
        const raw = control.value.trim();
        setIn(draft, path, raw === '' ? undefined : Number(raw));
        markDirty();
      };
      break;
    }
    case 'secret': {
      control = document.createElement('input');
      control.type = 'text';
      control.value = value ?? '';
      control.autocomplete = 'off';
      control.oninput = () => {
        setIn(draft, path, control.value || undefined);
        markDirty();
      };
      break;
    }
    case 'topic':
      control = topicControl(path, value);
      break;
    case 'notifier-type':
      return null; // rendered as the card's own header
    default: {
      control = document.createElement('input');
      control.type = 'text';
      control.value = value ?? '';
      if (fieldSpec.placeholder) control.placeholder = fieldSpec.placeholder;
      control.oninput = () => {
        setIn(draft, path, control.value || undefined);
        markDirty();
      };
    }
  }

  control.id = id;
  control.dataset.loc = loc;
  const inline = ['toggle'].includes(fieldSpec.widget);
  return fieldShell(fieldSpec, path, control, { inline });
}

function chipsControl(path, values) {
  const box = document.createElement('div');
  box.className = 'chips';
  box.tabIndex = -1;
  const list = document.createElement('div');
  list.className = 'chip-list';
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'type and press Enter';

  const redraw = () => {
    list.textContent = '';
    for (const [index, value] of values.entries()) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = value;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.textContent = '×';
      remove.title = 'remove';
      remove.onclick = () => {
        values.splice(index, 1);
        commit();
      };
      chip.appendChild(remove);
      list.appendChild(chip);
    }
  };
  const commit = () => {
    setIn(draft, path, values.length ? values : []);
    redraw();
    markDirty();
  };
  const add = (text) => {
    // Pasting a comma-separated list should not become one giant entry.
    const parts = text
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    let added = false;
    for (const part of parts) {
      if (!values.includes(part)) {
        values.push(part);
        added = true;
      }
    }
    if (added) commit();
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      add(input.value);
      input.value = '';
    } else if (e.key === 'Backspace' && !input.value && values.length) {
      values.pop();
      commit();
    }
  };
  input.onblur = () => {
    if (input.value.trim()) {
      add(input.value);
      input.value = '';
    }
  };
  box.onclick = (e) => {
    if (e.target === box || e.target === list) input.focus();
  };
  redraw();
  box.append(list, input);
  return box;
}

function ratingControl(path, value, fieldSpec) {
  const box = document.createElement('div');
  box.className = 'rating';
  box.tabIndex = -1;
  const current = value ?? fieldSpec.default ?? 4;
  const caption = document.createElement('div');
  caption.className = 'rating-caption muted';
  const buttons = [];
  for (let n = 1; n <= 5; n += 1) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = String(n);
    b.onclick = () => {
      setIn(draft, path, n);
      select(n);
      markDirty();
    };
    buttons.push(b);
    box.appendChild(b);
  }
  const select = (n) => {
    buttons.forEach((b, i) => b.classList.toggle('on', i + 1 === n));
    if (fieldSpec.captions) caption.textContent = fieldSpec.captions[n - 1] || '';
  };
  select(current);
  box.appendChild(caption);
  return box;
}

function topicControl(path, value) {
  const box = document.createElement('div');
  box.className = 'topic';
  const input = document.createElement('input');
  input.type = 'text';
  input.value = value ?? '';
  input.oninput = () => {
    setIn(draft, path, input.value || undefined);
    markDirty();
  };
  const generate = document.createElement('button');
  generate.type = 'button';
  generate.textContent = 'Generate a random name';
  generate.onclick = () => {
    input.value = state.starter_topic || 'deal-radar-' + Math.random().toString(16).slice(2, 12);
    input.dispatchEvent(new Event('input'));
  };
  const test = document.createElement('button');
  test.type = 'button';
  test.textContent = 'Send a test alert';
  const status = document.createElement('span');
  status.className = 'muted';
  status.setAttribute('role', 'status');
  test.onclick = async () => {
    status.textContent = 'sending…';
    status.className = 'muted';
    if (dirty) {
      status.textContent = 'Save first, then send a test.';
      status.className = 'err';
      return;
    }
    try {
      const r = await api('/api/setup/test-notify', { method: 'POST', body: {} });
      status.textContent = r.message;
      status.className = 'ok';
    } catch (err) {
      status.textContent = err.message;
      status.className = 'err';
    }
  };
  const buttons = document.createElement('div');
  buttons.className = 'topic-actions';
  buttons.append(generate, test, status);
  box.append(input, buttons);
  // The path lives on the inner input so error mapping finds a focusable node.
  input.dataset.loc = path.join('.');
  Object.defineProperty(box, 'dataset', { value: input.dataset });
  Object.defineProperty(box, 'id', {
    get: () => input.id,
    set: (v) => {
      input.id = v;
    },
  });
  box.focus = () => input.focus();
  box.classList.add('composite');
  return box;
}

function marketplaceControl(path, values) {
  const box = document.createElement('div');
  box.className = 'checkboxes';
  box.tabIndex = -1;
  const configured = Object.keys(draft.marketplaces || {});
  if (!configured.length) {
    const empty = document.createElement('span');
    empty.className = 'muted';
    empty.textContent = "You haven't set up anywhere to search yet - see “Where to look”.";
    box.appendChild(empty);
    return box;
  }
  for (const name of configured) {
    const label = document.createElement('label');
    label.className = 'checkbox';
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = values.includes(name);
    input.onchange = () => {
      const next = configured.filter(
        (n) => (n === name ? input.checked : values.includes(n)),
      );
      setIn(draft, path, next);
      markDirty();
    };
    label.append(input, document.createTextNode(' ' + name));
    box.appendChild(label);
  }
  return box;
}

/**
 * A per-item override. The control is always shown, starting at the global
 * default, so it can be edited straight away; editing it makes the value this
 * hunt's own. "Reset to default" drops the override so the hunt follows the
 * global setting again (including later changes to it).
 */
function renderOverride(path, fieldSpec, fallback) {
  const wrap = document.createElement('div');
  wrap.className = 'field field-wide override';
  const shown = fallback === true ? 'yes' : fallback === false ? 'no' : fallback;

  const reset = document.createElement('button');
  reset.type = 'button';
  reset.className = 'tiny';
  reset.textContent = 'Reset to default';
  reset.title = `Go back to your default (${shown})`;

  const holder = document.createElement('div');
  const refresh = () => {
    reset.disabled = getIn(draft, path) == null;
  };
  const build = () => {
    holder.textContent = '';
    const field = renderField(path, { ...fieldSpec, default: fallback });
    if (!field) return;
    // Number inputs show an unset value as a blank box with a placeholder;
    // show the real default instead, so it reads as a value you can change.
    const num = field.querySelector('input[type="number"]');
    if (num && num.value === '' && getIn(draft, path) == null && fallback != null) {
      num.value = String(fallback);
    }
    // Put the reset button on the same line as the field's name.
    const head = document.createElement('div');
    head.className = 'override-head';
    const label = field.querySelector(':scope > .field-label');
    if (label) {
      field.insertBefore(head, label);
      head.append(label, reset);
    } else {
      field.prepend(head);
      head.append(reset);
    }
    holder.appendChild(field);
  };
  build();
  refresh();
  // The field's own handlers update the draft first; these bubble up after.
  for (const ev of ['input', 'change', 'click']) holder.addEventListener(ev, refresh);
  reset.onclick = () => {
    setIn(draft, path, undefined);
    build();
    refresh();
    markDirty();
  };
  wrap.append(holder);
  return wrap;
}

// --- panels -------------------------------------------------------------------------

/** Fields flow into as many columns as the width allows. */
function fieldGrid() {
  const grid = document.createElement('div');
  grid.className = 'field-grid';
  return grid;
}

function fieldsFor(group, { advanced }) {
  return spec.fields.filter((f) => f.group === group && Boolean(f.advanced) === advanced);
}

function itemPanel() {
  const panel = document.createElement('div');
  panel.className = 'stack';
  const items = draft.items || (draft.items = []);
  if (!items.length) {
    panel.appendChild(emptyItems());
    return panel;
  }
  items.forEach((item, index) => panel.appendChild(itemCard(item, index)));
  const add = document.createElement('button');
  add.type = 'button';
  add.className = 'add-row';
  add.textContent = '+ Add another thing to hunt for';
  add.onclick = () => {
    items.push({
      name: '',
      marketplaces: Object.keys(draft.marketplaces || { facebook: {} }),
      search_phrases: [],
      description: '',
    });
    markDirty();
    render();
  };
  panel.appendChild(add);
  return panel;
}

function emptyItems() {
  const box = document.createElement('div');
  box.className = 'empty';
  box.innerHTML =
    "<p><b>You're not hunting for anything yet.</b></p>" +
    '<p class="muted">Add one thing you want to find, describe it, and deal-radar ' +
    'will start looking.</p>';
  const add = document.createElement('button');
  add.className = 'primary';
  add.type = 'button';
  add.textContent = 'Set up my first hunt';
  add.onclick = () => {
    draft.items = [
      {
        name: '',
        marketplaces: Object.keys(draft.marketplaces || {}),
        search_phrases: [],
        description: '',
      },
    ];
    markDirty();
    render();
  };
  box.appendChild(add);
  return box;
}

function itemCard(item, index) {
  const card = document.createElement('div');
  card.className = 'card';
  const summary = document.createElement('div');
  summary.className = 'card-head';
  const bits = [
    item.price_min != null || item.price_max != null
      ? `$${item.price_min ?? 0}–${item.price_max ?? '∞'}`
      : 'any price',
    item.location || 'default city',
  ];
  summary.innerHTML =
    `<b>${esc(item.name || '(unnamed)')}</b> ` +
    `<span class="muted">${esc(bits.join(' · '))}</span>` +
    (item.enabled === false ? ' <span class="tag">paused</span>' : '');
  // One hunt needs no heading of its own; several need telling apart.
  if ((draft.items || []).length > 1) card.appendChild(summary);

  const effective =
    (state.effective.items || []).find((e) => e.name === item.name) || {
      min_rating: state.defaults.ai.min_rating,
      negotiate: state.defaults.messaging.negotiate,
      offer_percent: state.defaults.messaging.offer_percent,
    };

  // Fields arrive in spec order, already grouped by their `section`; start a
  // new sub-heading and grid each time the section changes.
  let grid = null;
  let current = null;
  const gridFor = (section) => {
    if (grid && section === current) return grid;
    current = section;
    const sub = document.createElement('div');
    sub.className = 'subsection';
    if (section) {
      const title = document.createElement('h4');
      title.className = 'subsection-title';
      title.textContent = section;
      sub.appendChild(title);
    }
    grid = fieldGrid();
    sub.appendChild(grid);
    card.appendChild(sub);
    return grid;
  };
  for (const fieldSpec of fieldsFor('items', { advanced: false })) {
    const leaf = fieldSpec.path.split('.').slice(2).join('.');
    const path = ['items', index, ...leaf.split('.')];
    if (fieldSpec.overridable) {
      // Messaging overrides are noise when messaging is off.
      if (leaf !== 'min_rating' && !(draft.messaging || {}).enabled) continue;
      gridFor(fieldSpec.section).appendChild(renderOverride(path, fieldSpec, effective[leaf]));
      continue;
    }
    const field = renderField(path, fieldSpec);
    if (field) gridFor(fieldSpec.section).appendChild(field);
  }

  const foot = document.createElement('div');
  foot.className = 'card-foot';
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'danger-quiet';
  remove.textContent = 'Remove this';
  remove.onclick = () => {
    if ((draft.items || []).length <= 1) {
      alert(
        'You need at least one thing to hunt for - otherwise there is nothing to look for.',
      );
      return;
    }
    if (!confirm(`Remove “${item.name}”? Its history of shown listings stays.`)) return;
    draft.items.splice(index, 1);
    markDirty();
    render();
  };
  foot.appendChild(remove);
  card.appendChild(foot);
  return card;
}

/** A plain card around one grid of fields. */
function cardOf(grid) {
  const card = document.createElement('div');
  card.className = 'card';
  card.appendChild(grid);
  return card;
}

function alertPanel() {
  const top = fieldGrid();
  for (const fieldSpec of fieldsFor('alerts', { advanced: false })) {
    if (fieldSpec.path.startsWith('notifiers.')) continue;
    if (fieldSpec.path === 'ai.max_images' && !(draft.ai || {}).analyze_images) continue;
    const field = renderField(fieldSpec.path.split('.'), fieldSpec);
    if (field) top.appendChild(field);
  }
  return cardOf(top);
}

function notifierPanel() {
  const panel = document.createElement('div');
  panel.className = 'stack';
  const notifiers = draft.notifiers || (draft.notifiers = []);
  notifiers.forEach((notifier, index) => {
    const card = document.createElement('div');
    card.className = 'card';
    const kind = notifier.type || 'ntfy';
    const head = document.createElement('div');
    head.className = 'card-head';
    head.innerHTML =
      kind === 'ntfy'
        ? '<b>Phone or desktop push (ntfy)</b>'
        : '<b>Telegram</b> <span class="tag">not working yet</span>';
    card.appendChild(head);
    const grid = fieldGrid();
    for (const fieldSpec of spec.fields.filter(
      (f) => f.path.startsWith(`notifiers.*.${kind}.`) && !f.advanced,
    )) {
      const leaf = fieldSpec.path.split('.').slice(3).join('.');
      if (leaf === 'type') continue;
      const field = renderField(['notifiers', index, leaf], fieldSpec);
      if (field) grid.appendChild(field);
    }
    card.appendChild(grid);
    if (notifiers.length > 1) {
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'danger-quiet';
      remove.textContent = 'Remove';
      remove.onclick = () => {
        notifiers.splice(index, 1);
        markDirty();
        render();
      };
      const foot = document.createElement('div');
      foot.className = 'card-foot';
      foot.appendChild(remove);
      card.appendChild(foot);
    }
    panel.appendChild(card);
  });
  return panel;
}

function marketplacePanel() {
  const panel = document.createElement('div');
  panel.className = 'stack';
  const marketplaces = draft.marketplaces || (draft.marketplaces = {});
  for (const name of Object.keys(marketplaces)) {
    const card = document.createElement('div');
    card.className = 'card';
    const head = document.createElement('div');
    head.className = 'card-head';
    const supported = spec.capabilities.marketplaces.includes(name);
    head.innerHTML =
      `<b>${esc(name === 'facebook' ? 'Facebook Marketplace' : name)}</b>` +
      (supported ? '' : ' <span class="tag">not supported yet</span>');
    card.appendChild(head);
    const grid = fieldGrid();
    for (const fieldSpec of fieldsFor('where', { advanced: false })) {
      const leaf = fieldSpec.path.split('.').slice(2).join('.');
      const field = renderField(['marketplaces', name, leaf], fieldSpec);
      if (field) grid.appendChild(field);
    }
    card.appendChild(grid);
    panel.appendChild(card);
  }
  if (!Object.keys(marketplaces).length) {
    const add = document.createElement('button');
    add.className = 'primary';
    add.type = 'button';
    add.textContent = 'Search Facebook Marketplace';
    add.onclick = () => {
      draft.marketplaces = { facebook: { enabled: true } };
      markDirty();
      render();
    };
    panel.appendChild(add);
  }
  return panel;
}

function plainPanel(group) {
  const panel = fieldGrid();
  for (const fieldSpec of fieldsFor(group, { advanced: false })) {
    if (group === 'messaging' && fieldSpec.path !== 'messaging.enabled') {
      if (!(draft.messaging || {}).enabled) continue;
      if (fieldSpec.path === 'messaging.offer_percent' && !(draft.messaging || {}).negotiate) {
        continue;
      }
    }
    const field = renderField(fieldSpec.path.split('.'), fieldSpec);
    if (field) panel.appendChild(field);
  }
  return cardOf(panel);
}

function advancedPanel() {
  const panel = fieldGrid();
  for (const fieldSpec of spec.fields.filter((f) => f.advanced)) {
    const parts = fieldSpec.path.split('.');
    // Advanced per-item / per-marketplace / per-notifier fields are edited on
    // their own cards, not here.
    if (parts.includes('*')) {
      if (parts[0] === 'marketplaces') {
        for (const name of Object.keys(draft.marketplaces || {})) {
          const field = renderField(['marketplaces', name, ...parts.slice(2)], fieldSpec);
          if (field) panel.appendChild(field);
        }
      } else if (parts[0] === 'notifiers') {
        (draft.notifiers || []).forEach((notifier, index) => {
          if ((notifier.type || 'ntfy') !== parts[2]) return;
          const field = renderField(['notifiers', index, ...parts.slice(3)], fieldSpec);
          if (field) panel.appendChild(field);
        });
      }
      continue;
    }
    const field = renderField(parts, fieldSpec);
    if (field) panel.appendChild(field);
  }
  return panel;
}

const PANELS = {
  items: itemPanel,
  alerts: alertPanel,
  notifications: notifierPanel,
  where: marketplacePanel,
  when: () => plainPanel('when'),
  messaging: () => plainPanel('messaging'),
  advanced: advancedPanel,
};

// --- top level ----------------------------------------------------------------------

function render() {
  const host = $('#configform');
  host.textContent = '';
  if (!state.exists) {
    host.appendChild(firstRunCard());
    return;
  }
  if (state.config === null) {
    host.appendChild(unparseableCard());
    return;
  }
  for (const group of spec.groups) {
    // Each group is a row: its title and description on the left, its card(s)
    // on the right. Only Advanced folds away.
    const section = document.createElement('section');
    section.className = 'group';
    section.id = 'settings-' + group.id;
    const head = document.createElement('div');
    head.className = 'group-head';
    const title = document.createElement('h3');
    title.className = 'group-title';
    title.textContent = group.title;
    head.appendChild(title);
    if (group.blurb) {
      const blurb = document.createElement('p');
      blurb.className = 'group-blurb';
      blurb.textContent = group.blurb;
      head.appendChild(blurb);
    }
    const body = document.createElement('div');
    body.className = 'group-body';
    if (group.id === 'advanced') {
      const details = document.createElement('details');
      details.className = 'card panel';
      const summary = document.createElement('summary');
      summary.textContent = 'Show advanced settings';
      const inner = document.createElement('div');
      inner.className = 'panel-body';
      inner.append(PANELS[group.id](), rawEditor());
      details.append(summary, inner);
      body.appendChild(details);
    } else {
      body.appendChild(PANELS[group.id]());
    }
    section.append(head, body);
    host.appendChild(section);
  }
  runValidate();
}

function firstRunCard() {
  const box = document.createElement('div');
  box.className = 'empty';
  box.innerHTML =
    "<h3>You haven't set anything up yet.</h3>" +
    '<p class="muted">deal-radar needs two things to get going: somewhere to send ' +
    'your alerts, and one thing to hunt for.</p>';
  const start = document.createElement('button');
  start.className = 'primary';
  start.textContent = 'Set up my first hunt';
  start.onclick = () => {
    draft = {
      version: 1,
      ai: { min_rating: 4 },
      scan: { max_evaluations_per_item: 25 },
      marketplaces: { facebook: { enabled: true } },
      notifiers: [{ type: 'ntfy', topic: state.starter_topic }],
      items: [
        {
          name: '',
          marketplaces: ['facebook'],
          search_phrases: [],
          description: '',
        },
      ],
    };
    state.exists = true;
    state.config = draft;
    dirty = true;
    render();
  };
  const paste = document.createElement('button');
  paste.textContent = 'I already have a settings file - paste it';
  paste.onclick = () => {
    state.exists = true;
    state.config = draft = {};
    rawMode = true;
    render();
    $('#rawyaml')?.focus();
  };
  box.append(start, paste);
  return box;
}

function unparseableCard() {
  const box = document.createElement('div');
  box.className = 'empty';
  box.innerHTML =
    '<h3>Your settings file has a formatting problem.</h3>' +
    `<p class="err">${esc((state.errors[0] || {}).msg || '')}</p>` +
    '<p class="muted">The simple view can’t show a file it can’t read. Fix it below, ' +
    'then reload.</p>';
  box.appendChild(rawEditor());
  return box;
}

function rawEditor() {
  const box = document.createElement('div');
  box.className = 'raw';
  const note = document.createElement('p');
  note.className = 'muted';
  note.textContent =
    'The raw settings file.';
  const area = document.createElement('textarea');
  area.id = 'rawyaml';
  area.spellcheck = false;
  const save = document.createElement('button');
  save.className = 'primary';
  save.type = 'button';
  save.textContent = 'Save the file';
  const status = document.createElement('span');
  status.className = 'muted';

  (async () => {
    try {
      const r = await fetch('/api/config');
      area.value = await r.text();
    } catch {
      /* offline banner covers it */
    }
  })();

  save.onclick = async () => {
    status.textContent = 'saving…';
    status.className = 'muted';
    try {
      await api('/api/config', { method: 'POST', body: { text: area.value } });
      status.textContent = 'saved ✓';
      status.className = 'ok';
      await load();
    } catch (err) {
      status.textContent = err.message;
      status.className = 'err';
    }
  };
  const actions = document.createElement('div');
  actions.className = 'raw-actions';
  actions.append(save, status);
  box.append(note, area, actions);
  return box;
}

async function save() {
  const status = $('#formstatus');
  status.textContent = 'saving…';
  status.className = 'muted';
  try {
    const result = await api('/api/config', {
      method: 'PUT',
      body: { etag, config: draft },
    });
    dirty = false;
    etag = result.etag;
    // Reload first: load() resets the status line, so setting it before would
    // wipe the confirmation and leave the user unsure whether it saved.
    await load();
    status.textContent =
      result.changed.length
        ? `saved ✓ - ${result.changed.length} setting(s) changed, applies on the next scan`
        : 'nothing to save';
    status.className = 'ok';
    $('#formsave').disabled = true;
  } catch (err) {
    if (err.status === 409) {
      status.textContent = err.message;
      status.className = 'err';
      alert(err.message + '\n\nReloading now - your unsaved changes are in the box below.');
      await load();
      return;
    }
    status.textContent = 'not saved - see the problems listed above';
    status.className = 'err';
    if (err.data && err.data.errors) showErrors(err.data.errors, err.data.warnings || []);
  }
}

async function load() {
  if (!spec) spec = await api('/api/config/formspec');
  for (const field of spec.fields) byPath.set(field.path, field);
  state = await api('/api/config/form');
  draft = state.config ? clone(state.config) : {};
  etag = state.etag;
  dirty = false;
  render();
  $('#formsave').disabled = true;
  $('#formstatus').textContent = state.exists ? '' : 'not set up yet';
  if (state.errors && state.errors.length) showErrors(state.errors, state.warnings || []);
}

export async function initConfigForm() {
  $('#formsave').addEventListener('click', save);
  $('#formreload').addEventListener('click', async () => {
    if (dirty && !confirm('Discard your unsaved changes?')) return;
    await load();
  });
  window.addEventListener('beforeunload', (e) => {
    if (dirty) e.preventDefault();
  });
  await load();
}
