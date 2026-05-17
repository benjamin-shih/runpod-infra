# Project integration

Downstream research repos should treat `runpod-research` as infrastructure and keep project-specific choices in their own repo.

## Recommended layout in a downstream repo

```text
configs/runpod/
  my-sweep.json
  archive-sync-pod.json
experiments/<date>_<name>/README.md
build/runpod-queues/        # ignored
artifacts/runpod-lifecycle/ # ignored or externally synced
```

## Importing from Python

```python
from runpod_research.queue import QueueStore
from runpod_research.schema import validate_spec_file
```

## CLI use from a downstream repo

Install this repo into the downstream UV project or run it from a shared environment, then invoke:

```bash
uv run rpr validate spec --path configs/runpod/my-sweep.json
uv run rpr controller init-queue --spec configs/runpod/my-sweep.json --queue build/runpod-queues/my-sweep/queue.json
```

## What belongs downstream

- Sweep grid renderers.
- Worker Dockerfiles and image policy.
- Model/dataset-specific environment variables.
- Project archive naming conventions.
- Research run cards and experiment READMEs.

## What belongs in this repo

- RunPod API payload rendering.
- Queue/controller/reaper state machine.
- Artifact verification/checksum helpers.
- Generic archive sync mechanics.
- Generic docs/tests for safe operation.
