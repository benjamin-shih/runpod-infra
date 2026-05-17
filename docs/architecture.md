# Architecture

`runpod-research` is a standalone, dependency-light Python package for running research sweeps on individual RunPod pods. It gives downstream projects a Slurm-like lane lifecycle without requiring a cluster scheduler: one JSON sweep spec expands to one pod payload per job, a durable queue tracks each lane, and the controller launches/reaps lanes under explicit operator confirmations.

## Package and repository identity

- Python package: `runpod-research` / import package `runpod_research`.
- CLI family: top-level `rpr` with command groups such as `rpr controller`, plus direct aliases like `rpr-controller`.
- Source repository: `https://github.com/benjamin-shih/runpod-infra.git`.

The repository name is infrastructure-oriented, while the installable package and docs use `runpod-research` for the reusable sweep tooling.

## Runtime model

1. **Spec render** (`runpod_research.launcher`): loads a generic JSON sweep spec, validates it, merges defaults into jobs, injects `RPR_*` worker environment variables, expands local `${RUNPOD_*}` placeholders, strips controller-only metadata, and writes redacted launch manifests.
2. **Queue initialize** (`runpod_research.queue`, `runpod_research.controller_cli`): converts selected jobs into durable lane records. Queue paths are operator-chosen and should be unique per run.
3. **Controller tick/loop** (`runpod_research.controller`): reaps active lanes first, then launches queued lanes only when `--confirm-spend` is present and launch caps permit it.
4. **Reaper** (`runpod_research.reaper`, `runpod_research.lifecycle`): verifies pod provenance from launch manifests, discovers terminal run roots over SSH, copies generic artifacts, verifies required files, writes checksums/status cache, and only stops/deletes resources with cleanup confirmations.
5. **Archive promotion** (`runpod_research.archive_cli`, `runpod_research.archive_sync`): optionally uploads completed local archives through an SSH-capable sync pod with `--confirm-sync`.
6. **Monitoring and reporting** (`runpod_research.dashboard`, `runpod_research.aggregate`, `runpod_research.run_card_cli`): reads local manifests, queue/status cache, and archived metrics without requiring continuous agent monitoring.

## Generic boundaries

Belongs in this repo:

- RunPod payload rendering and API wrappers.
- Queue/controller/reaper state machine.
- Generic artifact verification, checksum, aggregation, dashboard, and archive sync helpers.
- Safety gates, offline validation, and collaborator/operator documentation.

Belongs downstream:

- Sweep generation/renderers, worker Dockerfiles, model names, dataset choices, image policy, and project-specific archive names.
- Worker code that consumes `RPR_SWEEP_NAME`, `RPR_JOB_NAME`, `RPR_ARTIFACT_ROOT`, and `RPR_LAUNCH_CREATED_AT_UTC`.
- Research run cards, experiment READMEs, generated queues/manifests, and bulky artifacts.

## Production safety posture

- Billable actions require `--confirm-spend`.
- Cleanup/destructive actions require explicit cleanup/delete flags.
- Controller launches are capped by default to one active lane and one new launch per tick; raise `--max-concurrent` and `--max-launches-per-tick` only after recording a budget.
- A queue is protected by an advisory lock during controller ticks and written with fsync-backed atomic replacement.
- `init-queue` refuses to overwrite an existing queue unless `--force` is supplied; active lanes require `--force-active-overwrite` after manual reconciliation.
- If a controller crashes after marking a lane `LAUNCHING` but before recording a pod id, the lane becomes `LAUNCH-UNKNOWN` on the next tick instead of being relaunched automatically. `LAUNCH-UNKNOWN` blocks further launches until operators inspect RunPod inventory and reconcile manually.
- Manifests, dashboards, and API error messages are redacted for known secret keys/values; agents and operators still must avoid printing local credential files or arbitrary worker logs containing secrets.

## Extension points

- Use JSON sweep specs directly, or generate them in downstream repos and validate with `uv run rpr validate spec --path <spec>`.
- Use the default SSH reaper when workers keep pods alive after terminal artifacts are written.
- For persistent artifact backends, keep the worker/backend integration downstream and use this repo for queue state, safety gates, and local validation until a generic backend interface is added.
- Import only documented stable helpers from `runpod_research.queue` and `runpod_research.schema` unless you are contributing to this infrastructure repo.
