# Operator guide

For a fresh-machine walkthrough, start with `docs/quickstart.md`. This guide focuses on routine operation after setup.

## Offline validation

```bash
uv run rpr validate spec --path examples/specs/stateless-smoke.json
uv run rpr launch render --spec examples/specs/stateless-smoke.json
uv run rpr controller init-queue \
  --spec examples/specs/stateless-smoke.json \
  --queue build/runpod-queues/smoke/queue.json
uv run rpr validate queue --path build/runpod-queues/smoke/queue.json
uv run rpr controller list --queue build/runpod-queues/smoke/queue.json
uv run rpr controller tick \
  --queue build/runpod-queues/smoke/queue.json \
  --events-path build/runpod-queues/smoke/events.jsonl
```

The final tick is a dry run unless `--confirm-spend` is present.

## Live launch pattern

1. Confirm the spec, worker image, artifact root, and cleanup plan.
2. Record current pod and network-volume inventory with `uv run rpr launch --env-file runpod-local-vars list pods` and `uv run rpr launch --env-file runpod-local-vars list network-volumes`.
3. Inspect available templates or rendered payloads for image, GPU type, cloud type, volume, and disk settings with `uv run rpr launch --env-file runpod-local-vars list templates` and `uv run rpr launch render --spec <spec>`.
4. Record a budget line before approval: maximum concurrent pods, expected GPU/cloud price source from current RunPod UI/pricing, planned runtime, and maximum spend.
5. Initialize a unique queue path for this run. Do not overwrite an existing queue unless it has been archived or reconciled.
6. Run a dry controller tick without confirmation flags.
7. Run the controller loop with `--confirm-spend`, explicit launch caps, and, when safe, `--confirm-cleanup` only after budget and command approval.
8. Inspect aggregates and status-cache outputs.
9. Confirm final inventory and retained resources.

Example:

```bash
uv run rpr-controller --env-file runpod-local-vars loop \
  --queue build/runpod-queues/my-run/queue.json \
  --events-path build/runpod-queues/my-run/events.jsonl \
  --archive-root artifacts/runpod-lifecycle/sweeps \
  --max-concurrent 2 \
  --max-launches-per-tick 1 \
  --confirm-spend \
  --confirm-cleanup \
  --unreachable-grace-seconds 1800
```

Confirmed controller launches default to `--max-concurrent 1 --max-launches-per-tick 1`. Increase those only when the recorded budget and approval cover the higher concurrency.

## Long-running controller daemon

Use the foreground `loop` command when an operator or process supervisor is already managing the run. Use the packaged daemon helper only when you want this repo to start a background loop and write logs under `build/runpod-daemons/`.

```bash
uv run rpr controller daemon-start \
  --name my-run \
  --queue build/runpod-queues/my-run/queue.json \
  --events-path build/runpod-queues/my-run/events.jsonl \
  --max-concurrent 2 \
  --max-launches-per-tick 1 \
  --confirm-spend

uv run rpr controller daemon-status --name my-run
uv run rpr controller daemon-stop --name my-run
```

Add cleanup/archive flags to `daemon-start` only after the same approvals required for the foreground loop.

## Run card before launch

Create a small run README in the downstream project before live launch so another collaborator can recover the intent, queue, image, storage mode, and archive destination.

```bash
uv run rpr run-card create \
  --experiment-dir experiments/2026-05-17_my-run \
  --title "my-run" \
  --spec configs/runpod/my-run.json \
  --queue build/runpod-queues/my-run/queue.json \
  --image runpod/pytorch:2.4.0 \
  --storage-mode stateless \
  --archive-subdir my-project/my-run/2026-05-17
```

## Manual one-shot reap

Use this only for a pod launched by this tooling and present in the launch manifests.

Dry plan:

```bash
uv run rpr reap --env-file runpod-local-vars \
  --pod-id <pod-id> \
  --manifest-root build/runpod-launch-manifests \
  --archive-root artifacts/runpod-lifecycle/sweeps \
  --status-cache build/runpod-monitor/status-cache \
  --events-path build/runpod-lifecycle/events.jsonl \
  --dry-run
```

After approval, add cleanup flags as appropriate:

- `--confirm-stop --confirm-delete-pod` for pod cleanup.
- `--delete-success-volume --confirm-delete-volume` only for lane-owned temporary volumes.
- `--force-cleanup-unreachable` only after the unreachable grace period and explicit approval.

## Archive promotion

Use archive promotion only after local artifacts are complete and useful outside git.

```bash
uv run rpr-archive --env-file runpod-local-vars \
  --local-path artifacts/runpod-lifecycle/sweeps/my-sweep \
  --remote-subdir my-project/my-sweep/2026-05-17 \
  --sync-pod-id <existing-sync-pod> \
  --confirm-sync
```

`rpr-controller` accepts either `--confirm-sync` or `--confirm-archive-sync` for promotion. Launching a sync pod through the controller is billable and also requires controller `--confirm-spend`; otherwise provide controller `--archive-sync-pod-id` for an already-running sync pod. The standalone archive CLI uses `--sync-pod-id` for the same existing-pod case. The packaged default sync spec is generic, and a copy is shown in `examples/specs/archive-sync-pod.json` for downstream customization.

## Troubleshooting quick reference

1. Start local:
   - `uv run rpr controller list --queue <queue>`
   - `uv run rpr validate queue --path <queue>`
   - `uv run rpr dashboard --offline --once`
   - Inspect `<events-path>` and lane `last_error`.
2. Unresolved `${RUNPOD_*}` after render:
   - Open the rendered `manifest.json` and ensure `unresolved_env` is empty before live launch.
   - Set missing values in shell or `runpod-local-vars`; do not paste values into chat.
3. SSH/reap failures:
   - Confirm the spec exposes `22/tcp`, `RUNPOD_PUBLIC_KEY` was injected, and the worker is still alive.
   - For stateless pods launched from an image, a missing public IP, empty port
     mappings, or `pod not ready` status can be transient while RunPod downloads
     and initializes the image. Treat it as a startup-grace condition until logs
     or repeated readiness checks show the pod is no longer progressing.
   - If the pod is unrecoverable, use `--force-cleanup-unreachable` only after explicit approval and a recorded cleanup plan.
4. Missing artifacts:
   - For `DONE`, ensure `status.json`, `lane_config.json`, and `metrics_all.csv` exist in the discovered run root.
   - Use `--include-checkpoints` only when large checkpoint copy is intended.
5. Capacity unavailable:
   - Leave the lane queued and retry later or change GPU/cloud type after approval.
6. `LAUNCH-UNKNOWN` lane:
   - Treat this as a manual stop condition: the controller blocks additional launches until reconciliation.
   - Run inventory commands and search for a pod matching the intended sweep/job/pod name.
   - If a pod exists, either manually record/reconcile it before reaping or stop/delete it after approval.
   - If no pod exists, reinitialize a new queue path or edit/requeue the lane only after documenting the reconciliation.

## Recovery notes

- Re-run the same controller loop after interruption; it recovers durable queue states.
- `LAUNCHING` lanes without pod ids become `LAUNCH-UNKNOWN` and are not relaunched automatically.
- `SYNCING` lanes are reaped again from persisted launch manifests.
- Unknown or protected volumes are not deleted by default.
