# Migration note

This repository was extracted from a project-local RunPod controller. The standalone package intentionally removes project defaults and keeps only the reusable lifecycle mechanics.

Kept as generic core:

- JSON sweep spec rendering.
- Durable queue state machine.
- Controller tick/loop/daemon helpers.
- One-shot reaper and artifact verification.
- Archive sync over SSH/rsync.
- Offline validators and run-card generation.

Not kept as generic defaults:

- Project-specific sweep renderers.
- Paper/figure-specific launch scripts.
- Model-family image build policy.
- Project archive volume names and paths.
- Model/layer/sparsity dashboard fields.

Downstream projects should reintroduce their own adapters in their own repo or under examples, not in `src/runpod_research`.
