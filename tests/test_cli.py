from __future__ import annotations

from pathlib import Path

import pytest

from runpod_research import cli


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "group",
    [
        "launch",
        "controller",
        "reap",
        "archive",
        "aggregate",
        "validate",
        "run-card",
        "dashboard",
    ],
)
def test_top_level_cli_dispatches_group_help(group: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main([group, "--help"])

    assert exc_info.value.code == 0


def test_documented_offline_smoke_flow(tmp_path: Path) -> None:
    spec = ROOT / "examples/specs/stateless-smoke.json"
    manifest_root = tmp_path / "manifests"
    queue = tmp_path / "queue.json"
    events = tmp_path / "events.jsonl"

    assert cli.main(["validate", "spec", "--path", str(spec)]) == 0
    assert cli.main(["launch", "render", "--spec", str(spec), "--out-dir", str(manifest_root)]) == 0
    assert cli.main(["controller", "init-queue", "--spec", str(spec), "--queue", str(queue)]) == 0
    assert cli.main(["validate", "queue", "--path", str(queue)]) == 0
    assert cli.main(["controller", "tick", "--queue", str(queue), "--events-path", str(events)]) == 0
