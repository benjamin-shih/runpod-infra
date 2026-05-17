from __future__ import annotations

from pathlib import Path

import pytest

from runpod_research.archive_sync import (
    build_archive_manifest,
    build_rsync_upload_command,
    validate_remote_subdir,
)
from runpod_research.lifecycle import PodEndpoint


def test_validate_remote_subdir_rejects_unsafe_paths() -> None:
    for value in ("", "/absolute", "../escape", "safe/../../escape", "bad space", "bad/$HOME", "bad/`date`"):
        with pytest.raises(ValueError):
            validate_remote_subdir(value)

    assert validate_remote_subdir("runpod-smoke/20260426T053824Z") == "runpod-smoke/20260426T053824Z"


def test_build_archive_manifest_preserves_relative_paths(tmp_path: Path) -> None:
    archive = tmp_path / "demo-sweep"
    (archive / "lane-a" / "stamp").mkdir(parents=True)
    (archive / "lane-a" / "stamp" / "metrics_all.csv").write_text("a,b\n1,2\n")
    queue = tmp_path / "queue.json"
    queue.write_text("{}\n")

    manifest = build_archive_manifest(
        local_paths=[archive, queue],
        remote_subdir="runpod-smoke/20260426T053824Z",
    )

    paths = {entry["path"] for entry in manifest["files"]}
    assert "demo-sweep/lane-a/stamp/metrics_all.csv" in paths
    assert "queue.json" in paths
    assert manifest["remote_subdir"] == "runpod-smoke/20260426T053824Z"
    assert all(entry["sha256"] for entry in manifest["files"])


def test_rsync_upload_command_quotes_unsafe_remote_root(tmp_path: Path) -> None:
    source = tmp_path / "archive"
    source.mkdir()
    endpoint = PodEndpoint(host="1.2.3.4", port=2222)

    command = build_rsync_upload_command(
        endpoint=endpoint,
        ssh_key=Path("/tmp/key"),
        local_path=source,
        remote_root="/workspace/archive root/$unsafe",
        remote_subdir="runpod-smoke/20260426T053824Z",
    )

    assert command.argv[-1].startswith("root@1.2.3.4:")
    assert command.argv[-1].endswith("'")
    assert "$unsafe" in command.argv[-1]


def test_rsync_upload_command_copies_path_under_remote_subdir(tmp_path: Path) -> None:
    source = tmp_path / "archive"
    source.mkdir()
    endpoint = PodEndpoint(host="1.2.3.4", port=2222)

    command = build_rsync_upload_command(
        endpoint=endpoint,
        ssh_key=Path("/tmp/key"),
        local_path=source,
        remote_root="/workspace/archives/runpod-research",
        remote_subdir="runpod-smoke/20260426T053824Z",
    )

    assert command.argv[0] == "rsync"
    assert "--no-owner" in command.argv
    assert "--no-group" in command.argv
    assert str(source) in command.argv
    assert command.argv[-1].endswith(
        ":/workspace/archives/runpod-research/runpod-smoke/20260426T053824Z/"
    )
    assert "StrictHostKeyChecking=accept-new" in " ".join(command.argv)
