#!/usr/bin/env python3
"""Create RunPod experiment run-card READMEs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


from runpod_research.run_card import create_run_card


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create <experiment-dir>/README.md.")
    create.add_argument("--experiment-dir", type=Path, required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--spec", required=True)
    create.add_argument("--queue", required=True)
    create.add_argument("--image", required=True)
    create.add_argument("--storage-mode", required=True)
    create.add_argument("--archive-subdir", required=True)
    create.add_argument("--commit")
    create.add_argument("--force", action="store_true")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "create":
        try:
            output_path = create_run_card(
                experiment_dir=args.experiment_dir,
                title=args.title,
                spec=args.spec,
                queue=args.queue,
                image=args.image,
                storage_mode=args.storage_mode,
                archive_subdir=args.archive_subdir,
                commit=args.commit,
                force=args.force,
            )
        except FileExistsError as exc:
            print(f"{exc.filename} already exists; pass --force to overwrite", file=sys.stderr)
            return 1
        print(output_path)
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
