"""Aggregate local RunPod lane archives into sweep-level tables."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lifecycle import utc_now, write_json


PROVENANCE_COLUMNS = (
    "sweep_name",
    "lane_name",
    "archive_stamp",
    "archive_path",
    "lane_status",
    "pod_id",
    "pod_name",
    "job_name",
    "remote_run_root",
    "metrics_checksum",
)
ISSUE_COLUMNS = (*PROVENANCE_COLUMNS, "has_metrics", "issue")


@dataclass(frozen=True)
class ArchiveSummary:
    sweep_name: str
    lane_name: str
    archive_stamp: str
    archive_path: str
    lane_status: str
    pod_id: str
    pod_name: str
    job_name: str
    remote_run_root: str
    has_metrics: bool
    metrics_checksum: str


@dataclass(frozen=True)
class AggregateResult:
    output_csv: Path
    issues_csv: Path
    manifest_json: Path
    archive_count: int
    metric_row_count: int
    issue_count: int


def aggregate_sweep_results(
    *,
    archive_root: Path,
    sweep_name: str | None = None,
    output_csv: Path | None = None,
    manifest_json: Path | None = None,
) -> AggregateResult:
    """Write a canonical sweep CSV from local per-lane archives.

    `archive_root` is usually `artifacts/runpod-lifecycle/sweeps`. Archives are
    expected under `<archive_root>/<sweep>/<lane>/<stamp>/`.
    """

    archives = discover_archives(archive_root=archive_root, sweep_name=sweep_name)
    rows: list[dict[str, str]] = []
    issue_rows: list[dict[str, str]] = []
    summaries: list[ArchiveSummary] = []
    metric_columns: list[str] = []

    for archive in archives:
        summary = summarize_archive(archive)
        summaries.append(summary)
        issue = _issue_for_summary(summary)
        if issue:
            issue_rows.append(
                {
                    **_summary_columns(summary),
                    "has_metrics": str(summary.has_metrics),
                    "issue": issue,
                }
            )
            continue
        metrics_path = archive / "metrics_all.csv"
        with metrics_path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                normalized = {key: value for key, value in row.items() if key is not None}
                for column in normalized:
                    if column not in metric_columns and column not in PROVENANCE_COLUMNS:
                        metric_columns.append(column)
                rows.append({**_summary_columns(summary), **normalized})

    output_csv = output_csv or default_output_csv(archive_root=archive_root, sweep_name=sweep_name)
    issues_csv = output_csv.with_name(output_csv.stem + "_issues.csv")
    manifest_json = manifest_json or output_csv.with_name(output_csv.stem + "_manifest.json")
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = list(PROVENANCE_COLUMNS) + metric_columns
    with output_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in fieldnames})
    with issues_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ISSUE_COLUMNS)
        writer.writeheader()
        for row in issue_rows:
            writer.writerow({column: row.get(column, "") for column in ISSUE_COLUMNS})

    write_json(
        manifest_json,
        {
            "created_at_utc": utc_now(),
            "archive_root": str(archive_root),
            "sweep_name": sweep_name,
            "output_csv": str(output_csv),
            "issues_csv": str(issues_csv),
            "archive_count": len(summaries),
            "metric_row_count": len(rows),
            "issue_count": len(issue_rows),
            "archives": [summary.__dict__ for summary in summaries],
        },
    )
    return AggregateResult(
        output_csv=output_csv,
        issues_csv=issues_csv,
        manifest_json=manifest_json,
        archive_count=len(summaries),
        metric_row_count=len(rows),
        issue_count=len(issue_rows),
    )


def default_output_csv(*, archive_root: Path, sweep_name: str | None) -> Path:
    if sweep_name:
        return archive_root / sweep_name / "sweep_results.csv"
    return archive_root / "sweep_results.csv"


def discover_archives(*, archive_root: Path, sweep_name: str | None = None) -> list[Path]:
    root = archive_root / sweep_name if sweep_name else archive_root
    if not root.exists():
        return []
    if sweep_name:
        candidates = root.glob("*/*")
    else:
        candidates = root.glob("*/*/*")
    return sorted(path for path in candidates if path.is_dir() and _looks_like_archive(path))


def summarize_archive(archive: Path) -> ArchiveSummary:
    lane_name = archive.parent.name
    sweep_name = archive.parent.parent.name
    receipt = _read_json_if_exists(archive / "archive-receipt.json")
    status = _read_json_if_exists(archive / "status.json")
    checksums = _read_checksums(archive / "CHECKSUMS.sha256")
    lane_status = str(receipt.get("lane_status") or status.get("status") or "")
    return ArchiveSummary(
        sweep_name=sweep_name,
        lane_name=lane_name,
        archive_stamp=archive.name,
        archive_path=str(archive),
        lane_status=lane_status,
        pod_id=str(receipt.get("pod_id") or ""),
        pod_name=str(receipt.get("pod_name") or ""),
        job_name=str(receipt.get("job_name") or ""),
        remote_run_root=str(receipt.get("remote_run_root") or status.get("run_root") or ""),
        has_metrics=(archive / "metrics_all.csv").exists(),
        metrics_checksum=checksums.get("metrics_all.csv", ""),
    )


def _looks_like_archive(path: Path) -> bool:
    return any((path / name).exists() for name in ("status.json", "archive-receipt.json", "metrics_all.csv"))


def _summary_columns(summary: ArchiveSummary) -> dict[str, str]:
    payload = summary.__dict__
    return {column: str(payload.get(column, "")) for column in PROVENANCE_COLUMNS}


def _issue_for_summary(summary: ArchiveSummary) -> str:
    if not summary.has_metrics:
        return "missing metrics_all.csv"
    if summary.lane_status.upper() != "DONE":
        return f"lane_status={summary.lane_status or 'UNKNOWN'}"
    return ""


def _read_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_checksums(path: Path) -> dict[str, str]:
    checksums: dict[str, str] = {}
    if not path.exists():
        return checksums
    for line in path.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if digest and name:
            checksums[name] = digest
    return checksums
