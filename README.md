# runpod-research

Reusable, dependency-light RunPod lifecycle tooling for research sweeps.

This repository packages the controller pattern used in prior research work as a standalone Python package named `runpod_research` with CLI entry points prefixed by `rpr`. It is designed for project repos to import or run without copying controller internals.

## What it provides

- Render one RunPod pod payload per job from a JSON sweep spec.
- Maintain a durable JSON lane queue with restart-safe states.
- Launch queued lanes only with explicit spend confirmation.
- Reap terminal pods by copying artifacts, verifying required files, writing checksums/status cache, and then stopping/deleting pods only with explicit cleanup confirmation.
- Optionally promote local archives to a configured archive volume via an SSH/rsync sync pod.
- Validate specs/queues offline and generate run-card READMEs for future recovery.

## Quickstart

From a fresh machine:

```bash
git clone https://github.com/benjamin-shih/runpod-infra.git
cd runpod-infra
uv sync --extra dev
make validate
```

Then run the offline smoke path:

```bash
uv run rpr validate spec --path examples/specs/stateless-smoke.json
uv run rpr launch render --spec examples/specs/stateless-smoke.json
uv run rpr controller init-queue \
  --spec examples/specs/stateless-smoke.json \
  --queue build/runpod-queues/smoke/queue.json
uv run rpr controller tick \
  --queue build/runpod-queues/smoke/queue.json \
  --events-path build/runpod-queues/smoke/events.jsonl
```

The render/init/tick path above is offline and does not require RunPod credentials. Live launch, cleanup, volume deletion, and archive upload paths require explicit confirmation flags. See `docs/quickstart.md` for collaborator setup, downstream-project installation, live-run commands, and an agent launch brief template.

## Worker artifact contract

By default each lane should write a run root under:

```text
<RPR_ARTIFACT_ROOT>/<RPR_SWEEP_NAME>/<RPR_JOB_NAME>/<timestamp>/
```

Required lane artifacts are:

- `status.json`
- `lane_config.json`
- `metrics_all.csv`

The reaper copies optional generic logs, metrics, outputs, evaluations, code snapshots, and checkpoint metadata. Large checkpoints are excluded unless `--include-checkpoints` is passed.

Because this first controller retrieves artifacts over SSH from the pod, stateless workers must keep the container alive after writing terminal artifacts (for example with a final sleep or service loop) until the controller reaps it. Workers that exit immediately may lose pod-local artifacts before the controller can copy them.

## Safety defaults

- No live spend without `--confirm-spend`.
- No pod cleanup without `--confirm-cleanup` / reaper cleanup flags.
- No temporary volume deletion unless explicitly requested.
- No archive upload without `--confirm-sync` (`rpr-controller` also accepts `--confirm-archive-sync`).
- Secrets are read from the shell environment or an explicit `--env-file` path and redacted from manifests and dry-render payload files.

## Docs

- `docs/quickstart.md` — fresh-machine collaborator setup and first run path.
- `docs/agent-usage.md` — required first-read for future agents.
- `docs/contracts.md` — spec, queue, worker artifact, and archive contracts.
- `docs/operator-guide.md` — common offline and live operation flow.
- `docs/project-integration.md` — how downstream research repos should consume this package.
