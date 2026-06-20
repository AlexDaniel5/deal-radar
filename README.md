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

Early development. **Phase 0 (scaffold)** is in place: packaging, config schema
and loader, domain models, base interfaces, logging, and a working
`validate-config` CLI command. Live scraping, AI evaluation, and notifications
arrive in later phases.

## Quickstart (Phase 0)

```bash
# install (editable, with dev tools)
pip install -e ".[dev]"          # or: uv pip install -e ".[dev]"

# validate your config
deal-radar validate-config --config config.yaml
# equivalently:
python -m deal_radar validate-config --config config.yaml
```

Copy `config.example.yaml` to `config.yaml` (gitignored) and edit it, and copy
`.env.example` to `.env` for your `ANTHROPIC_API_KEY`.

## Roadmap

- **Phase 0 — Scaffold** (done): config, loader, models, interfaces, logging, CLI skeleton, tests.
- **Phase 1 — MVP**: Facebook adapter (Playwright), SQLite dedup, Claude evaluator, ntfy notifier, `run-once`.
- **Phase 2 — Scheduling**: poll loop with interval, jitter, rate limiting; multi-item.
- **Phase 3 — Images**: optional photo analysis in the evaluator.
- **Phase 4 — Pluggability**: second notifier + second marketplace adapter.
- **Phase 5 — (optional)** local web UI.
