"""Command-line interface for deal-radar."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__, paths
from .config.loader import load_config, load_dotenv_if_present
from .config.schema import AppConfig, ItemConfig
from .errors import ConfigError, DealRadarError
from .logging import configure_logging, get_logger
from .pipeline import ScanStats, scan_item

log = get_logger("cli")


def _print_stats(stats: ScanStats) -> None:
    print(
        f"  {stats.item}: found={stats.found} new_seen_skipped={stats.skipped_seen} "
        f"filtered={stats.skipped_filter} evaluated={stats.evaluated} "
        f"matched={stats.matched} notified={stats.notified} errors={stats.errors}"
    )


def _select_items(cfg: AppConfig, only: str | None) -> list[ItemConfig]:
    items = [i for i in cfg.items if i.enabled and (only is None or i.name == only)]
    if only is not None and not items:
        raise ConfigError(f"no enabled item named {only!r}")
    return items


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
            f"{name}{'' if mk.enabled else ' (disabled)'}" for name, mk in cfg.marketplaces.items()
        )
    else:
        mks = "(none)"
    print(f"  marketplaces: {mks}")
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
    from .marketplaces.base import SearchContext
    from .marketplaces.registry import build_marketplace
    from .notifiers.registry import build_notifier

    cfg = load_config(args.config)
    items = _select_items(cfg, args.item)
    paths.ensure_data_dir()

    evaluator = ClaudeEvaluator(cfg.ai)
    notifiers = [build_notifier(n) for n in cfg.notifiers]

    needed = {
        m
        for item in items
        for m in item.marketplaces
        if m in cfg.marketplaces and cfg.marketplaces[m].enabled
    }
    if not needed:
        print("nothing to do: no enabled marketplaces referenced by selected items")
        return 0

    print(f"run-once: {len(items)} item(s), marketplaces={sorted(needed)}, dry_run={args.dry_run}")
    with SqliteSeenStore(paths.db_path()) as store:
        for mname in sorted(needed):
            mk_cfg = cfg.marketplaces[mname]
            marketplace = build_marketplace(
                mname, mk_cfg, headless=not args.headful, max_results=args.limit
            )
            with marketplace:
                ctx = SearchContext(config=mk_cfg, dry_run=args.dry_run)
                for item in items:
                    if mname not in item.marketplaces:
                        continue
                    stats = scan_item(
                        item=item,
                        marketplace=marketplace,
                        ctx=ctx,
                        evaluator=evaluator,
                        store=store,
                        notifiers=notifiers,
                        ai=cfg.ai,
                        max_evaluations=args.max_evals,
                        dry_run=args.dry_run,
                    )
                    _print_stats(stats)
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
        print(f"  [{rating}/5] {price:>6}  {row['item_name']}: {row['title']}  {row['url']}")
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
    p_run_once.add_argument("--item", default=None, help="only scan the item with this name")
    p_run_once.add_argument(
        "--limit", type=int, default=40, help="max listings to collect per marketplace (default 40)"
    )
    p_run_once.add_argument(
        "--max-evals",
        dest="max_evals",
        type=int,
        default=25,
        help="max AI evaluations per item per run (default 25)",
    )
    p_run_once.add_argument(
        "--dry-run", action="store_true", help="evaluate but do not send notifications"
    )
    p_run_once.add_argument(
        "--headful", action="store_true", help="show the browser window (default: headless)"
    )
    p_run_once.set_defaults(func=_cmd_run_once)

    with_config("run", "run the polling loop (Phase 2)").set_defaults(func=_cmd_stub)

    p_login = with_config("login", "log in once and save a browser session")
    p_login.add_argument("marketplace", nargs="?", default="facebook", help="marketplace to log in")
    p_login.set_defaults(func=_cmd_login)

    p_list = with_config("list-seen", "list previously seen listings")
    p_list.add_argument("--item", default=None, help="only this item")
    p_list.add_argument("--limit", type=int, default=50, help="max rows to print")
    p_list.set_defaults(func=_cmd_list_seen)

    return parser


def _cmd_stub(args: argparse.Namespace) -> int:
    log.error("'%s' is not implemented yet (planned for Phase 2).", args.command)
    return 2


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
