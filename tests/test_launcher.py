from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from runpod_research import launcher


def minimal_spec() -> dict:
    return {
        "schema_version": 1,
        "name": "Smoke Sweep",
        "remote_artifact_root": "/workspace/experiments/test",
        "defaults": {
            "networkVolumeId": "${RUNPOD_NETWORK_VOLUME_ID}",
            "volumeMountPath": "/workspace",
            "storage_mode": "master-volume",
            "env": {"HF_HOME": "/workspace/hf-cache"},
            "dockerStartCmd": ["bash", "-lc", "echo ${RPR_JOB_NAME}"],
        },
        "jobs": [
            {
                "name": "Lane 1",
                "env": {"RPR_LANE": "lane-1"},
                "dockerStartCmd": ["bash", "-lc", "echo ${RPR_JOB_NAME}"],
            }
        ],
    }


def test_build_payloads_allows_unresolved_for_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)

    payloads, unresolved = launcher.build_payloads(minimal_spec(), allow_unresolved=True)

    assert unresolved == ["RUNPOD_NETWORK_VOLUME_ID"]
    assert payloads[0]["payload"]["networkVolumeId"] == "${RUNPOD_NETWORK_VOLUME_ID}"
    assert payloads[0]["pod_name"] == "smoke-sweep-lane-1"
    assert payloads[0]["payload"]["env"]["RPR_ARTIFACT_ROOT"] == "/workspace/experiments/test"
    assert payloads[0]["payload"]["env"]["RPR_JOB_NAME"] == "lane-1"
    assert "storage_mode" not in payloads[0]["payload"]


def test_build_payloads_requires_resolved_env_for_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)

    with pytest.raises(launcher.ConfigError, match="RUNPOD_NETWORK_VOLUME_ID"):
        launcher.build_payloads(minimal_spec(), allow_unresolved=False)


def test_build_payloads_expands_volume_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNPOD_NETWORK_VOLUME_ID", "volume-123")

    payloads, unresolved = launcher.build_payloads(minimal_spec(), allow_unresolved=False)

    assert unresolved == []
    assert payloads[0]["payload"]["networkVolumeId"] == "volume-123"
    assert payloads[0]["payload"]["env"]["HF_HOME"] == "/workspace/hf-cache"
    assert payloads[0]["payload"]["env"]["RPR_LANE"] == "lane-1"
    assert payloads[0]["payload"]["dockerStartCmd"][2] == "echo ${RPR_JOB_NAME}"


def test_remote_root_can_be_any_absolute_path() -> None:
    spec = minimal_spec()
    spec["remote_artifact_root"] = "/mnt/research/artifacts"

    payloads, _ = launcher.build_payloads(spec, allow_unresolved=True)

    assert payloads[0]["payload"]["env"]["RPR_ARTIFACT_ROOT"] == "/mnt/research/artifacts"


def test_load_dotenv_accepts_spaces_without_overriding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "RUNPOD_API_KEY = from-file",
                "RUNPOD_NETWORK_VOLUME_ID=volume-from-file",
                "export EXTRA_VALUE = quoted",
            ]
        )
    )
    monkeypatch.setenv("RUNPOD_API_KEY", "from-shell")
    monkeypatch.delenv("RUNPOD_NETWORK_VOLUME_ID", raising=False)
    monkeypatch.delenv("EXTRA_VALUE", raising=False)

    loaded = launcher.load_dotenv(env_path)

    assert loaded == ["RUNPOD_NETWORK_VOLUME_ID", "EXTRA_VALUE"]
    assert launcher.os.environ["RUNPOD_API_KEY"] == "from-shell"
    assert launcher.os.environ["RUNPOD_NETWORK_VOLUME_ID"] == "volume-from-file"
    assert launcher.os.environ["EXTRA_VALUE"] == "quoted"


def test_ensure_default_public_key_reads_standard_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_ed25519.pub").write_text("ssh-ed25519 public-key\n")
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("RUNPOD_PUBLIC_KEY", raising=False)

    loaded = launcher.ensure_default_public_key()

    assert loaded is True
    assert launcher.os.environ["RUNPOD_PUBLIC_KEY"] == "ssh-ed25519 public-key"


def test_pod_id_from_nested_response() -> None:
    assert launcher.pod_id_from_response({"pod": {"id": "pod-123"}}) == "pod-123"


def test_render_writes_redacted_payload_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNPOD_API_KEY", "secret-token")
    spec = minimal_spec()
    spec["defaults"]["env"]["RUNPOD_API_KEY"] = "${RUNPOD_API_KEY}"
    spec["jobs"][0]["dockerStartCmd"] = ["bash", "-lc", "echo ${RUNPOD_API_KEY}"]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    out_dir = tmp_path / "manifests"

    result = launcher.render(
        SimpleNamespace(spec=str(spec_path), out_dir=str(out_dir), api_base="https://api.example")
    )

    assert result == 0
    [payload_path] = list(out_dir.glob("*/payloads/lane-1.json"))
    payload = json.loads(payload_path.read_text())
    assert payload["env"]["RUNPOD_API_KEY"] == "<redacted>"
    assert payload["dockerStartCmd"] == ["bash", "-lc", "echo <redacted>"]
    [manifest_path] = list(out_dir.glob("*/manifest.json"))
    manifest = json.loads(manifest_path.read_text())
    assert manifest["jobs"][0]["payload_redacted"] is True


def test_load_spec_enforces_full_schema_validation(tmp_path: Path) -> None:
    spec = minimal_spec()
    spec["jobs"] = [{"name": "Lane A"}, {"name": "Lane+A"}]
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))

    with pytest.raises(launcher.ConfigError, match="duplicate slugified job name"):
        launcher.load_spec(spec_path)


def test_api_request_redacts_http_error_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)

    def fake_urlopen(*args, **kwargs):
        raise HTTPError(
            "https://api.example/pods",
            400,
            "Bad Request",
            hdrs={},
            fp=io.BytesIO(b'{"RUNPOD_API_KEY":"direct-secret","message":"direct-secret"}'),
        )

    monkeypatch.setattr(launcher, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc_info:
        launcher.api_request(
            "POST",
            "/pods",
            api_key="direct-secret",
            api_base="https://api.example",
            payload={"name": "demo"},
        )

    message = str(exc_info.value)
    assert "direct-secret" not in message
    assert "<redacted>" in message


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_api_request_identifies_the_application_for_every_http_method(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    requests = []

    def fake_urlopen(request, *, timeout):
        requests.append(request)
        assert timeout == 60
        return io.BytesIO(b'{"ok":true}')

    monkeypatch.setattr(launcher, "urlopen", fake_urlopen)
    payload = {"name": "example"} if method == "POST" else None
    result = launcher.api_request(
        method, "/pods", api_key="example-key", api_base="https://api.example", payload=payload
    )
    assert result == {"ok": True}
    assert len(requests) == 1
    request = requests[0]
    assert request.get_method() == method
    assert request.get_header("User-agent") == launcher.API_USER_AGENT
    assert request.get_header("Authorization") == "Bearer example-key"
    assert request.get_header("Accept") == "application/json"
    if payload is not None:
        assert request.get_header("Content-type") == "application/json"
        assert json.loads(request.data) == payload
    else:
        assert request.data is None


@pytest.mark.parametrize(
    ("code", "body", "category"),
    [
        (401, b'{"message":"Unauthorized"}', "authentication"),
        (403, b"Forbidden", "authorization"),
        (403, b'{"error":"error code: 1010"}', "edge_rejection"),
        (503, b"Service unavailable", "http"),
    ],
)
def test_api_http_failures_preserve_category_and_never_retry_mutations(
    monkeypatch: pytest.MonkeyPatch, code: int, body: bytes, category: str
) -> None:
    calls = []

    def fake_urlopen(request, *, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, code, "Failure", hdrs={}, fp=io.BytesIO(body))

    monkeypatch.setattr(launcher, "urlopen", fake_urlopen)
    with pytest.raises(launcher.APIRequestError) as caught:
        launcher.api_request(
            "POST", "/pods", api_key="example-key", api_base="https://api.example", payload={}
        )
    assert caught.value.status_code == code
    assert caught.value.category == category
    assert len(calls) == 1


@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_api_connection_failures_never_retry_mutations(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    calls = []

    def fake_urlopen(request, *, timeout):
        calls.append(request)
        raise URLError("connection reset: example-secret")

    monkeypatch.setattr(launcher, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError) as caught:
        launcher.api_request(
            method, "/pods", api_key="example-secret", api_base="https://api.example"
        )
    assert "example-secret" not in str(caught.value)
    assert "<redacted>" in str(caught.value)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("account_key", "inherited_key", "expected"),
    [("account-key", "pod-key", "account-key"), (" ", "pod-key", "pod-key"),
     ("account-key", "", "account-key")],
)
def test_explicit_account_key_takes_precedence(
    monkeypatch: pytest.MonkeyPatch, account_key: str, inherited_key: str, expected: str
) -> None:
    monkeypatch.setenv("RUNPOD_ACCOUNT_API_KEY", account_key)
    monkeypatch.setenv("RUNPOD_API_KEY", inherited_key)
    assert launcher.api_key_from_env() == expected


def test_missing_api_keys_fail_without_fallback_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUNPOD_ACCOUNT_API_KEY", raising=False)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    with pytest.raises(launcher.ConfigError, match="RUNPOD_ACCOUNT_API_KEY or RUNPOD_API_KEY"):
        launcher.api_key_from_env()


def test_redact_for_manifest_removes_nested_secrets() -> None:
    payload = {
        "env": {
            "GITHUB_TOKEN": "secret-token",
            "RUNPOD_API_KEY": "secret-key",
            "PUBLIC_KEY": "ssh-ed25519 public-key",
        },
        "nested": [{"PASSWORD": "secret-password"}],
    }

    redacted = launcher.redact_for_manifest(payload)

    assert redacted["env"]["GITHUB_TOKEN"] == "<redacted>"
    assert redacted["env"]["RUNPOD_API_KEY"] == "<redacted>"
    assert redacted["env"]["PUBLIC_KEY"] == "ssh-ed25519 public-key"
    assert redacted["nested"][0]["PASSWORD"] == "<redacted>"
