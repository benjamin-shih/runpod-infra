# Agent usage guide

This is the required first-read for future agents using or modifying `runpod-research`.

## Operating boundaries

- Treat this repo as generic infrastructure. Do not add downstream project defaults to core modules.
- Do not run live RunPod launches, pod cleanup, volume deletion, or archive uploads unless the user explicitly approves that live action.
- Never print credential values. It is fine to reference environment variable names.
- Do not use an agent session as a continuous monitor. Use controller loops, daemon logs, queues, status caches, and sparse checkpoints.

## Standard workflow

1. Inspect repo status and read the relevant docs.
2. Validate specs/queues offline before launch.
3. For live work, record initial inventory, queue path, events path, manifest root, archive root, and cleanup plan.
4. Launch through `rpr-controller` rather than ad hoc scripts.
5. Reap/archive before deleting pods.
6. Verify final pod/volume inventory and record any intentionally retained resources.

## Safety flags

- `--confirm-spend` gates billable launches.
- `--confirm-cleanup` gates controller-driven pod stop/delete actions.
- `--confirm-delete-temp-volumes` gates temporary volume deletion and requires cleanup confirmation.
- `--confirm-sync` gates archive upload; controller promotion also accepts `--confirm-archive-sync` as a more explicit alias.
- `--confirm-delete-pod` and `--confirm-stop` gate one-shot reaper/archive pod cleanup.

## Generic worker contract

Workers receive these controller-managed environment variables:

- `RPR_SWEEP_NAME`
- `RPR_JOB_NAME`
- `RPR_ARTIFACT_ROOT`
- `RPR_LAUNCH_CREATED_AT_UTC`

A worker should write a run root at:

```text
$RPR_ARTIFACT_ROOT/$RPR_SWEEP_NAME/$RPR_JOB_NAME/<timestamp>/
```

Required files in each terminal run root:

- `status.json` with terminal `status` such as `DONE` or `FAILED`.
- `lane_config.json` with reproducibility metadata.
- `metrics_all.csv` with lane metrics, even if there is one row.

The current reaper uses SSH to copy pod-local artifacts, so stateless workers must remain alive after writing terminal artifacts until the controller reaps them. If a worker exits immediately, RunPod may tear down the pod-local filesystem before artifacts are copied. Use a final sleep/service loop or a project-specific persistent artifact backend.

## Review expectations

For nontrivial changes, request a bounded review focused on:

- generic abstraction and absence of downstream project coupling;
- destructive/spend confirmation gates;
- secret redaction and artifact hygiene;
- tests/docs updated with the changed behavior.
