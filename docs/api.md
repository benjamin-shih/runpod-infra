# Python API

The primary supported interface is the `rpr` CLI. Downstream projects may import a small stable helper surface when they need offline validation or queue inspection from Python.

## Stable imports

```python
from runpod_research.queue import LaneRecord, LaneState, QueueStore
from runpod_research.schema import validate_queue_file, validate_spec_file, validate_spec_payload
```

These helpers are dependency-free and safe for offline tooling.

## Internal modules

Modules such as `launcher`, `controller`, `lifecycle`, `reaper`, `archive_cli`, and `archive_sync` are production code, but their Python-level APIs are not yet a compatibility contract for downstream projects. Prefer invoking them through `uv run rpr ...` unless you are contributing to this infrastructure repo.

## Compatibility policy

- CLI commands and documented JSON contracts in `docs/contracts.md` are the main compatibility surface.
- Queue files should be validated with `uv run rpr validate queue --path <queue>` before reuse.
- Sweep specs should be validated with `uv run rpr validate spec --path <spec>` before render or launch.
- If a downstream project needs a new stable import, add it to this document and cover it with tests before relying on it.
