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
from ..config.loader import load_config
from ..config.schema import MarketplaceConfig
from ..dedup.sqlite_store import SqliteSeenStore
from ..logging import get_logger
from ..marketplaces.base import Marketplace
from ..marketplaces.registry import build_marketplace
from ..messaging.drafter import open_drafter
from ..notifiers.registry import build_notifier
from ..pipeline import scan_all
from ..ratelimit import RateLimiter
from ..scheduler import run_loop
from .controller import Job

log = get_logger("web.runner")


def build_jobs(
    config_path: str,
    *,
    headless: bool = True,
    limit: int = 40,
    max_evals: int = 25,
    dry_run: bool = False,
) -> tuple[Job, Job]:
    """Return (run_loop_job, run_once_job) bound to a config path, for the controller."""

    def _run(stop: threading.Event, *, loop: bool) -> None:
        cfg = load_config(config_path)  # reload so UI edits apply on restart
        paths.ensure_data_dir()
        evaluator = ClaudeEvaluator(cfg.ai)
        notifiers = [build_notifier(n) for n in cfg.notifiers]
        interval = cfg.schedule.per_request_min_interval_seconds
        pause = RateLimiter(interval, interval * 0.5)
        items = [item for item in cfg.items if item.enabled]

        def make_mk(name: str, mk_cfg: MarketplaceConfig) -> Marketplace:
            return build_marketplace(
                name, mk_cfg, headless=headless, max_results=limit, pause=pause
            )

        def _sleep(delay: float) -> None:
            stop.wait(delay)  # interruptible: returns early when stop is set

        with SqliteSeenStore(paths.db_path()) as store, open_drafter(cfg) as drafter:

            def scan() -> None:
                scan_all(
                    cfg=cfg,
                    items=items,
                    make_marketplace=make_mk,
                    evaluator=evaluator,
                    store=store,
                    notifiers=notifiers,
                    drafter=drafter,
                    max_evaluations=max_evals,
                    dry_run=dry_run,
                    should_stop=stop.is_set,
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
