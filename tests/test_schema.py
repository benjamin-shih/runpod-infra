from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from runpod_research.schema import validate_queue_payload, validate_spec_payload


ROOT = Path(__file__).resolve().parents[1]


def valid_spec() -> dict:
    return {
        "schema_version": 1,
        "name": "Smoke Sweep",
        "remote_artifact_root": "/workspace/experiments/smoke",
        "defaults": {
            "imageName": "runpod/pytorch:2.4.0",
            "storage_mode": "stateless",
        },
        "jobs": [
            {"name": "Lane A", "env": {"RPR_LANE": "a"}},
            {"name": "Lane B", "storage_mode": "temp-volume"},
        ],
    }


def valid_queue() -> dict:
    return {
        "schema_version": 1,
        "lanes": [
            {
                "lane_name": "lane-a",
                "sweep_name": "smoke-sweep",
                "spec_path": "configs/runpod/smoke.json",
                "job_index": 0,
                "state": "QUEUED",
            }
        ],
    }


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "runpod_research.validate_cli", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_valid_spec_has_no_issues() -> None:
    assert validate_spec_payload(valid_spec()) == []


def test_invalid_spec_reports_missing_and_job_issues() -> None:
    payload = {
        "remote_artifact_root": "/workspace/experiments/smoke",
        "storage_mode": "scratch",
        "defaults": {},
        "jobs": [
            {"name": "Lane A"},
            {"name": "Lane+A"},
            {"env": {"RPR_LANE": "missing-name"}},
        ],
    }

    issues = validate_spec_payload(payload)

    assert "missing required field: schema_version" in issues
    assert "missing required field: name" in issues
    assert "unsupported storage_mode at storage_mode: scratch" in issues
    assert "jobs[2].name is required" in issues
    assert "duplicate slugified job name: lane-a" in issues


def test_spec_requires_absolute_artifact_root() -> None:
    missing = valid_spec()
    del missing["remote_artifact_root"]

    invalid = valid_spec()
    invalid["remote_artifact_root"] = "artifacts/local"

    assert "missing required field: remote_artifact_root" in validate_spec_payload(missing)
    assert "remote_artifact_root must be an absolute remote path" in validate_spec_payload(
        invalid
    )


def test_spec_jobs_must_be_a_list() -> None:
    payload = valid_spec()
    payload["jobs"] = {"name": "not-a-list"}

    assert "jobs must be a list" in validate_spec_payload(payload)


def test_valid_queue_payload_has_no_issues() -> None:
    assert validate_queue_payload(valid_queue()) == []


def test_queue_rejects_invalid_state() -> None:
    payload = valid_queue()
    payload["lanes"][0]["state"] = "DONE"

    assert "lanes[0].state is invalid: DONE" in validate_queue_payload(payload)


def test_queue_reports_missing_required_record_fields() -> None:
    payload = {"schema_version": 1, "lanes": [{"state": "QUEUED"}]}

    issues = validate_queue_payload(payload)

    assert "lanes[0].lane_name is required" in issues
    assert "lanes[0].sweep_name is required" in issues
    assert "lanes[0].spec_path is required" in issues
    assert "lanes[0].job_index is required" in issues


def test_cli_returns_json_and_exit_codes(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(valid_spec()))

    valid_result = run_cli("spec", "--path", str(spec_path))

    assert valid_result.returncode == 0
    assert json.loads(valid_result.stdout) == {"ok": True, "issues": []}

    invalid_path = tmp_path / "queue.json"
    invalid_queue = valid_queue()
    invalid_queue["lanes"][0]["state"] = "DONE"
    invalid_path.write_text(json.dumps(invalid_queue))

    invalid_result = run_cli("queue", "--path", str(invalid_path))

    assert invalid_result.returncode == 1
    payload = json.loads(invalid_result.stdout)
    assert payload["ok"] is False
    assert "lanes[0].state is invalid: DONE" in payload["issues"]
