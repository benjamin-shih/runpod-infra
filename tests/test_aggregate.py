from __future__ import annotations

import csv
import json
from pathlib import Path

from runpod_research.aggregate import aggregate_sweep_results
from runpod_research.lifecycle import write_json


def write_archive(root: Path, *, lane: str, stamp: str, rows: list[dict[str, str]]) -> None:
    archive = root / "demo-sweep" / lane / stamp
    archive.mkdir(parents=True)
    write_json(
        archive / "archive-receipt.json",
        {
            "pod_id": f"pod-{lane}",
            "pod_name": f"pod-name-{lane}",
            "job_name": lane,
            "lane_status": "DONE",
            "remote_run_root": f"/workspace/{lane}/{stamp}",
        },
    )
    write_json(archive / "status.json", {"status": "DONE"})
    with (archive / "metrics_all.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (archive / "CHECKSUMS.sha256").write_text(
        "abc123  metrics_all.csv\n",
    )


def test_aggregate_sweep_results_writes_metrics_with_provenance(tmp_path: Path) -> None:
    archive_root = tmp_path / "artifacts"
    write_archive(
        archive_root,
        lane="lane-a",
        stamp="20260426T000000Z",
        rows=[{"run_id": "a", "lambda_1": "0.1", "l0": "12"}],
    )
    write_archive(
        archive_root,
        lane="lane-b",
        stamp="20260426T000100Z",
        rows=[{"run_id": "b", "lambda_1": "0.2", "l0": "8"}],
    )

    result = aggregate_sweep_results(archive_root=archive_root, sweep_name="demo-sweep")

    assert result.archive_count == 2
    assert result.metric_row_count == 2
    assert result.output_csv == archive_root / "demo-sweep" / "sweep_results.csv"
    assert result.manifest_json.exists()

    with result.output_csv.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["lane_name"] for row in rows] == ["lane-a", "lane-b"]
    assert [row["run_id"] for row in rows] == ["a", "b"]
    assert rows[0]["pod_id"] == "pod-lane-a"
    assert rows[0]["metrics_checksum"] == "abc123"


def test_aggregate_sweep_results_records_archives_without_metrics(tmp_path: Path) -> None:
    archive_root = tmp_path / "artifacts"
    archive = archive_root / "demo-sweep" / "failed-lane" / "20260426T000000Z"
    archive.mkdir(parents=True)
    write_json(archive / "status.json", {"status": "FAILED"})

    result = aggregate_sweep_results(archive_root=archive_root, sweep_name="demo-sweep")

    assert result.archive_count == 1
    assert result.metric_row_count == 0
    assert result.output_csv.read_text().startswith("sweep_name,lane_name")


def test_aggregate_sweep_results_flags_force_cleaned_failed_lanes(tmp_path: Path) -> None:
    archive_root = tmp_path / "artifacts"
    write_archive(
        archive_root,
        lane="lane-a",
        stamp="20260426T000000Z",
        rows=[{"run_id": "a", "lambda_1": "0.1", "l0": "12"}],
    )
    failed = archive_root / "demo-sweep" / "lane-b" / "20260426T000100Z"
    failed.mkdir(parents=True)
    write_json(
        failed / "status.json",
        {
            "status": "FAILED",
            "run_root": "/workspace/demo-sweep/lane-b/20260426T000100Z",
        },
    )
    (failed / "CHECKSUMS.sha256").write_text("")

    result = aggregate_sweep_results(archive_root=archive_root, sweep_name="demo-sweep")

    with result.output_csv.open(newline="") as handle:
        metric_rows = list(csv.DictReader(handle))
    with result.issues_csv.open(newline="") as handle:
        issue_rows = list(csv.DictReader(handle))
    manifest = json.loads(result.manifest_json.read_text())

    assert result.archive_count == 2
    assert result.metric_row_count == 1
    assert result.issue_count == 1
    assert [row["lane_name"] for row in metric_rows] == ["lane-a"]
    assert [row["lane_name"] for row in issue_rows] == ["lane-b"]
    assert issue_rows[0]["lane_status"] == "FAILED"
    assert issue_rows[0]["issue"] == "missing metrics_all.csv"
    assert manifest["issues_csv"] == str(result.issues_csv)
