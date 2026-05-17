# Project integration

Downstream research repos should treat `runpod-research` as infrastructure and keep project-specific choices in their own repo.

## Recommended layout in a downstream repo

```text
configs/runpod/
  my-sweep.json
  archive-sync-pod.json
experiments/<date>_<name>/README.md
build/                     # ignored controller queues, manifests, status cache, sync manifests
artifacts/                 # ignored local archives or externally synced outputs
```

## Importing from Python

```python
from runpod_research.queue import QueueStore
from runpod_research.schema import validate_spec_file
```

## CLI use from a downstream repo

Install this repo into the downstream UV project or run it from a shared environment, then invoke:

```bash
uv add "runpod-research @ git+https://github.com/benjamin-shih/runpod-infra.git"
uv run rpr validate spec --path configs/runpod/my-sweep.json
uv run rpr controller init-queue --spec configs/runpod/my-sweep.json --queue build/runpod-queues/my-sweep/queue.json
uv run rpr controller tick --queue build/runpod-queues/my-sweep/queue.json --events-path build/runpod-queues/my-sweep/events.jsonl
```

Use `docs/quickstart.md` for the full collaborator setup and live-run flow. Downstream repos should ignore generated `build/` and `artifacts/` outputs broadly, not only queue files, because defaults also write launch manifests, monitor status cache, archive-sync manifests, and local artifact archives.

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
