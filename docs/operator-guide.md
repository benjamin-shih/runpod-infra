# Operator guide

For a fresh-machine walkthrough, start with `docs/quickstart.md`. This guide focuses on routine operation after setup.

## Offline validation

```bash
uv run rpr validate spec --path examples/specs/stateless-smoke.json
uv run rpr launch render --spec examples/specs/stateless-smoke.json
uv run rpr controller init-queue --spec examples/specs/stateless-smoke.json --queue build/runpod-queues/smoke/queue.json
uv run rpr controller list --queue build/runpod-queues/smoke/queue.json
```

## Live launch pattern

1. Confirm the spec, worker image, artifact root, and cleanup plan.
2. Record current pod and network-volume inventory with `uv run rpr launch --env-file runpod-local-vars list pods` and `uv run rpr launch --env-file runpod-local-vars list network-volumes`.
3. Inspect available templates or rendered payloads for image, GPU type, cloud type, volume, and disk settings with `uv run rpr launch --env-file runpod-local-vars list templates` and `uv run rpr launch render --spec <spec>`.
4. Record a budget line before approval: maximum concurrent pods, expected GPU/cloud price source from current RunPod UI/pricing, planned runtime, and maximum spend.
5. Initialize a unique queue path for this run.
6. Run a dry controller tick without confirmation flags.
7. Run the controller loop with `--confirm-spend` and, when safe, `--confirm-cleanup` only after budget and command approval.
8. Inspect aggregates and status-cache outputs.
9. Confirm final inventory and retained resources.

Example:

```bash
uv run rpr-controller --env-file runpod-local-vars loop \
  --queue build/runpod-queues/my-run/queue.json \
  --events-path build/runpod-queues/my-run/events.jsonl \
  --archive-root artifacts/runpod-lifecycle/sweeps \
  --confirm-spend \
  --confirm-cleanup \
  --unreachable-grace-seconds 1800
```

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

## Recovery notes

- Re-run the same controller loop after interruption; it recovers durable queue states.
- `LAUNCHING` lanes without pod ids are requeued.
- `SYNCING` lanes are reaped again from persisted launch manifests.
- Unknown or protected volumes are not deleted by default.
