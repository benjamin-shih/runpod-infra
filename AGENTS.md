# AGENTS.md — runpod-research

This repo contains reusable RunPod research lifecycle tooling. Before modifying code or operating live resources, read `docs/agent-usage.md`. For collaborator setup or a first run from a new machine, also read `docs/quickstart.md` and `docs/contracts.md`.

## Core rules

- Keep the package generic; project-specific sweep renderers, image policies, model names, and archive destinations belong in downstream repos or `examples/`.
- Do not print, commit, or infer credential values. Use shell environment variables or ignored local credential files only.
- If a downstream run spec expects `RUNPOD_HF_TOKEN` but the local credential file
  only defines `HF_TOKEN`, map it before launch, for example
  `RUNPOD_HF_TOKEN="$HF_TOKEN" ...`, or add a local-only alias in the ignored env
  file. Never print either token value.
- Do not run billable or destructive RunPod actions without explicit user approval and the required confirmation flags.
- Prefer offline validation (`make validate`, CLI help, spec/queue validation, render/init/dry-run tick) before any live smoke.
- Keep generated queues, manifests, archives, logs, checkpoints, and bulky artifacts out of git.

## Validation

Run `make validate` before committing changes. If it cannot run, record the exact blocker and the narrow checks that did run.

## GPT-Pro Review Passes

For non-trivial, risky, or review-heavy work, use GPT-Pro/GPT-0Pro review passes as an external critique loop when the ChatGPT Pro sidecar is available: ask for critique, apply fixes locally, and rerun only when the follow-up materially improves quality.

Guardrails: keep GPT-Pro advisory only; do not send secrets, credentials, private account pages, or unapproved proprietary/raw data; do not ask it to operate financial/trading sites or place trades. The local agent remains accountable for implementation, verification, commits, pushes, and final synthesis.
