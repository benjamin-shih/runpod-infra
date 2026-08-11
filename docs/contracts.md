# Contracts

## Sweep spec

Specs are JSON objects with `schema_version: 1`, `name`, `remote_artifact_root`, `defaults`, and `jobs`.

- `remote_artifact_root` must be an absolute remote path.
- `defaults` is merged into each job payload.
- Each job must have a non-empty `name`; its slug becomes `RPR_JOB_NAME`, and duplicate slugified job names are invalid.
- Valid `storage_mode` values are `stateless`, `temp-volume`, and `master-volume`.
- Controller-only metadata such as `storage_mode`, `artifact_contract`, and `controller` is stripped before POSTing to RunPod.
- Only local placeholders named `${RUNPOD_*}` are expanded before launch. Other placeholders remain available for the worker shell.
- Offline render may report unresolved `${RUNPOD_*}` placeholders for inspection. Live launch and confirmed controller launch fail until all `${RUNPOD_*}` placeholders in the RunPod payload resolve locally.

## Worker environment

The launcher injects:

- `RPR_SWEEP_NAME`
- `RPR_JOB_NAME`
- `RPR_ARTIFACT_ROOT`
- `RPR_LAUNCH_CREATED_AT_UTC`

## Lane artifact contract

A terminal lane run root should contain:

- `status.json`
- `lane_config.json`
- `metrics_all.csv`

`status.json` should contain a top-level `status`. Terminal statuses recognized by the default reaper are `DONE` and `FAILED`. For `DONE`, the local archive must contain all required files above or reaping fails verification.

The default reaper copies artifacts over SSH from a still-running pod. Stateless workers should keep the container alive after writing these files until reaping completes, unless the project implements its own persistent artifact backend.

The local `CHECKSUMS.sha256` manifest covers stable archived artifacts. It
intentionally excludes itself and the mutable `archive-receipt.json`; the final
receipt embeds the checksum map for the other artifacts after terminal cleanup.

Optional generic paths copied by default include root and nested logs, metrics, outputs, evals,
training summaries, code snapshots, top-level eligibility decisions, compressed JSONL behavior
rows, scientific NumPy array outputs under `outputs/`, and small checkpoint metadata. Large checkpoint payloads are copied
only with `--include-checkpoints`. For LoRA/PEFT training lanes, use
`--include-checkpoints` when adapter tensors are required; the checkpoint policy
includes scheduled checkpoint directories and final adapter directories under
both `training/` and `outputs/training/`.

## Queue contract

The queue is a single JSON file with one writer/controller. Controller ticks take an advisory lock and replace the queue with fsync-backed atomic writes. Lane states are durable and restart-safe:

- `QUEUED`
- `LAUNCHING`
- `LAUNCH-UNKNOWN`
- `RUNNING`
- `SYNCING`
- `CLEANED`
- failure states for launch/running/cleanup paths

`SYNCING` is persisted before artifact reaping so a crash can resume safely. `LAUNCH-UNKNOWN` means the controller found a lane that was `LAUNCHING` before a pod id was recorded; it is intentionally not retried automatically because a crash after a successful RunPod POST could otherwise double-launch a lane. `LAUNCH-UNKNOWN` also blocks further launches and active-queue overwrite until an operator inspects RunPod inventory and reconciles the lane.

## Archive promotion contract

Archive promotion copies selected local archive paths to a configured remote root and subdirectory through an SSH-capable sync pod. Remote subdirectories must be relative, must not contain `..`, and must use safe path components made of letters, numbers, `.`, `_`, `-`, `+`, or `=`.
