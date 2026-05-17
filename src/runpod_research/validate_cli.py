#!/usr/bin/env python3
"""Validate RunPod sweep specs and lifecycle queue JSON files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from runpod_research.schema import validate_queue_file, validate_spec_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    spec_parser = subparsers.add_parser("spec", help="validate a RunPod sweep spec")
    spec_parser.add_argument("--path", type=Path, required=True)

    queue_parser = subparsers.add_parser("queue", help="validate a RunPod lifecycle queue")
    queue_parser.add_argument("--path", type=Path, required=True)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "spec":
        issues = validate_spec_file(args.path)
    else:
        issues = validate_queue_file(args.path)

    result = {"ok": not issues, "issues": issues}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
