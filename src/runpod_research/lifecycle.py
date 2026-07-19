"""Reduced Phase A RunPod lifecycle helpers.

This module intentionally implements only the one-shot reaper primitives:
launch-manifest guarding, status parsing, artifact verification, status-cache
updates, and local audit events. Scheduling, queues, S3 archive transport,
Docker-image work, and aggregation are deliberately out of scope.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_REQUIRED_ARTIFACTS = ("status.json", "lane_config.json", "metrics_all.csv")
DEFAULT_OPTIONAL_PATTERNS = (
    "bootstrap.log",
    "bootstrap_timing.json",
    "run_config.json",
    "training_summary.json",
    "checkpoint_*.json",
    "git_*.txt",
    "upstream_commit.txt",
    "python_version.txt",
    "pip_freeze.txt",
    "code_snapshot.bundle",
    "logs/**/*.log",
    "logs/**/*.txt",
    "metrics/**/*.csv",
    "metrics/**/*.json",
    "metrics/**/*.jsonl",
    "outputs/**/*.csv",
    "outputs/**/*.json",
    "outputs/**/*.jsonl",
    "outputs/**/*.md",
    "outputs/**/*.npy",
    "outputs/**/*.npz",
    "outputs/**/*.txt",
    "training/**/*.log",
    "training/**/*.csv",
    "training/**/*.json",
    "evals/**/*.csv",
    "evals/**/*.json",
    "evals/**/*.jsonl",
    "evals/**/*.log",
)
DEFAULT_CHECKPOINT_PATTERNS = (
    "checkpoints/**/*.pt",
    "checkpoints/**/*.pth",
    "checkpoints/**/*.safetensors",
    "training/checkpoints/**/*",
    "training/**/checkpoints/**/*",
    "training/final_adapter/*",
    "training/final_adapter/**/*",
    "outputs/training/checkpoints/**/*",
    "outputs/training/final_adapter/*",
    "outputs/training/final_adapter/**/*",
)


class LifecycleError(RuntimeError):
    """Base error for lifecycle failures."""


class UnknownPodError(LifecycleError):
    """Raised when a pod is not present in a known launch manifest."""


class ArtifactVerificationError(LifecycleError):
    """Raised when synced artifacts are incomplete."""


@dataclass(frozen=True)
class LaunchManifestEntry:
    """RunPod launch metadata for one pod."""

    pod_id: str
    pod_name: str
    job_name: str
    sweep_name: str
    manifest_path: Path
    spec_path: str = ""
    volume_id: str | None = None
    artifact_root: str | None = None


@dataclass(frozen=True)
class PodEndpoint:
    """SSH endpoint for a RunPod pod."""

    host: str
    port: int
    user: str = "root"

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


@dataclass(frozen=True)
class ReapDecision:
    """Lifecycle action plan for a lane status."""

    lane_status: str
    sync_artifacts: bool
    stop_pod: bool
    delete_pod: bool
    delete_volume: bool
    reason: str


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def append_event(path: Path, kind: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": utc_now(), "kind": kind, **fields}
    with path.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def _nested_response(job: dict[str, Any]) -> dict[str, Any]:
    response = job.get("response") or job.get("launch_response") or {}
    return response if isinstance(response, dict) else {}


def _nested_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("payload") or {}
    return payload if isinstance(payload, dict) else {}


def _entry_from_job(
    job: dict[str, Any],
    *,
    manifest_path: Path,
    spec_path: str,
    fallback_sweep: str,
) -> LaunchManifestEntry | None:
    response = _nested_response(job)
    payload = _nested_payload(job)
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    response_env = response.get("env") if isinstance(response.get("env"), dict) else {}
    env = {**response_env, **env}

    pod_id = job.get("pod_id") or job.get("podId") or response.get("id") or response.get("podId")
    if not isinstance(pod_id, str) or not pod_id:
        return None

    pod_name = job.get("pod_name") or payload.get("name") or response.get("name") or ""
    job_name = job.get("job_name") or env.get("RPR_JOB_NAME") or pod_name or pod_id
    sweep_name = env.get("RPR_SWEEP_NAME") or fallback_sweep
    volume_id = (
        job.get("volume_id")
        or job.get("networkVolumeId")
        or payload.get("networkVolumeId")
        or response.get("networkVolumeId")
    )
    artifact_root = env.get("RPR_ARTIFACT_ROOT")

    return LaunchManifestEntry(
        pod_id=pod_id,
        pod_name=str(pod_name),
        job_name=str(job_name),
        sweep_name=str(sweep_name),
        manifest_path=manifest_path,
        spec_path=spec_path,
        volume_id=str(volume_id) if volume_id else None,
        artifact_root=str(artifact_root) if artifact_root else None,
    )


def load_launch_manifest_index(root: Path) -> dict[str, LaunchManifestEntry]:
    """Build a pod-id index from RunPod launch manifests."""

    entries: dict[str, LaunchManifestEntry] = {}
    if not root.exists():
        return entries

    for path in sorted(root.rglob("launch_manifest.json")):
        try:
            payload = read_json(path)
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("schema_version", 1) != 1:
            continue

        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            spec_path = str(payload.get("spec_path") or "")
            fallback_sweep = Path(spec_path).stem if spec_path else path.parent.name
            for job in jobs:
                if not isinstance(job, dict):
                    continue
                entry = _entry_from_job(
                    job,
                    manifest_path=path,
                    spec_path=spec_path,
                    fallback_sweep=fallback_sweep,
                )
                if entry:
                    entries[entry.pod_id] = entry
            continue

        if "job_name" in payload or "pod_id" in payload:
            entry = _entry_from_job(
                payload,
                manifest_path=path,
                spec_path=str(payload.get("spec_path") or ""),
                fallback_sweep=path.parent.parent.name,
            )
            if entry:
                entries[entry.pod_id] = entry

    return entries


def require_launch_manifest_entry(root: Path, pod_id: str) -> LaunchManifestEntry:
    index = load_launch_manifest_index(root)
    try:
        return index[pod_id]
    except KeyError as exc:
        raise UnknownPodError(f"pod {pod_id!r} is not present in {root}") from exc


def endpoint_from_pod(
    pod: dict[str, Any],
    *,
    user: str = "root",
    override_host: str | None = None,
    override_port: int | None = None,
) -> PodEndpoint:
    if override_host and override_port:
        return PodEndpoint(host=override_host, port=override_port, user=user)

    host = pod.get("publicIp") or pod.get("ip")
    port: int | None = None

    port_mappings = pod.get("portMappings")
    if isinstance(port_mappings, dict):
        mapped = port_mappings.get("22") or port_mappings.get(22)
        if mapped is not None:
            port = int(mapped)

    runtime = pod.get("runtime") if isinstance(pod.get("runtime"), dict) else {}
    runtime_ports = runtime.get("ports") if isinstance(runtime.get("ports"), list) else []
    for item in runtime_ports:
        if not isinstance(item, dict):
            continue
        if str(item.get("privatePort")) == "22" and item.get("publicPort"):
            host = item.get("ip") or host
            port = int(item["publicPort"])
            break

    if not host or port is None:
        raise LifecycleError("pod does not expose an SSH endpoint for private port 22")
    return PodEndpoint(host=str(host), port=port, user=user)


def run_ssh(endpoint: PodEndpoint, ssh_key: Path, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ConnectionAttempts=1",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-i",
            str(ssh_key),
            "-p",
            str(endpoint.port),
            endpoint.target,
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def discover_remote_run_root(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    artifact_root: str,
    sweep_name: str,
    job_name: str,
) -> str:
    command = remote_run_root_discovery_script(
        artifact_root=artifact_root,
        sweep_name=sweep_name,
        job_name=job_name,
    )
    result = run_ssh(endpoint, ssh_key, command)
    remote_root = result.stdout.strip()
    if not remote_root:
        raise LifecycleError(
            "no status.json found under remote artifact root "
            f"{artifact_root.rstrip('/')} for sweep={sweep_name!r} job={job_name!r}"
        )
    return remote_root


def remote_run_root_discovery_script(*, artifact_root: str, sweep_name: str, job_name: str) -> str:
    """Return a remote shell script that finds the newest terminal run root.

    The controller is intentionally experiment-agnostic. It does not assume a
    fixed project subdirectory; it searches under the spec's `RPR_ARTIFACT_ROOT` for
    the conventional `<sweep>/<job>/<stamp>/status.json` shape at any depth.
    It must not fall back to job-name-only discovery, because job names are
    commonly reused across sweeps and a stale terminal run can then be reaped as
    if it belonged to the newly launched pod.
    """

    quoted_root = json.dumps(artifact_root.rstrip("/"))
    quoted_sweep = json.dumps(sweep_name)
    quoted_job = json.dumps(job_name)
    command = (
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "import json\n"
        f"base = Path({quoted_root})\n"
        f"sweep = {quoted_sweep}\n"
        f"job = {quoted_job}\n"
        "patterns = [\n"
        "    f'{sweep}/{job}/*/status.json',\n"
        "    f'*/{sweep}/{job}/*/status.json',\n"
        "    f'**/{sweep}/{job}/*/status.json',\n"
        "]\n"
        "terminal_statuses = {'DONE', 'FAILED'}\n"
        "seen = {}\n"
        "terminal = {}\n"
        "for pattern in patterns:\n"
        "    for path in base.glob(pattern):\n"
        "        if path.is_file():\n"
        "            try:\n"
        "                payload = json.loads(path.read_text())\n"
        "                status = str(payload.get('status', '')).upper()\n"
        "            except Exception:\n"
        "                status = ''\n"
        "            seen[path] = (path.stat().st_mtime, status)\n"
        "            if status in terminal_statuses:\n"
        "                terminal[path] = seen[path]\n"
        "if not seen:\n"
        "    print('')\n"
        "else:\n"
        "    candidates = terminal or seen\n"
        "    newest = max(candidates, key=lambda path: (candidates[path][0], str(path)))\n"
        "    print(newest.parent)\n"
        "PY"
    )
    return command


def volume_delete_blocker(
    *,
    volume_id: str | None,
    protected_volume_id: str | None = None,
    collector_pod_id: str | None,
    confirm_delete_collector: bool,
) -> str | None:
    if volume_id and protected_volume_id and volume_id == protected_volume_id:
        return "refusing to delete protected master network volume"
    if collector_pod_id and not confirm_delete_collector:
        return "collector pod is still attached; confirm collector deletion before deleting volume"
    return None


def read_remote_status(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    remote_run_root: str,
) -> dict[str, Any]:
    result = run_ssh(endpoint, ssh_key, f"cat {shlex.quote(remote_run_root.rstrip('/') + '/status.json')}")
    status = json.loads(result.stdout)
    if not isinstance(status, dict):
        raise LifecycleError("remote status.json did not contain an object")
    return status


def should_copy(relative_path: str, *, include_checkpoints: bool = False) -> bool:
    normalized = relative_path.strip("/")
    if normalized in DEFAULT_REQUIRED_ARTIFACTS:
        return True
    if any(fnmatch.fnmatch(normalized, pattern) for pattern in DEFAULT_OPTIONAL_PATTERNS):
        return True
    if include_checkpoints and any(
        fnmatch.fnmatch(normalized, pattern) for pattern in DEFAULT_CHECKPOINT_PATTERNS
    ):
        return True
    return False


def copy_artifacts_from_local(
    source: Path,
    destination: Path,
    *,
    include_checkpoints: bool = False,
) -> list[Path]:
    copied: list[Path] = []
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source).as_posix()
        if not should_copy(relative, include_checkpoints=include_checkpoints):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)
    return copied


def rsync_pull_artifacts(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    remote_run_root: str,
    destination: Path,
    include_checkpoints: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    destination.mkdir(parents=True, exist_ok=True)
    ssh_command = (
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        f"-i {shlex.quote(str(ssh_key))} -p {endpoint.port}"
    )
    patterns = [
        *DEFAULT_REQUIRED_ARTIFACTS,
        *DEFAULT_OPTIONAL_PATTERNS,
    ]
    if include_checkpoints:
        patterns.extend(DEFAULT_CHECKPOINT_PATTERNS)
    includes = ["--include=*/"]
    seen_patterns: set[str] = set()
    for pattern in patterns:
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        includes.append(f"--include={pattern}")
    command = [
        "rsync",
        "-az",
        "--prune-empty-dirs",
        *includes,
        "--exclude=*",
        "-e",
        ssh_command,
        f"{endpoint.target}:{shlex.quote(remote_run_root.rstrip('/') + '/')}",
        f"{destination}/",
    ]
    if dry_run:
        command.insert(2, "--dry-run")
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _remote_artifact_listing_python(*, include_checkpoints: bool) -> str:
    patterns = list(DEFAULT_OPTIONAL_PATTERNS)
    if include_checkpoints:
        patterns.extend(DEFAULT_CHECKPOINT_PATTERNS)
    return (
        "from pathlib import Path\n"
        "import fnmatch\n"
        "import sys\n"
        f"required = {json.dumps(list(DEFAULT_REQUIRED_ARTIFACTS))}\n"
        f"patterns = {json.dumps(patterns)}\n"
        "for path in sorted(item for item in Path('.').rglob('*') if item.is_file()):\n"
        "    relative = path.as_posix()\n"
        "    if relative.startswith('./'):\n"
        "        relative = relative[2:]\n"
        "    if relative in required or any(fnmatch.fnmatch(relative, p) for p in patterns):\n"
        "        sys.stdout.buffer.write(relative.encode() + b'\\0')\n"
    )


def ssh_tar_pull_artifacts(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    remote_run_root: str,
    destination: Path,
    include_checkpoints: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Pull artifacts over plain SSH without requiring remote rsync."""

    destination.mkdir(parents=True, exist_ok=True)
    listing_code = _remote_artifact_listing_python(include_checkpoints=include_checkpoints)
    cd_command = f"cd {shlex.quote(remote_run_root.rstrip('/'))}"
    if dry_run:
        command = (
            f"{cd_command} && python3 - <<'PY' | tr '\\0' '\\n'\n"
            f"{listing_code}"
            "PY"
        )
        return run_ssh(endpoint, ssh_key, command)

    remote_command = (
        f"set -euo pipefail; {cd_command}; "
        "python3 - <<'PY' | tar -czf - --null -T -\n"
        f"{listing_code}"
        "PY"
    )
    ssh_command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-i",
        str(ssh_key),
        "-p",
        str(endpoint.port),
        endpoint.target,
        remote_command,
    ]
    tar_command = ["tar", "-xzf", "-", "-C", str(destination)]
    ssh_process = subprocess.Popen(
        ssh_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert ssh_process.stdout is not None
    assert ssh_process.stderr is not None
    tar_process = subprocess.run(
        tar_command,
        stdin=ssh_process.stdout,
        capture_output=True,
    )
    ssh_process.stdout.close()
    ssh_stderr = ssh_process.stderr.read().decode("utf-8", errors="replace")
    ssh_returncode = ssh_process.wait()
    tar_stderr = tar_process.stderr.decode("utf-8", errors="replace")
    if ssh_returncode != 0:
        raise subprocess.CalledProcessError(ssh_returncode, ssh_command, stderr=ssh_stderr)
    if tar_process.returncode != 0:
        raise subprocess.CalledProcessError(
            tar_process.returncode,
            tar_command,
            stderr=tar_stderr,
        )
    return subprocess.CompletedProcess(
        args=ssh_command,
        returncode=0,
        stdout="",
        stderr=ssh_stderr + tar_stderr,
    )


def pull_artifacts(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    remote_run_root: str,
    destination: Path,
    include_checkpoints: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Pull artifacts over SSH, preferring rsync and falling back to tar streaming."""

    try:
        return rsync_pull_artifacts(
            endpoint=endpoint,
            ssh_key=ssh_key,
            remote_run_root=remote_run_root,
            destination=destination,
            include_checkpoints=include_checkpoints,
            dry_run=dry_run,
        )
    except FileNotFoundError as exc:
        if exc.filename != "rsync":
            raise
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if exc.returncode != 127 and "rsync" not in stderr.lower():
            raise
    return ssh_tar_pull_artifacts(
        endpoint=endpoint,
        ssh_key=ssh_key,
        remote_run_root=remote_run_root,
        destination=destination,
        include_checkpoints=include_checkpoints,
        dry_run=dry_run,
    )


def verify_required_artifacts(root: Path) -> None:
    missing = [relative for relative in DEFAULT_REQUIRED_ARTIFACTS if not (root / relative).exists()]
    if missing:
        raise ArtifactVerificationError("missing required artifacts: " + ", ".join(missing))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksums(root: Path, *, filename: str = "CHECKSUMS.sha256") -> dict[str, str]:
    checksums: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name in {filename, "archive-receipt.json"}:
            continue
        relative = path.relative_to(root).as_posix()
        checksums[relative] = file_sha256(path)
    lines = [f"{digest}  {relative}" for relative, digest in sorted(checksums.items())]
    (root / filename).write_text("\n".join(lines) + ("\n" if lines else ""))
    return checksums


def write_status_cache(
    status_cache_root: Path,
    *,
    entry: LaunchManifestEntry,
    status: dict[str, Any],
    local_archive: Path,
    remote_run_root: str,
) -> Path:
    payload = {
        **status,
        "pod_id": entry.pod_id,
        "pod_name": entry.pod_name,
        "job_name": entry.job_name,
        "sweep_name": entry.sweep_name,
        "local_archive": str(local_archive),
        "remote_run_root": remote_run_root,
        "cached_at": utc_now(),
    }
    target = status_cache_root / f"{entry.sweep_name}__{entry.job_name}__{entry.pod_id}.json"
    write_json(target, payload)
    return target


def decision_for_status(
    lane_status: str,
    *,
    has_volume: bool,
    delete_success_volume: bool = False,
    delete_failed_volume: bool = False,
) -> ReapDecision:
    normalized = lane_status.upper()
    if normalized == "DONE":
        return ReapDecision(
            lane_status=normalized,
            sync_artifacts=True,
            stop_pod=True,
            delete_pod=True,
            delete_volume=has_volume and delete_success_volume,
            reason="lane completed successfully",
        )
    if normalized == "FAILED":
        return ReapDecision(
            lane_status=normalized,
            sync_artifacts=True,
            stop_pod=True,
            delete_pod=True,
            delete_volume=has_volume and delete_failed_volume,
            reason="lane failed; preserve temporary volume unless explicitly overridden",
        )
    return ReapDecision(
        lane_status=normalized,
        sync_artifacts=False,
        stop_pod=False,
        delete_pod=False,
        delete_volume=False,
        reason="lane is not terminal",
    )


def archive_destination(
    archive_root: Path,
    *,
    entry: LaunchManifestEntry,
    remote_run_root: str,
) -> Path:
    launch_stamp = Path(remote_run_root.rstrip("/")).name
    return archive_root / entry.sweep_name / entry.job_name / launch_stamp
