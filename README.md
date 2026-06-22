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
| `validate-config` | Parse and validate the config, print a summary, exit. No network. |
| `login [marketplace]` | Open a browser for a one-time manual login; saves the session for later runs. Defaults to `facebook`. |
| `run-once` | Run **one** full scan pass over your items, then exit. |
| `run` | Run the **polling loop** — repeat `run-once`'s work on the configured interval (with jitter + rate limiting) until you press Ctrl-C. |
| `list-seen` | Print listings already recorded in the local SQLite "seen" store (so you don't get re-notified). |

### Flags for `run-once` and `run`

| Flag | Default | What it does |
|---|---|---|
| `--item SUBSTR` | all items | Only scan items whose name **contains** `SUBSTR` (case-insensitive). **Repeatable**: `--item pc --item bike` scans both. Omit to scan everything. An unknown value errors and lists the available names. |
| `--limit N` | `40` | Max listings to collect per marketplace (caps how many search results are scraped). |
| `--max-evals N` | `25` | Max **AI evaluations per item, per scan**. Each evaluation is one Claude API call = real spend, so this is your cost cap. `--max-evals 0` short-circuits before any API call — a free scrape-only mode. |
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

## Cost & safety notes

- **Every AI evaluation costs money.** It's one Claude API call per *new* listing
  that passes the cheap filters (price + exclude-keywords), capped by `--max-evals`.
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
- **Phase 3 — Images**: optional photo analysis in the evaluator.
- **Phase 4 — Pluggability**: second notifier + second marketplace adapter.
- **Phase 5 — (optional)** local web UI.
