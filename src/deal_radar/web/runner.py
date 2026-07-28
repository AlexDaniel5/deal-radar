"""Production scanner jobs for the web controller.

Builds the same runtime as ``deal-radar run`` (evaluator, notifiers, dedup store,
marketplace factory) and drives it via ``scan_all`` / ``run_loop``, wired to a
stop Event for cooperative cancellation. Config is reloaded on each job start, so
edits made in the web UI take effect the next time the scanner starts.
"""

from __future__ import annotations

import threading

from .. import paths
from ..ai.claude import ClaudeEvaluator
from ..ai.pricing import METER
from ..config.loader import load_config
from ..config.schema import MarketplaceConfig
from ..dedup.sqlite_store import SqliteSeenStore
from ..logging import get_logger
from ..marketplaces.base import Marketplace
from ..marketplaces.registry import build_marketplace
from ..messaging.drafter import open_drafter
from ..notifiers.registry import build_notifier
from ..pipeline import ScanStats, format_stats, scan_all
from ..ratelimit import RateLimiter
from ..scheduler import run_loop
from .controller import Job

log = get_logger("web.runner")


def build_jobs(
    config_path: str,
    *,
    headless: bool = True,
    limit: int | None = None,
    max_evals: int | None = None,
    dry_run: bool = False,
) -> tuple[Job, Job]:
    """Return (run_loop_job, run_once_job) bound to a config path, for the controller.

    ``limit`` and ``max_evals`` are *overrides*: left as None they come from the
    config's ``scan:`` block, which is read at job start so the value applies to
    the polling loop as well as a one-off scan.
    """

    def _run(stop: threading.Event, *, loop: bool) -> None:
        cfg = load_config(config_path)  # reload so UI edits apply on restart
        paths.ensure_data_dir()
        effective_limit = limit if limit is not None else cfg.scan.max_listings_per_search
        effective_max_evals = (
            max_evals if max_evals is not None else cfg.scan.max_evaluations_per_item
        )
        evaluator = ClaudeEvaluator(cfg.ai)
        notifiers = [build_notifier(n) for n in cfg.notifiers]
        interval = cfg.schedule.per_request_min_interval_seconds
        pause = RateLimiter(interval, interval * 0.5)
        items = [item for item in cfg.items if item.enabled]

        def make_mk(name: str, mk_cfg: MarketplaceConfig) -> Marketplace:
            return build_marketplace(
                name, mk_cfg, headless=headless, max_results=effective_limit, pause=pause
            )

        def _sleep(delay: float) -> None:
            stop.wait(delay)  # interruptible: returns early when stop is set

        with SqliteSeenStore(paths.db_path()) as store, open_drafter(cfg) as drafter:

            def scan() -> None:
                # Reset per-scan spend here, not once per job: a polling loop
                # runs many scans and the UI shows "spent this scan".
                METER.start_scan()
                collected: list[ScanStats] = []

                def on_stats(s: ScanStats) -> None:
                    collected.append(s)
                    log.info("%s", format_stats(s))

                scan_all(
                    cfg=cfg,
                    items=items,
                    make_marketplace=make_mk,
                    evaluator=evaluator,
                    store=store,
                    notifiers=notifiers,
                    drafter=drafter,
                    max_evaluations=effective_max_evals,
                    dry_run=dry_run,
                    on_stats=on_stats,
                    should_stop=stop.is_set,
                )
                # Everything found was already in the seen store: nothing new to judge.
                if collected and all(s.evaluated == 0 and s.found > 0 for s in collected):
                    total = sum(s.found for s in collected)
                    log.info(
                        "all current listings already scanned — nothing new this run "
                        "(%d listings, all already seen)",
                        total,
                    )

            if loop:
                run_loop(scan=scan, schedule=cfg.schedule, sleep=_sleep, should_stop=stop.is_set)
            else:
                log.info("manual scan starting")
                scan()
                log.info("manual scan complete")

    def run_loop_job(stop: threading.Event) -> None:
        _run(stop, loop=True)

    def run_once_job(stop: threading.Event) -> None:
        _run(stop, loop=False)

    return run_loop_job, run_once_job


def build_free_scan_job(config_path: str) -> Job:
    """A scan that costs nothing: no AI calls, no detail-page fetches.

    ``max_evaluations=0`` short-circuits the pipeline before either. It is the
    right first thing for a new user to press — it proves the browser works and
    the Facebook sign-in is live, and it cannot spend a cent.
    """
    _, once = build_jobs(config_path, max_evals=0)

    def job(stop: threading.Event) -> None:
        log.info("free test scan starting — no AI calls will be made")
        once(stop)
        log.info("free test scan complete (nothing was charged)")

    return job
