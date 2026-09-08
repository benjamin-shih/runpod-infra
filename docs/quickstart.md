# Quickstart for collaborators and agents

Use this guide from a fresh machine when you want to run your own RunPod-backed research sweep with `runpod-research`. The source repository is named `runpod-infra`, while the Python package is `runpod-research` and imports as `runpod_research`.

## Who can use this repo?

A collaborator can use it from another machine if they have:

- Git access to `https://github.com/benjamin-shih/runpod-infra.git`.
- Python 3.11+ and `uv` installed.
- A RunPod account/token available in their local shell for live API actions.
- An SSH public key available as `RUNPOD_PUBLIC_KEY` or at `~/.ssh/id_ed25519.pub`.
- A worker image/spec that writes the required lane artifacts described in `docs/contracts.md`.

Agents can use it safely if they read `AGENTS.md` and `docs/agent-usage.md` first, validate specs offline, and ask before passing live confirmation flags.

## 1. Install from a fresh clone

```bash
git clone https://github.com/benjamin-shih/runpod-infra.git
cd runpod-infra
uv sync --extra dev
make validate
```

This is the recommended first setup because it gives collaborators the examples, tests, docs, and local validation target.

## 2. Optional: use it from a downstream research repo

From another project, either call the cloned checkout directly or add the Git package to that project:

```bash
uv add "runpod-research @ git+https://github.com/benjamin-shih/runpod-infra.git"
uv run rpr --help
```

For one-off CLI use without adding a dependency:

```bash
uvx --from git+https://github.com/benjamin-shih/runpod-infra.git rpr --help
```

Keep project-specific sweep specs, worker images, run cards, and result archives in the downstream project, not in this infrastructure repo.

Default new GPU workers to stateless compute. Keep the persistent archive on
the controller or a separate sync/storage endpoint, so its volume placement
does not constrain GPU availability. A stateless spec uses `volumeInGb: 0`, no
`networkVolumeId`, and enough container-local disk for the model, inputs, caches,
and results. Retain a volume and its placement when the task explicitly calls
for that mounted storage.

## 3. Prepare local credentials without printing them

For live API actions, provide `RUNPOD_ACCOUNT_API_KEY` or `RUNPOD_API_KEY`. The launcher also needs `RUNPOD_PUBLIC_KEY` for SSH access to workers, unless `~/.ssh/id_ed25519.pub` exists and can be loaded automatically. Optional archive sync pods that mount a network volume need `RUNPOD_NETWORK_VOLUME_ID` or an explicit spec value.

Inside an existing Pod, `RUNPOD_API_KEY` can be an automatically supplied
pod-scoped key. Set `RUNPOD_ACCOUNT_API_KEY` to your account key to override it
explicitly; all API command entrypoints, including the dashboard, prefer this
variable. It can also be supplied as `RUNPOD_ACCOUNT_API_KEY=<set-locally>` in
the ignored env file below. Existing shell variables win over env-file values,
so an env file that sets only `RUNPOD_API_KEY` will not replace an inherited
pod-scoped key. Both key names are redacted from manifests and errors.

The client identifies itself as `runpod-research/0.1.0`. A 403 `edge_rejection`
with error code 1010 means the HTTP client was rejected before authentication;
401 `authentication` and other 403 `authorization` failures instead require
checking the account key and its permissions. Requests are not automatically
retried, including resource-creation and deletion requests.

Use either shell variables:

```bash
export RUNPOD_API_KEY=<set-locally>
export RUNPOD_PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)"
# Optional, only for specs that mount an existing network volume:
export RUNPOD_NETWORK_VOLUME_ID=<set-locally>
```

Or use the ignored local env-file name already covered by `.gitignore`:

```bash
cat > runpod-local-vars <<'EOF'
RUNPOD_API_KEY=<set-locally>
# Omit RUNPOD_PUBLIC_KEY to use ~/.ssh/id_ed25519.pub automatically.
# RUNPOD_PUBLIC_KEY=<set-locally>
# RUNPOD_NETWORK_VOLUME_ID=<optional-existing-network-volume>
EOF
```

Do not commit credential files or paste values into chat. For grouped commands, place `--env-file` before the subcommand, for example `uv run rpr controller --env-file runpod-local-vars loop ...` or `uv run rpr launch --env-file runpod-local-vars list pods`.

## 4. Validate and render offline

Start with the included smoke spec:

```bash
uv run rpr validate spec --path examples/specs/stateless-smoke.json
uv run rpr launch render --spec examples/specs/stateless-smoke.json
```

Rendering writes redacted payload/manifests under `build/runpod-launch-manifests/` and does not call RunPod.

For a project worker, arrange input staging before using the worker command.
An established SSH bootstrap pattern is to launch an SSH-capable image/template,
wait for SSH readiness, transfer the pinned source snapshot and required input
directories, and then start the project worker. Reuse the project's existing
bootstrap helper; `rpr launch` does not upload local files automatically. Model
downloads and disposable caches belong on the worker's local disk. Include
image startup and input transfer in the budget and startup grace period.

## 5. Create a queue

```bash
uv run rpr controller init-queue \
  --spec examples/specs/stateless-smoke.json \
  --queue build/runpod-queues/smoke/queue.json

uv run rpr validate queue --path build/runpod-queues/smoke/queue.json
uv run rpr controller list \
  --queue build/runpod-queues/smoke/queue.json
```

Use a unique queue path per run and one controller per queue. Do not have multiple agents or machines write the same queue JSON. `init-queue` refuses to overwrite an existing queue unless `--force` is supplied; queues with active lanes require `--force-active-overwrite` only after manual reconciliation.

## 6. Dry-run one controller tick

```bash
uv run rpr controller tick \
  --queue build/runpod-queues/smoke/queue.json \
  --events-path build/runpod-queues/smoke/events.jsonl
```

Without `--confirm-spend`, queued lanes are reported as would-launch and no pods are created.

## 7. Launch a live run only after approval

Before launch, record inventory and inspect the rendered payloads for GPU type, cloud type, image, disk/volume settings, artifact root, and expected spend. The CLI records `costPerHr` after launch, but before launch the operator must estimate cost from the rendered payload's GPU type/cloud type and current RunPod pricing or account UI. Confirmed controller launches default to one active lane and one new pod per tick; raise `--max-concurrent` and `--max-launches-per-tick` only after the budget line covers that concurrency.

```bash
uv run rpr launch --env-file runpod-local-vars list pods
uv run rpr launch --env-file runpod-local-vars list network-volumes
uv run rpr launch --env-file runpod-local-vars list templates
```

Then check the queue dry run from step 6 and record a budget line such as `max pods × expected $/hr × planned hours = max spend`. Ask for explicit approval of both the budget and the live loop command. After approval, run:

```bash
uv run rpr controller --env-file runpod-local-vars loop \
  --queue build/runpod-queues/smoke/queue.json \
  --events-path build/runpod-queues/smoke/events.jsonl \
  --archive-root artifacts/runpod-lifecycle/sweeps \
  --max-concurrent 2 \
  --max-launches-per-tick 1 \
  --confirm-spend \
  --confirm-cleanup \
  --unreachable-grace-seconds 1800
```

This launches queued lanes within the explicit caps, reaps terminal pods, writes local archives/checksums, and stops/deletes pods only when cleanup is confirmed. Add `--confirm-delete-temp-volumes` only for lane-owned temporary volumes that should be deleted.

## 8. Monitor without wasting agent context

Prefer local controller artifacts over live chat polling:

```bash
uv run rpr controller list --queue build/runpod-queues/smoke/queue.json
uv run rpr dashboard --once --offline
uv run rpr dashboard --env-file runpod-local-vars --once
```

Use the offline dashboard when you only need local manifests/status-cache. The API dashboard call requires local RunPod credentials.

## 9. Archive promotion, if needed

Promote completed local archives only after verifying they are worth keeping outside git:

```bash
uv run rpr archive --env-file runpod-local-vars \
  --local-path artifacts/runpod-lifecycle/sweeps/stateless-smoke \
  --remote-subdir my-project/stateless-smoke/$(date -u +%Y%m%dT%H%M%SZ) \
  --sync-pod-id <existing-sync-pod-id> \
  --confirm-sync
```

If the controller launches a sync pod for you, that is billable and requires `--confirm-spend`. The packaged default sync spec is generic; copy `examples/specs/archive-sync-pod.json` into your downstream project before customizing GPU, volume, or image policy.

The sync pod may mount a persistent network volume even when every compute
worker is stateless. Its volume ID and region belong to the sync spec, not to
the compute spec. Keep stateless workers alive until the SSH reaper has copied
their terminal artifacts; archive promotion then operates on that local copy.

## 10. Worker contract checklist

Before launching a project-specific worker, confirm it:

- Uses `RPR_ARTIFACT_ROOT`, `RPR_SWEEP_NAME`, and `RPR_JOB_NAME` to choose a run root.
- Writes `status.json`, `lane_config.json`, and `metrics_all.csv` before it is considered terminal.
- Keeps the container alive after writing terminal artifacts until the SSH reaper copies them, unless the project has a separate persistent artifact backend.
- Avoids printing secrets in logs or writing credential-bearing files to the artifact root.

## Agent launch brief template

Give a collaborator's agent a bounded brief like this:

```text
Read AGENTS.md, docs/agent-usage.md, docs/quickstart.md, and docs/contracts.md in the runpod-infra repo. Work from <project-root>. Do not run live RunPod actions until I approve the exact command with confirmation flags. Validate the spec and queue offline, render payloads, initialize a unique queue, and report the dry-run tick result plus the proposed live loop command including launch caps. Keep generated build/artifact outputs out of git and never print credential values.
```
