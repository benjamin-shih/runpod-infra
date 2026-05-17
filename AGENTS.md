# AGENTS.md — runpod-research

This repo contains reusable RunPod research lifecycle tooling. Before modifying code or operating live resources, read `docs/agent-usage.md`.

## Core rules

- Keep the package generic; project-specific sweep renderers, image policies, model names, and archive destinations belong in downstream repos or `examples/`.
- Do not print, commit, or infer credential values. Use shell environment variables or ignored local credential files only.
- Do not run billable or destructive RunPod actions without explicit user approval and the required confirmation flags.
- Prefer offline validation (`make validate`, CLI help, spec/queue validation) before any live smoke.
- Keep generated queues, manifests, archives, logs, checkpoints, and bulky artifacts out of git.

## Validation

Run `make validate` before committing changes. If it cannot run, record the exact blocker and the narrow checks that did run.
