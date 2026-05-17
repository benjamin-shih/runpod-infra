#!/usr/bin/env python3
"""Aggregate local RunPod lifecycle archives into a sweep results table."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from runpod_research.aggregate import aggregate_sweep_results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-root",
        type=Path,
        default=Path("artifacts/runpod-lifecycle/sweeps"),
        help="Root containing <sweep>/<lane>/<stamp> local archives.",
    )
    parser.add_argument("--sweep", help="Optional sweep name to aggregate.")
    parser.add_argument("--output-csv", type=Path)
    parser.add_argument("--manifest-json", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = aggregate_sweep_results(
        archive_root=args.archive_root,
        sweep_name=args.sweep,
        output_csv=args.output_csv,
        manifest_json=args.manifest_json,
    )
    print(
        json.dumps(
            {
                "output_csv": str(result.output_csv),
                "issues_csv": str(result.issues_csv),
                "manifest_json": str(result.manifest_json),
                "archive_count": result.archive_count,
                "metric_row_count": result.metric_row_count,
                "issue_count": result.issue_count,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
