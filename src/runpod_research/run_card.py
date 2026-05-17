"""Markdown run-card helpers for RunPod-backed research experiments."""

from __future__ import annotations

import shlex
from pathlib import Path


def _text(value: str | Path) -> str:
    return str(value)


def _double_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_run_card(
    *,
    title: str,
    spec: str | Path,
    queue: str | Path,
    image: str,
    storage_mode: str,
    archive_subdir: str,
    commit: str | None = None,
) -> str:
    """Render a generic Markdown run card template for a RunPod experiment."""

    spec_text = _text(spec)
    queue_text = _text(queue)
    commit_text = commit or "TBD"
    return f"""# {title}

## Question

- [ ] State the research question, hypothesis, and done condition.
- [ ] Record what result would change the next decision.

## Setup

- Spec: `{spec_text}`
- Queue: `{queue_text}`
- Image: `{image}`
- Storage mode: `{storage_mode}`
- Optional archive subdir: `{archive_subdir}`
- Commit: `{commit_text}`
- Operator docs read: `docs/operator-guide.md`
- Artifact contract read: `docs/contracts.md`
- Required local credentials checked without printing values: RunPod API token and SSH public key.
- [ ] Record GPU type, cloud/datacenter policy, seeds, dataset/model identifiers, and sweep parameters.
- [ ] Record any deviation from the spec or previous baseline settings.

## Launch Commands

```bash
uv run rpr-launch render \\
  --spec {shlex.quote(spec_text)}

uv run rpr-controller init-queue \\
  --spec {shlex.quote(spec_text)} \\
  --queue {shlex.quote(queue_text)}

uv run rpr-controller loop \\
  --queue {shlex.quote(queue_text)} \\
  --confirm-spend \\
  --confirm-cleanup \\
  --unreachable-grace-seconds 1800
```

Add remote archive promotion only if this run produces non-git artifacts that
need durable network-volume storage:

```bash
uv run rpr-controller loop \\
  --queue {shlex.quote(queue_text)} \\
  --confirm-spend \\
  --confirm-cleanup \\
  --unreachable-grace-seconds 1800 \\
  --promote-to-archive \\
  --archive-remote-subdir {_double_quote(archive_subdir)} \\
  --confirm-archive-sync \\
  --confirm-delete-sync-pod
```

- [ ] Record actual launch command if it differs.
- [ ] Record queue path, events path, controller PID/session, and start time.

## Artifact Contract

- Local archive root: `artifacts/runpod-lifecycle/sweeps/`
- Required lane artifacts: `status.json`, `lane_config.json`, `metrics_all.csv`, `CHECKSUMS.sha256`, `archive-receipt.json`.
- [ ] Record local archive path.
- [ ] Record aggregate output path.
- [ ] Record remote archive destination and verification command, or state why local/git storage is sufficient.
- [ ] Record any failed lane artifacts that were copied or declared disposable.

## Monitoring

```bash
uv run rpr-controller list \\
  --queue {shlex.quote(queue_text)}

uv run rpr-launch list pods
uv run rpr-launch list network-volumes
```

- [ ] Record live inventory before launch.
- [ ] Record queue status checkpoints.
- [ ] Record bootstrap timing and first-worker-step timing when available.

## Cleanup

- [ ] Confirm terminal lane artifacts are archived before pod deletion.
- [ ] Confirm temporary volumes are deleted or explicitly retained with reason.
- [ ] Confirm archive sync pod is deleted after verification, if a sync pod was used.
- [ ] Record cleanup command and result.

## Results

- [ ] Summarize result status and key metrics.
- [ ] Link local aggregate, remote archive, logs, and manifests.
- [ ] Record known deviations, failures, or follow-up experiments.

## Final Inventory

```bash
uv run rpr-launch list pods
uv run rpr-launch list network-volumes
```

- [ ] Record final pod inventory.
- [ ] Record final network volume inventory.
- [ ] Confirm only intentional stopped pods and protected archive volumes remain.
"""


def create_run_card(
    *,
    experiment_dir: str | Path,
    title: str,
    spec: str | Path,
    queue: str | Path,
    image: str,
    storage_mode: str,
    archive_subdir: str,
    commit: str | None = None,
    force: bool = False,
) -> Path:
    """Create ``README.md`` in an experiment directory."""

    output_dir = Path(experiment_dir)
    output_path = output_dir / "README.md"
    if output_path.exists() and not force:
        raise FileExistsError(output_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_run_card(
            title=title,
            spec=spec,
            queue=queue,
            image=image,
            storage_mode=storage_mode,
            archive_subdir=archive_subdir,
            commit=commit,
        )
    )
    return output_path
