"""Helpers for promoting local RunPod archives to an archive volume."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .lifecycle import PodEndpoint, utc_now, write_json


@dataclass(frozen=True)
class RsyncUploadCommand:
    argv: tuple[str, ...]


def validate_remote_subdir(value: str) -> str:
    if value.strip().startswith("/"):
        raise ValueError(f"unsafe remote subdir: {value!r}")
    normalized = value.strip().strip("/")
    if not normalized:
        raise ValueError("remote subdir must not be empty")
    parts = Path(normalized).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe remote subdir: {value!r}")
    return normalized


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_archive_manifest(*, local_paths: list[Path], remote_subdir: str) -> dict[str, Any]:
    remote_subdir = validate_remote_subdir(remote_subdir)
    entries: list[dict[str, str]] = []
    for local_path in local_paths:
        if not local_path.exists():
            raise FileNotFoundError(local_path)
        root_name = local_path.name
        if local_path.is_file():
            entries.append(
                {
                    "path": root_name,
                    "source": str(local_path),
                    "sha256": sha256_file(local_path),
                }
            )
            continue
        for path in sorted(item for item in local_path.rglob("*") if item.is_file()):
            entries.append(
                {
                    "path": (Path(root_name) / path.relative_to(local_path)).as_posix(),
                    "source": str(path),
                    "sha256": sha256_file(path),
                }
            )
    return {
        "schema_version": 1,
        "created_at_utc": utc_now(),
        "remote_subdir": remote_subdir,
        "files": entries,
    }


def write_archive_manifest(path: Path, *, local_paths: list[Path], remote_subdir: str) -> dict[str, Any]:
    manifest = build_archive_manifest(local_paths=local_paths, remote_subdir=remote_subdir)
    write_json(path, manifest)
    return manifest


def build_rsync_upload_command(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    local_path: Path,
    remote_root: str,
    remote_subdir: str,
) -> RsyncUploadCommand:
    remote_subdir = validate_remote_subdir(remote_subdir)
    remote_destination = f"{remote_root.rstrip('/')}/{remote_subdir}/"
    ssh_command = (
        f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        f"-i {shlex.quote(str(ssh_key))} -p {endpoint.port}"
    )
    return RsyncUploadCommand(
        argv=(
            "rsync",
            "-az",
            "--no-owner",
            "--no-group",
            "-e",
            ssh_command,
            str(local_path),
            f"{endpoint.target}:{remote_destination}",
        )
    )


def upload_path_via_rsync(
    *,
    endpoint: PodEndpoint,
    ssh_key: Path,
    local_path: Path,
    remote_root: str,
    remote_subdir: str,
    dry_run: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = list(
        build_rsync_upload_command(
            endpoint=endpoint,
            ssh_key=ssh_key,
            local_path=local_path,
            remote_root=remote_root,
            remote_subdir=remote_subdir,
        ).argv
    )
    if dry_run:
        command.insert(2, "--dry-run")
    return subprocess.run(command, check=True, capture_output=True, text=True)


def remote_verify_command(*, remote_root: str, remote_subdir: str, manifest_name: str) -> str:
    remote_subdir = validate_remote_subdir(remote_subdir)
    remote_dir = f"{remote_root.rstrip('/')}/{remote_subdir}"
    return (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "import hashlib, json, sys\n"
        f"root = Path({json.dumps(remote_dir)})\n"
        f"manifest = json.loads((root / {json.dumps(manifest_name)}).read_text())\n"
        "bad = []\n"
        "for item in manifest.get('files', []):\n"
        "    path = root / item['path']\n"
        "    h = hashlib.sha256()\n"
        "    if not path.exists():\n"
        "        bad.append(item['path'])\n"
        "        continue\n"
        "    with path.open('rb') as handle:\n"
        "        for chunk in iter(lambda: handle.read(1024 * 1024), b''):\n"
        "            h.update(chunk)\n"
        "    if h.hexdigest() != item['sha256']:\n"
        "        bad.append(item['path'])\n"
        "if bad:\n"
        "    print('\\n'.join(bad), file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "print('archive verify ok')\n"
        "PY"
    )
