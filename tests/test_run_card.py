from __future__ import annotations

from pathlib import Path

import pytest

from runpod_research import run_card_cli
from runpod_research.run_card import create_run_card, render_run_card


def render_kwargs() -> dict[str, str]:
    return {
        "title": "Demo RunPod Sweep",
        "spec": "configs/runpod/demo.json",
        "queue": "build/runpod-queues/demo/queue.json",
        "image": "ghcr.io/example/research-worker:demo",
        "storage_mode": "stateless",
        "archive_subdir": "demo-sweeps/20260426T120000Z",
        "commit": "abc1234",
    }


def test_render_run_card_includes_sections_values_and_checklists() -> None:
    markdown = render_run_card(**render_kwargs())

    assert markdown.startswith("# Demo RunPod Sweep\n")
    for section in [
        "Question",
        "Setup",
        "Launch Commands",
        "Artifact Contract",
        "Monitoring",
        "Cleanup",
        "Results",
        "Final Inventory",
    ]:
        assert f"## {section}\n" in markdown

    assert "- Spec: `configs/runpod/demo.json`" in markdown
    assert "- Queue: `build/runpod-queues/demo/queue.json`" in markdown
    assert "- Image: `ghcr.io/example/research-worker:demo`" in markdown
    assert "- Storage mode: `stateless`" in markdown
    assert "- Optional archive subdir: `demo-sweeps/20260426T120000Z`" in markdown
    assert "- Commit: `abc1234`" in markdown
    assert "--spec configs/runpod/demo.json" in markdown
    assert "--queue build/runpod-queues/demo/queue.json" in markdown
    assert '--archive-remote-subdir "demo-sweeps/20260426T120000Z"' in markdown
    assert "- [ ] State the research question" in markdown
    assert "- [ ] Record final pod inventory." in markdown


def test_create_run_card_refuses_to_overwrite_existing_readme(tmp_path: Path) -> None:
    experiment_dir = tmp_path / "experiments" / "demo"
    experiment_dir.mkdir(parents=True)
    readme = experiment_dir / "README.md"
    readme.write_text("existing\n")

    with pytest.raises(FileExistsError):
        create_run_card(experiment_dir=experiment_dir, **render_kwargs())

    assert readme.read_text() == "existing\n"


def test_create_run_card_force_overwrites_existing_readme(tmp_path: Path) -> None:
    experiment_dir = tmp_path / "experiments" / "demo"
    experiment_dir.mkdir(parents=True)
    readme = experiment_dir / "README.md"
    readme.write_text("existing\n")

    created = create_run_card(experiment_dir=experiment_dir, force=True, **render_kwargs())

    assert created == readme
    assert "existing" not in readme.read_text()
    assert "# Demo RunPod Sweep" in readme.read_text()


def test_cli_create_makes_parent_directories_and_writes_readme(tmp_path: Path) -> None:
    experiment_dir = tmp_path / "new" / "nested" / "demo"

    result = run_card_cli.main(
        [
            "create",
            "--experiment-dir",
            str(experiment_dir),
            "--title",
            "Demo RunPod Sweep",
            "--spec",
            "configs/runpod/demo.json",
            "--queue",
            "build/runpod-queues/demo/queue.json",
            "--image",
            "ghcr.io/example/research-worker:demo",
            "--storage-mode",
            "stateless",
            "--archive-subdir",
            "demo-sweeps/20260426T120000Z",
            "--commit",
            "abc1234",
        ]
    )

    readme = experiment_dir / "README.md"
    assert result == 0
    assert readme.exists()
    assert "- Spec: `configs/runpod/demo.json`" in readme.read_text()
