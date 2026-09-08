# Agent usage guide

This is the required first-read for future agents using or modifying `runpod-research`. For first use on a collaborator machine, read `docs/quickstart.md` next.

## Operating boundaries

- Treat this repo as generic infrastructure. Do not add downstream project defaults to core modules.
- Do not run live RunPod launches, pod cleanup, volume deletion, or archive uploads unless the user explicitly approves that live action.
- Never print credential values. It is fine to reference environment variable names.
- For an account-level API action inside a Pod, explicitly set
  `RUNPOD_ACCOUNT_API_KEY` when `RUNPOD_API_KEY` is pod-scoped. The account alias
  takes precedence, including when loaded from an ignored env file; do not
  print either key or assume the inherited key has account permissions.
- Do not use an agent session as a continuous monitor. Use controller loops, daemon logs, queues, status caches, and sparse checkpoints.

## Compute and archive storage

Use stateless, pod-local storage for new compute workers unless the task
explicitly requires a mounted volume. Keep persistent archive storage separate:
reap the worker to the controller's archive first, then promote that archive
through a sync pod when needed. An existing archive volume or a locally set
`RUNPOD_NETWORK_VOLUME_ID` is not a reason to attach that volume to every GPU.

A stateless worker needs its source, datasets, and adapters staged before its
command starts, or an image that already contains them. Reuse the downstream
project's SSH/bootstrap transfer flow. Size the local disk for inputs, caches,
and outputs, and include transfer time in the worker's launch-to-cleanup budget.
Keep explicitly requested volume and placement settings intact; for a stateless
job, do not inherit an archive volume's region constraint.

## Standard workflow

1. Inspect repo status and read the relevant docs (`docs/quickstart.md`, `docs/contracts.md`, and `docs/operator-guide.md` for launches).
2. Validate specs/queues offline before launch.
3. Render payloads and run a dry controller tick without confirmation flags.
4. For live work, record initial inventory, queue path, events path, manifest root, archive root, and cleanup plan.
5. Launch through `rpr-controller` rather than ad hoc scripts.
6. Reap/archive before deleting pods.
7. Verify final pod/volume inventory and record any intentionally retained resources.

## Safety flags

| Action | Command family | Required approval/flags |
|---|---|---|
| Launch new pods or start stopped pods | `rpr controller`, `rpr launch launch/start`, archive sync pod launch | `--confirm-spend`; controller launches also require budgeted `--max-concurrent` / `--max-launches-per-tick` when raising defaults |
| Stop a pod directly | `rpr launch stop`, `rpr reap`, `rpr archive` cleanup | `--confirm-stop` |
| Delete a pod directly | `rpr launch delete`, `rpr reap`, `rpr archive` cleanup | `--confirm-delete` or `--confirm-delete-pod`, depending on command |
| Controller cleanup | `rpr controller tick/loop` | `--confirm-cleanup` |
| Temporary volume deletion | controller/reaper | cleanup approval plus `--confirm-delete-temp-volumes` or `--confirm-delete-volume` |
| Archive upload | `rpr archive`, controller promotion | `--confirm-sync`; controller promotion also accepts `--confirm-archive-sync` |
| Existing queue overwrite | `rpr controller init-queue` | `--force`; active lanes additionally require `--force-active-overwrite` after reconciliation |

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

## Collaborator-machine readiness checklist

Before proposing a live command for someone else's machine, confirm:

- The repo clone or downstream project can run `uv run rpr --help`.
- The spec validates with `uv run rpr validate spec --path <spec>`.
- A unique queue path and events path are chosen for this run.
- The worker artifact contract is satisfied and the worker remains alive for SSH reaping.
- Required RunPod environment variable names are present locally without printing values: `RUNPOD_API_KEY` for live API calls, `RUNPOD_PUBLIC_KEY` or `~/.ssh/id_ed25519.pub` for SSH workers, and `RUNPOD_NETWORK_VOLUME_ID` only for specs that mount an existing network volume.
- If a local env file is used, prefer the ignored filename `runpod-local-vars` and pass it before grouped subcommands, for example `uv run rpr controller --env-file runpod-local-vars tick ...`.
- Initial inventory commands have been run or explicitly deferred: `uv run rpr launch --env-file runpod-local-vars list pods`, `uv run rpr launch --env-file runpod-local-vars list network-volumes`, and `uv run rpr launch --env-file runpod-local-vars list templates`.
- A budget line has been recorded before approval: maximum concurrent pods, expected GPU/cloud price source, planned runtime, and maximum spend. Use rendered payloads plus the current RunPod UI/pricing because exact pre-launch prices are account/time dependent.
- The proposed command includes confirmation flags only after explicit approval of both the budget and the command.

## Review expectations

For nontrivial changes, request a bounded review focused on:

- generic abstraction and absence of downstream project coupling;
- destructive/spend confirmation gates;
- secret redaction and artifact hygiene;
- tests/docs updated with the changed behavior.
