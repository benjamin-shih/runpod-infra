from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from runpod_research.lifecycle import (
    remote_run_root_discovery_script,
    volume_delete_blocker,
)


def test_remote_run_root_discovery_is_not_project_specific() -> None:
    script = remote_run_root_discovery_script(
        artifact_root="/workspace/experiments/generic-smoke",
        sweep_name="generic-smoke",
        job_name="lane-smoke",
    )

    assert "paper-specific-replication" not in script
    assert "generic-smoke" in script
    assert "**/" in script


def test_remote_run_root_discovery_prefers_terminal_root(tmp_path: Path) -> None:
    base = tmp_path / "experiments"
    old_done = base / "sweep" / "lane" / "20260427T010000Z"
    new_running = base / "sweep" / "lane" / "20260427T020000Z"
    old_done.mkdir(parents=True)
    new_running.mkdir(parents=True)
    (old_done / "status.json").write_text(json.dumps({"status": "DONE"}) + "\n")
    time.sleep(0.01)
    (new_running / "status.json").write_text(json.dumps({"status": "RUNNING"}) + "\n")

    completed = subprocess.run(
        remote_run_root_discovery_script(
            artifact_root=str(base),
            sweep_name="sweep",
            job_name="lane",
        ),
        shell=True,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == str(old_done)


def test_volume_delete_blocker_requires_collector_cleanup() -> None:
    assert (
        volume_delete_blocker(
            volume_id="temp-volume",
            collector_pod_id=None,
            confirm_delete_collector=False,
        )
        is None
    )
    assert (
        volume_delete_blocker(
            volume_id="temp-volume",
            collector_pod_id="collector",
            confirm_delete_collector=True,
        )
        is None
    )
    assert "collector pod" in (
        volume_delete_blocker(
            volume_id="temp-volume",
            collector_pod_id="collector",
            confirm_delete_collector=False,
        )
        or ""
    )
    assert "protected master" in (
        volume_delete_blocker(
            volume_id="master-volume",
            protected_volume_id="master-volume",
            collector_pod_id=None,
            confirm_delete_collector=False,
        )
        or ""
    )
