"""Command-line interface for deal-radar.

Phase 0 ships a working ``validate-config``; ``run``, ``run-once``, and
``list-seen`` are placeholders until later phases.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__
from .config.loader import load_config, load_dotenv_if_present
from .errors import ConfigError, DealRadarError
from .logging import configure_logging, get_logger

log = get_logger("cli")

_NOT_IMPLEMENTED: dict[str, str] = {
    "run": "Phase 2 (scheduled loop)",
    "run-once": "Phase 1 (single scan)",
    "list-seen": "Phase 1 (SQLite seen store)",
}


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
            for name, mk in cfg.marketplaces.items()
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
            f"    - {item.name}{flag}: "
            f"phrases={len(item.search_phrases)} "
            f"markets={item.marketplaces} "
            f"min_rating={item.effective_min_rating(cfg.ai)}"
        )
    return 0


def _cmd_stub(args: argparse.Namespace) -> int:
    phase = _NOT_IMPLEMENTED.get(args.command, "a later phase")
    log.error("'%s' is not implemented yet (planned for %s).", args.command, phase)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deal-radar", description=__doc__)
    parser.add_argument("--version", action="version", version=f"deal-radar {__version__}")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity (default: INFO)",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument(
            "--config",
            default="config.yaml",
            help="path to the YAML config (default: config.yaml)",
        )
        return sp

    add("validate-config", "load and validate the config, then exit").set_defaults(
        func=_cmd_validate
    )
    add("run-once", "run a single scan of all items (Phase 1)").set_defaults(func=_cmd_stub)
    add("run", "run the polling loop (Phase 2)").set_defaults(func=_cmd_stub)
    add("list-seen", "list previously seen listings (Phase 1)").set_defaults(func=_cmd_stub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)
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


if __name__ == "__main__":
    raise SystemExit(main())
