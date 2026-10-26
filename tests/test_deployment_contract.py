import os
import subprocess
from pathlib import Path

import pytest

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_COMPOSE = ROOT / "docker-compose.yml"
PRODUCTION_DOCKERFILE = ROOT / "Dockerfile"


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _seed_internal_checkout(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "internal.git"
    seed = tmp_path / "seed"
    checkout = tmp_path / "checkout"
    _run_git("init", "--bare", str(remote), cwd=tmp_path)
    _run_git("init", str(seed), cwd=tmp_path)
    _run_git("config", "user.email", "test@example.com", cwd=seed)
    _run_git("config", "user.name", "Test", cwd=seed)
    (seed / ".env").write_text(
        "SECPIPE_IMAGE=registry.example/secpipe-monitor:v2.3.2\n",
        encoding="utf-8",
    )
    _run_git("add", ".env", cwd=seed)
    _run_git("commit", "-m", "internal baseline", cwd=seed)
    _run_git("branch", "-M", "v2.0", cwd=seed)
    _run_git("tag", "v2.3.2", cwd=seed)
    _run_git("push", str(remote), "v2.0", "--tags", cwd=seed)
    subprocess.run(
        ["git", "clone", "-b", "v2.0", str(remote), str(checkout)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    _run_git("config", "user.email", "test@example.com", cwd=checkout)
    _run_git("config", "user.name", "Test", cwd=checkout)
    return remote, checkout


def _fake_ssh_bin(tmp_path: Path, *, with_docker: bool = False) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "ssh").write_text(
        """#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-p|-i) shift 2 ;;
    -*) shift ;;
    *) shift; break ;;
  esac
done
[ "$1" = bash ] && shift
[ "$1" = -s ] && shift
[ "$1" = -- ] && shift
exec bash -s -- "$@"
""",
        encoding="utf-8",
    )
    (fake_bin / "ssh").chmod(0o755)
    if with_docker:
        (fake_bin / "docker").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "docker").chmod(0o755)
        (fake_bin / "curl").write_text(
            "#!/bin/sh\nprintf '%s' '{\"status\":\"healthy\"}'\n",
            encoding="utf-8",
        )
        (fake_bin / "curl").chmod(0o755)
    return fake_bin


def test_deploy_rejects_a_local_only_release_tag(tmp_path: Path):
    _remote, checkout = _seed_internal_checkout(tmp_path)
    _run_git("tag", "v2.3.3", cwd=checkout)
    fake_bin = _fake_ssh_bin(tmp_path)
    result = subprocess.run(
        [
            str(ROOT / "scripts/deploy_internal_release.sh"),
            "--host",
            "fake-host",
            "--path",
            str(checkout),
            "--tag",
            "v2.3.3",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Release tag does not exist on origin: v2.3.3" in result.stderr


def test_deploy_accepts_a_release_tag_that_exists_on_origin(tmp_path: Path):
    _remote, checkout = _seed_internal_checkout(tmp_path)
    _run_git("tag", "v2.3.3", cwd=checkout)
    _run_git("push", "origin", "v2.3.3", cwd=checkout)
    fake_bin = _fake_ssh_bin(tmp_path, with_docker=True)
    result = subprocess.run(
        [
            str(ROOT / "scripts/deploy_internal_release.sh"),
            "--host",
            "fake-host",
            "--path",
            str(checkout),
            "--tag",
            "v2.3.3",
        ],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Deployed v2.3.3" in result.stdout


def test_release_metadata_is_consistent():
    expected_version = "1.3.8"
    image = f"registry.example.com/scanops/scanops:v{expected_version}"

    assert app.version == expected_version
    assert f"SECPIPE_IMAGE={image}" in (ROOT / ".env.example").read_text(
        encoding="utf-8"
    )
    assert f"SECPIPE_IMAGE={image}" in (ROOT / "docs/operations-reference.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(
    not PRODUCTION_COMPOSE.exists(),
    reason="production compose is owned by the internal repository",
)
def test_production_compose_requires_auth_and_shared_integration_key():
    compose = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

    assert "INTEGRATION_CREDENTIALS_KEY: ${INTEGRATION_CREDENTIALS_KEY:?" in compose
    assert "UI_AUTH_USERNAME: ${UI_AUTH_USERNAME:?" in compose
    assert "UI_AUTH_PASSWORD: ${UI_AUTH_PASSWORD:?" in compose


def test_credential_reset_runbook_uses_host_uid_for_manifest_bind_mount():
    runbook = (ROOT / "docs/operations-reference.md").read_text(encoding="utf-8")

    assert runbook.count('--user "$(id -u):$(id -g)"') >= 2
    assert "docker build \\\n     --platform linux/amd64" in runbook


def test_production_dockerfile_builds_without_private_infrastructure():
    dockerfile = PRODUCTION_DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG NODE_BASE_IMAGE=node:22-alpine" in dockerfile
    assert "ARG PYTHON_BASE_IMAGE=python:3.12-alpine3.22" in dockerfile
    assert "ARG NPM_REGISTRY_URL=https://registry.npmjs.org/" in dockerfile
    assert "ARG PIP_INDEX_URL=https://pypi.org/simple/" in dockerfile
    assert "npm config set strict-ssl true" in dockerfile
    assert "--index-url" in dockerfile
    assert "USER devsec" in dockerfile
    assert "COPY cert" not in dockerfile
    assert "trusted-host" not in dockerfile


@pytest.mark.skipif(
    not PRODUCTION_COMPOSE.exists(),
    reason="production compose is owned by the internal repository",
)
def test_production_compose_keeps_reports_and_database_off_public_network():
    compose = PRODUCTION_COMPOSE.read_text(encoding="utf-8")

    assert '"127.0.0.1:${POSTGRES_PORT:-5432}:5432"' in compose
    assert "CONFIG_SCAN_REPORTS_DIR: ${CONFIG_SCAN_REPORTS_DIR:-/data/config_scan_reports}" in compose
    assert "- config_scan_reports:/data/config_scan_reports" in compose
    assert "config_scan_reports:" in compose
    assert "ASOC_VERIFY_SSL: ${ASOC_VERIFY_SSL:-true}" in compose
    assert "PTAI_VERIFY_SSL: ${PTAI_VERIFY_SSL:-true}" in compose
    assert "HEALTH_PIPELINE_POLL_STALE_SECONDS: ${HEALTH_PIPELINE_POLL_STALE_SECONDS:-900}" in compose
    assert "TOPOLOGY_SIGNAL_STALE_SECONDS: ${TOPOLOGY_SIGNAL_STALE_SECONDS:-86400}" in compose
