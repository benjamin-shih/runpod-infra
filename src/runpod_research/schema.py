"""Dependency-free validators for RunPod sweep specs and lane queues."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .queue import LaneState


Issue = str

VALID_STORAGE_MODES = frozenset({"stateless", "temp-volume", "master-volume"})
VALID_QUEUE_STATES = frozenset(state.value for state in LaneState)


class SchemaValidationError(ValueError):
    """Raised when a payload cannot be validated as JSON-backed data."""


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    if not slug:
        raise SchemaValidationError("empty name cannot be slugified")
    return slug


def validate_spec_payload(payload: Any) -> list[Issue]:
    """Return validation issues for a RunPod sweep spec payload."""
    if not isinstance(payload, dict):
        return ["spec payload must be an object"]

    issues: list[Issue] = []
    for field in ("schema_version", "name", "remote_artifact_root", "defaults", "jobs"):
        if field not in payload:
            issues.append(f"missing required field: {field}")

    if "schema_version" in payload and payload["schema_version"] != 1:
        issues.append("schema_version must be 1")

    name = payload.get("name")
    if "name" in payload and not _is_non_empty_string(name):
        issues.append("name is required")

    root = payload.get("remote_artifact_root")
    if "remote_artifact_root" in payload and (
        not isinstance(root, str) or not root.startswith("/")
    ):
        issues.append("remote_artifact_root must be an absolute remote path")

    defaults = payload.get("defaults")
    if "defaults" in payload and not isinstance(defaults, dict):
        issues.append("defaults must be an object")

    _validate_storage_mode(payload, "storage_mode", issues)
    if isinstance(defaults, dict):
        _validate_storage_mode(defaults, "defaults.storage_mode", issues)

    jobs = payload.get("jobs")
    if "jobs" not in payload:
        return issues
    if not isinstance(jobs, list):
        issues.append("jobs must be a list")
        return issues
    if not jobs:
        issues.append("jobs must not be empty")
        return issues

    seen_slugs: set[str] = set()
    for index, job in enumerate(jobs):
        path = f"jobs[{index}]"
        if not isinstance(job, dict):
            issues.append(f"{path} must be an object")
            continue

        _validate_storage_mode(job, f"{path}.storage_mode", issues)
        job_name = job.get("name")
        if not _is_non_empty_string(job_name):
            issues.append(f"{path}.name is required")
            continue

        try:
            slug = slugify(job_name)
        except SchemaValidationError:
            issues.append(f"{path}.name cannot be slugified")
            continue

        if slug in seen_slugs:
            issues.append(f"duplicate slugified job name: {slug}")
        else:
            seen_slugs.add(slug)

    return issues


def validate_queue_payload(payload: Any) -> list[Issue]:
    """Return validation issues for a RunPod lifecycle queue payload."""
    issues: list[Issue] = []
    if isinstance(payload, dict):
        if "schema_version" in payload and payload["schema_version"] != 1:
            issues.append("schema_version must be 1")
        if "lanes" not in payload:
            issues.append("lanes must be present")
            return issues
        lanes = payload["lanes"]
    elif isinstance(payload, list):
        lanes = payload
    else:
        return ["queue payload must be an object or list"]

    if not isinstance(lanes, list):
        issues.append("lanes must be a list")
        return issues

    for index, lane in enumerate(lanes):
        path = f"lanes[{index}]"
        if not isinstance(lane, dict):
            issues.append(f"{path} must be an object")
            continue

        for field in ("lane_name", "sweep_name", "spec_path"):
            if not _is_non_empty_string(lane.get(field)):
                issues.append(f"{path}.{field} is required")

        if "job_index" not in lane:
            issues.append(f"{path}.job_index is required")
        elif type(lane["job_index"]) is not int:
            issues.append(f"{path}.job_index must be an integer")

        state = lane.get("state")
        if state is not None and state not in VALID_QUEUE_STATES:
            issues.append(f"{path}.state is invalid: {state}")

    return issues


def validate_spec_file(path: Path) -> list[Issue]:
    return _validate_json_file(path, validate_spec_payload)


def validate_queue_file(path: Path) -> list[Issue]:
    return _validate_json_file(path, validate_queue_payload)


def _validate_json_file(path: Path, validator: Callable[[Any], list[Issue]]) -> list[Issue]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError:
        return [f"{path} does not exist"]
    except OSError as exc:
        return [f"could not read {path}: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"{path} is not valid JSON: {exc}"]
    return validator(payload)


def _validate_storage_mode(payload: dict[str, Any], path: str, issues: list[Issue]) -> None:
    if "storage_mode" not in payload:
        return
    value = payload["storage_mode"]
    if value not in VALID_STORAGE_MODES:
        issues.append(f"unsupported storage_mode at {path}: {value}")


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
