"""Top-level command dispatcher for runpod-research."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from runpod_research import __version__
from runpod_research import aggregate_cli, archive_cli, controller_cli, dashboard, launcher, reaper
from runpod_research import run_card_cli, validate_cli

CommandMain = Callable[[list[str] | None], int | None]


def _call_main(module_main: Callable[..., int | None], argv: list[str]) -> int:
    try:
        result = module_main(argv)
    except TypeError:
        original = sys.argv
        sys.argv = [original[0], *argv]
        try:
            result = module_main()
        finally:
            sys.argv = original
    return int(result or 0)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rpr",
        description="Reusable RunPod research controller commands.",
    )
    parser.add_argument("--version", action="version", version=f"runpod-research {__version__}")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "launch",
            "controller",
            "reap",
            "archive",
            "aggregate",
            "validate",
            "run-card",
            "dashboard",
        ),
        help="Command group. Use `uv run rpr <group> --help` for group-specific help.",
    )
    parser.add_argument("args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command is None:
        parse_args(["--help"])
        return 0
    dispatch: dict[str, Callable[..., int | None]] = {
        "launch": launcher.main,
        "controller": controller_cli.main,
        "reap": reaper.main,
        "archive": archive_cli.main,
        "aggregate": aggregate_cli.main,
        "validate": validate_cli.main,
        "run-card": run_card_cli.main,
        "dashboard": dashboard.main,
    }
    return _call_main(dispatch[args.command], list(args.args))


if __name__ == "__main__":
    raise SystemExit(main())
