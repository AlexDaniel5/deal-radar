"""Command-line interface for deal-radar."""

from __future__ import annotations

import argparse
import signal
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from types import FrameType
from typing import TYPE_CHECKING

from . import __version__, paths
from .config.loader import load_config, load_dotenv_if_present
from .config.schema import AppConfig, ItemConfig, MarketplaceConfig
from .errors import ConfigError, DealRadarError
from .logging import configure_logging, get_logger
from .pipeline import ScanStats, format_stats

if TYPE_CHECKING:
    from .marketplaces.base import Marketplace

log = get_logger("cli")


@contextmanager
def _graceful_sigint() -> Iterator[None]:
    """Make Ctrl-C shut a Playwright run down cleanly, but always killable.

    First Ctrl-C raises KeyboardInterrupt to break the loop (clean teardown).
    A second Ctrl-C restores Python's default handler and force-quits, so the
    process can never get stuck if the first interrupt is swallowed mid-call or
    browser teardown hangs.
    """
    state = {"count": 0}

    def handler(signum: int, frame: FrameType | None) -> None:
        state["count"] += 1
        if state["count"] == 1:
            print("\nstopping… (press Ctrl-C again to force quit)", flush=True)
            raise KeyboardInterrupt
        # Second+ press: hand back to the default handler and force the interrupt.
        signal.signal(signal.SIGINT, signal.default_int_handler)
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:  # not in the main thread; can't install a handler
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _print_stats(stats: ScanStats) -> None:
    print(f"  {format_stats(stats)}")


def _select_items(cfg: AppConfig, patterns: Sequence[str] | None) -> list[ItemConfig]:
    """Pick enabled items by case-insensitive name substring (e.g. 'pc', 'bike').

    No patterns -> all enabled items. Each pattern must match at least one enabled
    item, so a typo is reported rather than silently scanning nothing.
    """
    enabled = [i for i in cfg.items if i.enabled]
    if not patterns:
        return enabled

    selected: list[ItemConfig] = []
    chosen: set[str] = set()
    for pattern in patterns:
        matches = [i for i in enabled if pattern.lower() in i.name.lower()]
        if not matches:
            available = ", ".join(i.name for i in enabled) or "(none)"
            raise ConfigError(f"no enabled item matches {pattern!r}; available: {available}")
        for item in matches:
            if item.name not in chosen:
                chosen.add(item.name)
                selected.append(item)
    return selected


def _marketplace_factory(
    cfg: AppConfig, *, headless: bool, max_results: int
) -> Callable[[str, MarketplaceConfig], Marketplace]:
    """A ``(name, marketplace_config) -> Marketplace`` builder with config-driven pacing."""
    from .marketplaces.registry import build_marketplace
    from .ratelimit import RateLimiter

    interval = cfg.schedule.per_request_min_interval_seconds
    # Shared pacer across passes; jitter avoids perfectly periodic page loads.
    pause = RateLimiter(interval, interval * 0.5)

    def make(name: str, mk_cfg: MarketplaceConfig) -> Marketplace:
        return build_marketplace(
            name, mk_cfg, headless=headless, max_results=max_results, pause=pause
        )

    return make


def _scan_limits(cfg: AppConfig, args: argparse.Namespace) -> tuple[int, int]:
    """(max_listings, max_evaluations) — CLI flags override the config's ``scan:`` block."""
    limit = args.limit if args.limit is not None else cfg.scan.max_listings_per_search
    max_evals = (
        args.max_evals if args.max_evals is not None else cfg.scan.max_evaluations_per_item
    )
    return limit, max_evals


def _cmd_validate(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    print(f"OK: {args.config}")
    print(f"  version: {cfg.version}")
    print(
        f"  ai: model={cfg.ai.model} min_rating={cfg.ai.min_rating} "
        f"analyze_images={cfg.ai.analyze_images}"
    )
    if cfg.marketplaces:
        mks = ", ".join(
            f"{name}{'' if mk.enabled else ' (disabled)'}"
            f"{'' if mk.fetch_details else ' [no detail fetch]'}"
            for name, mk in cfg.marketplaces.items()
        )
    else:
        mks = "(none)"
    print(f"  marketplaces: {mks}")
    msg = cfg.messaging
    print(
        f"  messaging: {'enabled' if msg.enabled else 'disabled'} "
        f"negotiate={msg.negotiate} offer_percent={msg.offer_percent}"
    )
    print(f"  notifiers: {', '.join(n.type for n in cfg.notifiers)}")
    enabled = sum(1 for i in cfg.items if i.enabled)
    print(f"  items: {len(cfg.items)} ({enabled} enabled)")
    for item in cfg.items:
        flag = "" if item.enabled else " (disabled)"
        print(
            f"    - {item.name}{flag}: phrases={len(item.search_phrases)} "
            f"markets={item.marketplaces} min_rating={item.effective_min_rating(cfg.ai)}"
        )
    return 0


def _cmd_run_once(args: argparse.Namespace) -> int:
    # Imports are local so `validate-config`/`list-seen` don't pull heavy deps.
    from .ai.claude import ClaudeEvaluator
    from .dedup.sqlite_store import SqliteSeenStore
    from .messaging.drafter import open_drafter
    from .notifiers.registry import build_notifier
    from .pipeline import scan_all

    cfg = load_config(args.config)
    items = _select_items(cfg, args.item)
    paths.ensure_data_dir()

    limit, max_evals = _scan_limits(cfg, args)
    evaluator = ClaudeEvaluator(cfg.ai)
    notifiers = [build_notifier(n) for n in cfg.notifiers]
    make_marketplace = _marketplace_factory(cfg, headless=not args.headful, max_results=limit)

    print(f"run-once: {len(items)} item(s), dry_run={args.dry_run}")
    with (
        _graceful_sigint(),
        SqliteSeenStore(paths.db_path()) as store,
        open_drafter(cfg) as drafter,
    ):
        results = scan_all(
            cfg=cfg,
            items=items,
            make_marketplace=make_marketplace,
            evaluator=evaluator,
            store=store,
            notifiers=notifiers,
            drafter=drafter,
            max_evaluations=max_evals,
            dry_run=args.dry_run,
            on_stats=_print_stats,
        )
    if not results:
        print("nothing to do: no enabled marketplaces referenced by selected items")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    # Imports are local so `validate-config`/`list-seen` don't pull heavy deps.
    from .ai.claude import ClaudeEvaluator
    from .dedup.sqlite_store import SqliteSeenStore
    from .messaging.drafter import open_drafter
    from .notifiers.registry import build_notifier
    from .pipeline import scan_all
    from .scheduler import run_loop

    cfg = load_config(args.config)
    items = _select_items(cfg, args.item)
    paths.ensure_data_dir()

    limit, max_evals = _scan_limits(cfg, args)
    evaluator = ClaudeEvaluator(cfg.ai)
    notifiers = [build_notifier(n) for n in cfg.notifiers]
    make_marketplace = _marketplace_factory(cfg, headless=not args.headful, max_results=limit)

    sched = cfg.schedule
    print(
        f"run: {len(items)} item(s) every ~{sched.poll_interval_seconds}s "
        f"(+/-{sched.jitter_seconds}s jitter), dry_run={args.dry_run}. Ctrl-C to stop."
    )
    with (
        _graceful_sigint(),
        SqliteSeenStore(paths.db_path()) as store,
        open_drafter(cfg) as drafter,
    ):

        def scan() -> None:
            scan_all(
                cfg=cfg,
                items=items,
                make_marketplace=make_marketplace,
                evaluator=evaluator,
                store=store,
                notifiers=notifiers,
                drafter=drafter,
                max_evaluations=max_evals,
                dry_run=args.dry_run,
                on_stats=_print_stats,
            )

        cycles = run_loop(scan=scan, schedule=sched, max_cycles=args.max_cycles)
    print(f"stopped after {cycles} cycle(s)")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError as exc:
        raise DealRadarError(
            "web UI needs extra deps; install them with: pip install -e '.[web]'"
        ) from exc

    # Deliberately do NOT fail fast on a missing or broken config: fixing it is
    # the web UI's job now. Refusing to start would leave a first-time user with
    # no way in — the setup screen and the settings form both exist precisely
    # for the case where there is nothing valid on disk yet.
    try:
        load_config(args.config)
    except DealRadarError as exc:
        print(f"note: {exc}")
        print("      Starting anyway — set things up in the browser.")

    app = create_app(config_path=args.config)
    print(f"deal-radar web UI at http://{args.host}:{args.port}  (config: {args.config})")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"  WARNING: listening on {args.host}, not just this computer. There is no "
            "password — anyone who can reach this address can read your settings, "
            "start scans that cost money, and message sellers as you."
        )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        # The live-log stream holds a connection open for as long as the page
        # is in a browser, and a graceful shutdown waits for open connections —
        # so without a bound, Ctrl-C simply never returns while the UI is open.
        timeout_graceful_shutdown=3,
    )
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    from .marketplaces.facebook import capture_session

    cfg = load_config(args.config)
    name = args.marketplace
    mk_cfg = cfg.marketplaces.get(name)
    if mk_cfg is None:
        raise ConfigError(f"marketplace {name!r} is not configured")
    if name != "facebook":
        raise ConfigError(f"login is only implemented for 'facebook' (got {name!r})")

    def wait_for_login() -> None:
        input(
            "\nA browser window has opened. Log in to Facebook, then press Enter here "
            "to save the session... "
        )

    path = capture_session(mk_cfg, wait_for_login=wait_for_login)
    print(f"saved session: {path}")
    return 0


_STATE_MARK = {"ok": "[ ok ]", "warn": "[warn]", "fail": "[FAIL]", "unknown": "[ ?  ]"}


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Print the same readiness checks the web wizard shows.

    This is what a technical helper reaches for when someone says "it isn't
    working" — same source of truth as the browser, no browser required.
    """
    from .web import preflight
    from .web.setup import read_state

    state = read_state()
    checks = preflight.all_checks(
        args.config,
        key_verified_ts=state.get("key_verified_ts"),
        facebook_checked=(
            {"ok": state.get("fb_checked_ok"), "ts": state.get("fb_checked_ts")}
            if state.get("fb_checked_ts")
            else None
        ),
    )
    for check in checks:
        print(f"{_STATE_MARK.get(check.state, '[ ?  ]')} {check.label}: {check.detail}")
        if check.fix:
            print(f"        → {check.fix}")
        if check.copyable:
            print(f"          {check.copyable}")
    blocking = [c for c in checks if c.blocking and c.state == "fail"]
    print()
    if blocking:
        print(f"{len(blocking)} thing(s) must be fixed before a scan can work.")
        return 1
    print("Ready to scan.")
    return 0


def _cmd_test_notify(args: argparse.Namespace) -> int:
    """Send one obviously-fake alert, to prove notifications actually arrive."""
    from .web.setup import SetupError, send_test_notification

    cfg = load_config(args.config)
    try:
        result = send_test_notification(cfg)
    except SetupError as exc:
        raise DealRadarError(str(exc)) from exc
    print(result["message"])
    for warning in result.get("warnings", []):
        print(f"  note: {warning}")
    return 0


def _cmd_setup(args: argparse.Namespace) -> int:
    """One-time install of the browser deal-radar drives.

    Deliberately a CLI command rather than a button in the web UI: on Linux
    this often needs `playwright install-deps` too, which needs sudo, so a
    one-click promise in the browser would break exactly where the user
    couldn't recover.
    """
    import subprocess
    import sys

    print("Downloading the browser deal-radar uses (about 150 MB, one time)…")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "playwright", "install", "chromium"], check=False
    )
    if result.returncode != 0:
        print(
            "\nThat didn't work. On Linux you may also need:\n"
            f"  {sys.executable} -m playwright install-deps chromium\n"
            "which usually needs sudo."
        )
        return result.returncode
    print("\nDone. Now run:  deal-radar serve")
    return 0


def _cmd_list_seen(args: argparse.Namespace) -> int:
    from .dedup.sqlite_store import SqliteSeenStore

    db = paths.db_path()
    if not db.is_file():
        print(f"no seen store yet at {db}")
        return 0
    with SqliteSeenStore(db) as store:
        rows = store.list_seen(args.item)
    if not rows:
        print("(no listings recorded)")
        return 0
    for row in rows[: args.limit]:
        price = f"{row['last_price']:.0f}" if row["last_price"] is not None else "?"
        rating = row["rating"] if row["rating"] is not None else "-"
        cam = " 📷" if row.get("images_analyzed") else ""
        print(f"  [{rating}/5]{cam} {price:>6}  {row['item_name']}: {row['title']}  {row['url']}")
    print(f"({len(rows)} total)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # --log-level lives on a shared parent so it is accepted both before and
    # after the subcommand (SUPPRESS default so the after-position copy never
    # clobbers a value set before the subcommand).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-level",
        default=argparse.SUPPRESS,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO); accepted before or after the command",
    )

    parser = argparse.ArgumentParser(
        prog="deal-radar", description="Marketplace deal monitor.", parents=[common]
    )
    parser.add_argument("--version", action="version", version=f"deal-radar {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def with_config(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text, parents=[common])
        sp.add_argument("--config", default="config.yaml", help="path to the YAML config")
        return sp

    with_config("validate-config", "validate the config and exit").set_defaults(func=_cmd_validate)

    p_run_once = with_config("run-once", "run a single scan of all items")
    p_run_once.add_argument(
        "--item",
        action="append",
        default=None,
        metavar="SUBSTR",
        help="only scan items whose name contains this (case-insensitive); "
        "repeatable, e.g. --item pc --item bike. Omit to scan all.",
    )
    p_run_once.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max listings to collect per marketplace (default: the scan.max_listings_per_search "
        "setting in your config)",
    )
    p_run_once.add_argument(
        "--max-evals",
        dest="max_evals",
        type=int,
        default=None,
        help="max AI evaluations per item per run (default: the "
        "scan.max_evaluations_per_item setting in your config)",
    )
    p_run_once.add_argument(
        "--dry-run", action="store_true", help="evaluate but do not send notifications"
    )
    p_run_once.add_argument(
        "--headful", action="store_true", help="show the browser window (default: headless)"
    )
    p_run_once.set_defaults(func=_cmd_run_once)

    p_run = with_config("run", "run the polling loop (interval + jitter + rate limiting)")
    p_run.add_argument(
        "--item",
        action="append",
        default=None,
        metavar="SUBSTR",
        help="only scan items whose name contains this (case-insensitive); "
        "repeatable, e.g. --item pc --item bike. Omit to scan all.",
    )
    p_run.add_argument(
        "--limit",
        type=int,
        default=None,
        help="max listings to collect per marketplace (default: the scan.max_listings_per_search "
        "setting in your config)",
    )
    p_run.add_argument(
        "--max-evals",
        dest="max_evals",
        type=int,
        default=None,
        help="max AI evaluations per item per cycle (default: the "
        "scan.max_evaluations_per_item setting in your config)",
    )
    p_run.add_argument(
        "--dry-run", action="store_true", help="evaluate but do not send notifications"
    )
    p_run.add_argument(
        "--headful", action="store_true", help="show the browser window (default: headless)"
    )
    p_run.add_argument(
        "--max-cycles",
        dest="max_cycles",
        type=int,
        default=None,
        help="stop after this many scan cycles (default: run until interrupted)",
    )
    p_run.set_defaults(func=_cmd_run)

    p_serve = with_config("serve", "run the local web UI (config editor, logs, scanner control)")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p_serve.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    p_serve.set_defaults(func=_cmd_serve)

    p_login = with_config("login", "log in once and save a browser session")
    p_login.add_argument("marketplace", nargs="?", default="facebook", help="marketplace to log in")
    p_login.set_defaults(func=_cmd_login)

    p_doctor = with_config("doctor", "check whether deal-radar is ready to scan")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_notify = with_config("test-notify", "send one test alert to prove notifications work")
    p_notify.set_defaults(func=_cmd_test_notify)

    p_setup = sub.add_parser("setup", help="download the browser deal-radar needs")
    p_setup.set_defaults(func=_cmd_setup)

    p_list = with_config("list-seen", "list previously seen listings")
    p_list.add_argument("--item", default=None, help="only this item")
    p_list.add_argument("--limit", type=int, default=50, help="max rows to print")
    p_list.set_defaults(func=_cmd_list_seen)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(getattr(args, "log_level", "INFO"))
    load_dotenv_if_present()

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help()
        return 1
    try:
        return int(func(args))
    except ConfigError as exc:
        print(f"config error: {exc}")
        return 1
    except DealRadarError as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
