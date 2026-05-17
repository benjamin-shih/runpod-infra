# Contracts

## Sweep spec

Specs are JSON objects with `schema_version: 1`, `name`, `remote_artifact_root`, `defaults`, and `jobs`.

- `remote_artifact_root` must be an absolute remote path.
- `defaults` is merged into each job payload.
- Each job must have a `name`; its slug becomes `RPR_JOB_NAME`.
- Controller-only metadata such as `storage_mode`, `artifact_contract`, and `controller` is stripped before POSTing to RunPod.
- Only local placeholders named `${RUNPOD_*}` are expanded before launch. Other placeholders remain available for the worker shell.

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

The default reaper copies artifacts over SSH from a still-running pod. Stateless workers should keep the container alive after writing these files until reaping completes, unless the project implements its own persistent artifact backend.

Optional generic paths copied by default include logs, metrics, outputs, evals, training summaries, code snapshots, and small checkpoint metadata. Large checkpoint payloads are copied only with `--include-checkpoints`.

## Queue contract

The queue is a single JSON file with one writer/controller. Lane states are durable and restart-safe:

- `QUEUED`
- `LAUNCHING`
- `RUNNING`
- `SYNCING`
- `CLEANED`
- failure states for launch/running/cleanup paths

`SYNCING` is persisted before artifact reaping so a crash can resume safely.

## Archive promotion contract

Archive promotion copies selected local archive paths to a configured remote root and subdirectory through an SSH-capable sync pod. Remote subdirectories must be relative and must not contain `..`.
