from __future__ import annotations

import json
from pathlib import Path

import pytest

from runpod_research.lifecycle import (
    ArtifactVerificationError,
    UnknownPodError,
    archive_destination,
    copy_artifacts_from_local,
    decision_for_status,
    endpoint_from_pod,
    load_launch_manifest_index,
    read_remote_status,
    remote_run_root_discovery_script,
    require_launch_manifest_entry,
    verify_required_artifacts,
    write_checksums,
    write_status_cache,
)


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def test_launch_manifest_guard_accepts_known_parallel_pod(tmp_path: Path) -> None:
    manifest = tmp_path / "build/runpod-launch-manifests/20260426_sweep/launch_manifest.json"
    write_json(
        manifest,
        {
            "spec_path": "configs/runpod/demo-full-sweep.json",
            "jobs": [
                {
                    "job_name": "lane-a",
                    "pod_name": "sweep-lane-a",
                    "pod_id": "pod-123",
                    "volume_id": "vol-123",
                    "payload": {
                        "env": {
                            "RPR_SWEEP_NAME": "sweep",
                            "RPR_JOB_NAME": "lane-a",
                            "RPR_ARTIFACT_ROOT": "/workspace/experiments/root",
                        }
                    },
                }
            ],
        },
    )

    index = load_launch_manifest_index(tmp_path / "build/runpod-launch-manifests")
    entry = index["pod-123"]

    assert entry.job_name == "lane-a"
    assert entry.volume_id == "vol-123"
    assert entry.artifact_root == "/workspace/experiments/root"


def test_launch_manifest_index_ignores_non_manifest_json(tmp_path: Path) -> None:
    root = tmp_path / "build/runpod-launch-manifests"
    write_json(
        root / "not-a-launch-manifest.json",
        {
            "schema_version": 1,
            "pod_id": "pod-stray",
            "job_name": "stray",
        },
    )
    write_json(
        root / "bad-schema" / "launch_manifest.json",
        {
            "schema_version": 2,
            "pod_id": "pod-bad-schema",
            "job_name": "bad-schema",
        },
    )
    write_json(
        root / "good" / "launch_manifest.json",
        {
            "schema_version": 1,
            "spec_path": "configs/runpod/example.json",
            "jobs": [
                {
                    "job_name": "lane-a",
                    "pod_name": "sweep-lane-a",
                    "pod_id": "pod-good",
                }
            ],
        },
    )

    assert set(load_launch_manifest_index(root)) == {"pod-good"}


def test_launch_manifest_guard_rejects_unknown_pod(tmp_path: Path) -> None:
    with pytest.raises(UnknownPodError):
        require_launch_manifest_entry(tmp_path / "missing", "pod-missing")


def test_copy_verify_checksums_and_status_cache(tmp_path: Path) -> None:
    remote = tmp_path / "remote" / "20260426T010203Z"
    write_json(remote / "status.json", {"status": "DONE", "completed_l1_count": 1})
    write_json(remote / "lane_config.json", {"model_name": "gpt2-small"})
    write_json(remote / "training_summary.json", {"final_loss": 1.0})
    (remote / "code_snapshot.bundle").write_text("bundle")
    (remote / "metrics_all.csv").write_text("run_id,lambda_1\nr,1e-4\n")
    (remote / "training/run/run.log").parent.mkdir(parents=True)
    (remote / "training/run/run.log").write_text("training log\n")
    (remote / "training/run/checkpoints").mkdir(parents=True)
    (remote / "training/run/checkpoints/final.pt").write_text("large checkpoint")
    (remote / "outputs/cache").mkdir(parents=True)
    (remote / "outputs/cache/activations.npz").write_text("activation cache")

    local = tmp_path / "archive"
    copied = copy_artifacts_from_local(remote, local)
    verify_required_artifacts(local)
    checksums = write_checksums(local)

    assert local / "status.json" in copied
    assert "metrics_all.csv" in checksums
    assert "training_summary.json" in checksums
    assert "code_snapshot.bundle" in checksums
    assert "outputs/cache/activations.npz" in checksums
    assert not (local / "training/run/checkpoints/final.pt").exists()

    entry = require_launch_manifest_entry_from_payload(tmp_path)
    cache_path = write_status_cache(
        tmp_path / "status-cache",
        entry=entry,
        status={"status": "DONE"},
        local_archive=local,
        remote_run_root="/workspace/run",
    )
    cached = json.loads(cache_path.read_text())
    assert cached["pod_id"] == "pod-cache"
    assert cached["local_archive"] == str(local)


def test_copy_artifacts_includes_lora_tensors_with_checkpoint_flag(tmp_path: Path) -> None:
    remote = tmp_path / "remote" / "20260426T010203Z"
    write_json(remote / "status.json", {"status": "DONE"})
    write_json(remote / "lane_config.json", {"model_name": "llama"})
    (remote / "metrics_all.csv").write_text("metric,value\nok,1\n")
    (remote / "training/checkpoints/checkpoint-1").mkdir(parents=True)
    (remote / "training/checkpoints/checkpoint-1/adapter_model.safetensors").write_text(
        "checkpoint tensor"
    )
    (remote / "training/final_adapter").mkdir(parents=True)
    (remote / "training/final_adapter/adapter_model.safetensors").write_text(
        "final tensor"
    )
    (remote / "outputs/training/final_adapter").mkdir(parents=True)
    (remote / "outputs/training/final_adapter/adapter_model.safetensors").write_text(
        "copied final tensor"
    )

    local = tmp_path / "archive"
    copy_artifacts_from_local(remote, local, include_checkpoints=True)

    assert (local / "training/checkpoints/checkpoint-1/adapter_model.safetensors").exists()
    assert (local / "training/final_adapter/adapter_model.safetensors").exists()
    assert (local / "outputs/training/final_adapter/adapter_model.safetensors").exists()


def test_verify_required_artifacts_reports_missing(tmp_path: Path) -> None:
    (tmp_path / "status.json").write_text("{}")
    with pytest.raises(ArtifactVerificationError, match="lane_config.json"):
        verify_required_artifacts(tmp_path)


def test_endpoint_from_pod_prefers_runtime_ssh_mapping() -> None:
    endpoint = endpoint_from_pod(
        {
            "publicIp": "1.2.3.4",
            "runtime": {"ports": [{"ip": "5.6.7.8", "privatePort": 22, "publicPort": 2222}]},
        }
    )

    assert endpoint.host == "5.6.7.8"
    assert endpoint.port == 2222


def test_reap_decisions_are_conservative_for_failed_volumes() -> None:
    done = decision_for_status("DONE", has_volume=True)
    done_with_volume_delete = decision_for_status("DONE", has_volume=True, delete_success_volume=True)
    failed = decision_for_status("FAILED", has_volume=True)
    running = decision_for_status("RUNNING", has_volume=True)

    assert done.delete_volume is False
    assert done_with_volume_delete.delete_volume is True
    assert failed.delete_pod is True
    assert failed.delete_volume is False
    assert running.sync_artifacts is False


def test_archive_destination_uses_remote_launch_stamp(tmp_path: Path) -> None:
    entry = require_launch_manifest_entry_from_payload(tmp_path)

    destination = archive_destination(
        tmp_path / "archive-root",
        entry=entry,
        remote_run_root="/workspace/experiments/sweep/lane/20260426T010203Z",
    )

    assert destination == tmp_path / "archive-root/sweep-cache/lane-cache/20260426T010203Z"


def test_read_remote_status_shell_quotes_path(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[str] = []

    def fake_run_ssh(endpoint, ssh_key, command):
        commands.append(command)

        class Completed:
            stdout = '{"status":"DONE"}'

        return Completed()

    monkeypatch.setattr("runpod_research.lifecycle.run_ssh", fake_run_ssh)

    read_remote_status(
        endpoint=endpoint_from_pod({"publicIp": "1.2.3.4", "portMappings": {"22": 2222}}),
        ssh_key=Path("/tmp/key"),
        remote_run_root="/workspace/run roots/$(bad)",
    )

    assert commands == ["cat '/workspace/run roots/$(bad)/status.json'"]


def test_remote_run_root_discovery_requires_sweep_and_job() -> None:
    script = remote_run_root_discovery_script(
        artifact_root="/workspace/experiments/root",
        sweep_name="phase3-decision",
        job_name="flat-batchtopk-k640",
    )

    assert "f'{sweep}/{job}/*/status.json'" in script
    assert "f'**/{sweep}/{job}/*/status.json'" in script
    assert "f'**/{job}/*/status.json'" not in script


def require_launch_manifest_entry_from_payload(tmp_path: Path):
    manifest = tmp_path / "manifests/launch_manifest.json"
    write_json(
        manifest,
        {
            "spec_path": "configs/runpod/example.json",
            "jobs": [
                {
                    "job_name": "lane-cache",
                    "pod_name": "pod-cache-name",
                    "pod_id": "pod-cache",
                    "payload": {
                        "env": {
                            "RPR_SWEEP_NAME": "sweep-cache",
                            "RPR_JOB_NAME": "lane-cache",
                            "RPR_ARTIFACT_ROOT": "/workspace/experiments/root",
                        }
                    },
                }
            ],
        },
    )
    return require_launch_manifest_entry(tmp_path / "manifests", "pod-cache")
