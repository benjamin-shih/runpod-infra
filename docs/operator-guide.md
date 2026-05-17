# Operator guide

## Offline validation

```bash
uv run rpr validate spec --path examples/specs/stateless-smoke.json
uv run rpr launch render --spec examples/specs/stateless-smoke.json
uv run rpr controller init-queue --spec examples/specs/stateless-smoke.json --queue build/runpod-queues/smoke/queue.json
uv run rpr controller list --queue build/runpod-queues/smoke/queue.json
```

## Live launch pattern

1. Confirm the spec, worker image, artifact root, and cleanup plan.
2. Record current pod and network-volume inventory.
3. Initialize a queue.
4. Run the controller loop with `--confirm-spend` and, when safe, `--confirm-cleanup`.
5. Inspect aggregates and status-cache outputs.
6. Confirm final inventory and retained resources.

Example:

```bash
uv run rpr-controller loop \
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
uv run rpr-archive \
  --local-path artifacts/runpod-lifecycle/sweeps/my-sweep \
  --remote-subdir my-project/my-sweep/2026-05-17 \
  --sync-pod-id <existing-sync-pod> \
  --confirm-sync
```

`rpr-controller` accepts either `--confirm-sync` or `--confirm-archive-sync` for promotion. Launching a sync pod is billable and also requires controller `--confirm-spend`; otherwise provide `--archive-sync-pod-id` / `--sync-pod-id` for an already-running sync pod. The packaged default sync spec is generic, and a copy is shown in `examples/specs/archive-sync-pod.json` for downstream customization.

## Recovery notes

- Re-run the same controller loop after interruption; it recovers durable queue states.
- `LAUNCHING` lanes without pod ids are requeued.
- `SYNCING` lanes are reaped again from persisted launch manifests.
- Unknown or protected volumes are not deleted by default.
