# deal-radar

A personal, config-driven marketplace monitor with AI listing evaluation.

deal-radar watches online marketplace listings for items you define, uses the
Claude API to judge whether each new listing actually matches what you want and
whether it's a good deal, and pings you (via [ntfy](https://ntfy.sh)) when
something good shows up. It generalizes across categories — you configure what
you're hunting for in a YAML file.

> **Personal, hobbyist use only.** Automated collection generally breaches
> marketplace terms, so deal-radar is designed for low volume and politeness:
> conservative poll intervals, rate limiting, jitter, and a single logged-in
> account. It does not evade bot-detection beyond behaving like a slow, normal
> human user. No credentials or tokens are stored in the repo.

The open-source AGPL project `ai-marketplace-monitor` was used only as
conceptual reference; none of its code is used here.

## Status

Working end to end on Facebook Marketplace: scrape → parse → dedup → optional
detail-page fetch → Claude evaluation → ntfy notification, plus a polling loop.
See the Roadmap below for what's done.

## Quickstart

```bash
# 1. install (editable, with dev tools)
pip install -e ".[dev]"          # or: uv pip install -e ".[dev]"
playwright install chromium      # one-time browser download

# 2. config + secrets
cp config.example.yaml config.yaml   # then edit what you're hunting for
cp .env.example .env                 # then add your ANTHROPIC_API_KEY

# 3. check the config parses
deal-radar validate-config

# 4. log in to Facebook once (saves a browser session)
deal-radar login facebook

# 5. a single cheap test scan (1 AI eval per item), watching the browser
deal-radar run-once --headful --max-evals 1 --log-level DEBUG

# 6. once happy, run the polling loop until you stop it (Ctrl-C)
deal-radar run
```

`deal-radar` is the installed console script; `python -m deal_radar …` is
equivalent. All commands take `--config PATH` (default `config.yaml`).

## Command-line reference

### Commands

| Command | What it does |
|---|---|
| `setup` | One-time: download the Chromium build deal-radar drives. Run this before `serve`. |
| `doctor` | Check whether deal-radar is ready to scan — settings, browser, API key, Facebook sign-in, alerts — and say what to do about anything that isn't. Same checks the web UI's setup screen shows. Exits non-zero if a scan couldn't work. |
| `test-notify` | Send one obviously-fake alert, to prove notifications actually reach your phone. |
| `validate-config` | Parse and validate the config, print a summary, exit. No network. |
| `login [marketplace]` | Open a browser for a one-time manual login; saves the session for later runs. Defaults to `facebook`. |
| `run-once` | Run **one** full scan pass over your items, then exit. |
| `run` | Run the **polling loop** — repeat `run-once`'s work on the configured interval (with jitter + rate limiting) until you press Ctrl-C. |
| `serve` | Launch the **local web UI** (config editor, live logs, scanner start/stop). |
| `list-seen` | Print listings already recorded in the local SQLite "seen" store (so you don't get re-notified). |

### Flags for `run-once` and `run`

| Flag | Default | What it does |
|---|---|---|
| `--item SUBSTR` | all items | Only scan items whose name **contains** `SUBSTR` (case-insensitive). **Repeatable**: `--item pc --item bike` scans both. Omit to scan everything. An unknown value errors and lists the available names. |
| `--limit N` | `scan.max_listings_per_search` (200) | Max listings to collect per marketplace (caps how many search results are scraped). Overrides the config. |
| `--max-evals N` | `scan.max_evaluations_per_item` (25) | Max **AI evaluations per item, per scan**. Each evaluation is one Claude API call = real spend, so this is your cost cap. `--max-evals 0` short-circuits before any API call *or detail-page fetch* — a free scrape-only mode. Overrides the config. |
| `--dry-run` | off | Still evaluates listings, but **does not send notifications**. This does *not* save money — the Claude call still happens; only the ntfy push is suppressed. Use `--max-evals` to bound cost. |
| `--headful` | off (headless) | Show the Chromium window so you can watch it work (useful for debugging selectors/login). |
| `--max-cycles N` (`run` only) | unlimited | Stop after N loop cycles instead of running until Ctrl-C. Mainly for testing. |

The loop's cadence (`run`) comes from the `schedule:` block in your config —
`poll_interval_seconds`, `jitter_seconds`, and `per_request_min_interval_seconds`
(the polite gap between page loads) — not from flags.

### Global flags (any command)

| Flag | What it does |
|---|---|
| `--log-level DEBUG\|INFO\|WARNING\|ERROR` | Logging verbosity (default `INFO`). Accepted **before or after** the command. `DEBUG` adds per-card parse lines (`card id=… raw=…`) and detail-page extraction (`detail id=… text[…]`) — useful for tuning selectors and keyword filters. |
| `--config PATH` | Path to the YAML config (default `config.yaml`). |
| `--version` | Print the version and exit. |

### Worked example

```bash
deal-radar run-once --headful --max-evals 1 --log-level DEBUG
```

- **`run-once`** — do a single scan pass over all enabled items, then exit (not the loop).
- **`--headful`** — show the browser window so you can watch the scrape happen.
- **`--max-evals 1`** — send at most one listing per item to Claude. With two
  items that's ≤ 2 API calls — a fraction of a cent — so it's a safe, cheap probe.
- **`--log-level DEBUG`** — verbose output, including each parsed card and the
  text pulled from each detail page.

### Flags for `list-seen`

| Flag | Default | What it does |
|---|---|---|
| `--item NAME` | all | Filter recorded listings to one item by name. |
| `--limit N` | `50` | Max rows to print. |

## Web UI

Run the local web server with `.venv/bin/deal-radar serve`, then open http://127.0.0.1:8000:

```bash
.venv/bin/deal-radar serve
```

The page is, in order: **Deals worth a look** (auto-loaded, ranked by match then
score then price), **Messages waiting for you** (hidden unless messaging is on),
**Everything checked recently** (the ten most recent, expandable), **Settings**,
and **Activity** — a collapsed log with an "only show problems" filter. Scan
controls and the cost estimate live in the header, always visible.

**Settings.** The page edits your config through a guided form — every setting
with a plain-language label and an explanation, grouped into *What I'm hunting
for*, *How you get alerted*, *Where to look*, *How often to check*, and
*Messaging sellers*. Saving writes a **minimal patch**, so your comments, block
scalars, key order and indentation all survive; an untouched save is
byte-identical. The raw YAML editor is still there under **Advanced** — nothing
you could do before is gone, and a test asserts the form covers every setting in
the schema.

**First run.** If anything essential is missing, the page opens on a setup screen
instead of the control panel: it names each problem in plain language and, where
it can, fixes it in place — paste an Anthropic API key (saved to `.env` next to
your config, `0600`, already gitignored), sign in to Facebook in a browser window
it opens for you, send a test alert, and check whether a saved Facebook sign-in
still works. That last one matters: an expired sign-in otherwise makes a scan
finish looking clean with zero results.

Downloading the browser is the one thing the UI can't do for you — on Linux it
often needs `playwright install-deps` and root — so it shows the command to run.
`deal-radar setup` does it, and `deal-radar doctor` prints the same checks in a
terminal.

> The server has no password. It listens on `127.0.0.1` by default, which is
> the right thing; if you pass `--host`, anyone who can reach that address can
> read your settings, start scans that cost money, and message sellers as you.

## Messaging sellers (off by default)

> **Warning.** Automated messaging can violate Facebook's Terms of Service and
> may get your account restricted or banned. This feature is for low-volume
> personal use only, and it never sends anything without your explicit approval.

With `messaging.enabled: true` in the config, each match also produces a *draft*
first message to the seller (written by Claude around a price you control). The
draft appears in the web UI's **Message drafts** section, where you can edit the
text, then **Approve & send** (a browser opens the listing with your saved
session and sends it) or **Dismiss**. Guarantees:

- Nothing is ever sent automatically — every message requires a click.
- At most one message per listing, ever (enforced in the database).
- With `negotiate: false` the draft just asks about availability at asking price.
  With `negotiate: true` it includes an opening offer: `offer_percent` of the
  asking price, rounded to the nearest $5, never above asking. Both settings can
  be overridden per item.
- Opening offer only — deal-radar never reads or replies to the seller's
  response. Continue the conversation yourself in Messenger.

## Cost & safety notes

- **Every AI evaluation costs money.** It's one Claude API call per *new* listing
  that passes the cheap filters (price + exclude-keywords), capped by
  `scan.max_evaluations_per_item` (or `--max-evals`).
- **The web UI shows the cost before you click**, computed from your model, your
  item count and your cap — and a running "$0.014 this scan" counter while it
  works. Under Advanced there's a **Test scan (free)**, which opens the
  marketplace and runs one search without asking the AI anything: it proves the
  browser and your Facebook sign-in work, and costs nothing.
- **Scans are slow on purpose** — page loads are paced ~25s apart and each
  candidate costs a second load, so 25 candidates is a quarter of an hour. The UI
  shows a real progress bar ("Asking the AI about a listing (1 of 2: Gaming PC).
  Checked 7 of 25, 1 worth a look so far", "about 12 minutes left") rather than
  leaving you unable to tell working from wedged.
  Already-seen listings are skipped, so steady-state cost is just newly-appeared
  listings. With the default `claude-haiku-4-5` model each eval is well under a
  cent; the per-eval cost is logged (`eval usage: … est_cost=$…`).
- **`--dry-run` ≠ free.** It only suppresses the push notification.
- **Politeness.** Keep `schedule.poll_interval_seconds` conservative; the tool is
  for low-volume personal use, not bulk scraping.

## Roadmap

- **Phase 0 — Scaffold** (done): config, loader, models, interfaces, logging, CLI skeleton, tests.
- **Phase 1 — MVP** (done): Facebook adapter (Playwright), SQLite dedup, Claude evaluator, ntfy notifier, `run-once`.
- **Phase 2 — Scheduling** (done): poll loop with interval, jitter, rate limiting; multi-item; per-eval usage/cost logging.
- **Detail-page fetch** (done): enrich each candidate with its full listing body before AI evaluation.
- **Phase 5 — Web UI** (done): local FastAPI control panel — config editor, live logs, scanner start/stop.
- **Phase 3 — Images** (skipped for now): optional photo analysis in the evaluator.
- **Phase 4 — Pluggability** (skipped for now): second notifier + second marketplace adapter.
