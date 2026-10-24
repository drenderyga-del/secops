import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def git(*args: str, cwd: Path) -> None:
    run("git", *args, cwd=cwd)


def configure(repo: Path) -> None:
    git("config", "user.email", "test@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)


@pytest.fixture
def flow_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    if not shutil.which("rsync"):
        pytest.skip("rsync is required by the release-export script")

    source_remote = tmp_path / "source.git"
    source = tmp_path / "source"
    internal_remote = tmp_path / "internal.git"
    internal = tmp_path / "internal"
    run("git", "init", "--bare", str(source_remote), cwd=tmp_path)
    run("git", "init", "--bare", str(internal_remote), cwd=tmp_path)
    run("git", "clone", str(source_remote), str(source), cwd=tmp_path)
    run("git", "clone", str(internal_remote), str(internal), cwd=tmp_path)
    configure(source)
    configure(internal)

    (source / "scripts").mkdir()
    for filename in (
        "export_internal.sh",
        "internal-export.exclude",
        "sync_internal_release.sh",
        "publish_internal_release.sh",
    ):
        shutil.copy2(ROOT / "scripts" / filename, source / "scripts" / filename)
    (source / "app.py").write_text("RELEASE = 'source-v1.2.3'\n", encoding="utf-8")
    (source / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: source-release\n", encoding="utf-8"
    )
    (source / "obsolete.py").write_text("REMOVE_IN_NEXT_RELEASE = True\n", encoding="utf-8")
    (source / ".gitlab-ci.yml").write_text("source-ci\n", encoding="utf-8")
    (source / ".impeccable").mkdir()
    (source / ".impeccable" / "local-note.md").write_text("local artifact\n", encoding="utf-8")
    (source / ".gitignore").write_text(
        "node_modules/\n"
        "app/static/next/\n"
        "playwright-report/\n"
        "test-results/\n",
        encoding="utf-8",
    )
    ignored_dependencies = tmp_path / "ignored-dependencies"
    ignored_dependencies.mkdir()
    (ignored_dependencies / "generated.js").write_text("local dependency\n", encoding="utf-8")
    (source / "node_modules").symlink_to(ignored_dependencies, target_is_directory=True)
    (source / "app" / "static" / "next").mkdir(parents=True)
    (source / "app" / "static" / "next" / "index.js").write_text(
        "local build\n", encoding="utf-8"
    )
    git("add", ".", cwd=source)
    git("commit", "-m", "source release", cwd=source)
    git("branch", "-M", "main", cwd=source)
    git("tag", "v1.2.3", cwd=source)
    git("push", "origin", "main", "--tags", cwd=source)

    (internal / ".gitlab-ci.yml").write_text("internal-ci\n", encoding="utf-8")
    (internal / ".env").write_text(
        "SECPIPE_IMAGE=registry.internal/secpipe-monitor:v2.3.2\n"
        "INTERNAL_SECRET=keep\n",
        encoding="utf-8",
    )
    (internal / "internal-only.txt").write_text("keep\n", encoding="utf-8")
    (internal / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: internal-release\n", encoding="utf-8"
    )
    git("add", ".", cwd=internal)
    git("commit", "-m", "internal baseline", cwd=internal)
    git("branch", "-M", "v2.0", cwd=internal)
    git("tag", "v2.3.2", cwd=internal)
    git("push", "origin", "v2.0", "--tags", cwd=internal)
    return source, internal, internal_remote


def test_publish_exports_source_tag_and_pushes_internal_patch_release(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, internal_remote = flow_fixture

    result = run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--bump",
        "patch",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        str(internal),
        cwd=source,
    )

    assert "Internal release: v2.3.3" in result.stdout
    assert (internal / "app.py").read_text(encoding="utf-8") == "RELEASE = 'source-v1.2.3'\n"
    assert (internal / ".gitlab-ci.yml").read_text(encoding="utf-8") == "internal-ci\n"
    assert "INTERNAL_SECRET=keep" in (internal / ".env").read_text(encoding="utf-8")
    assert (internal / "internal-only.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (internal / ".impeccable").exists()
    assert "internal_tag=v2.3.3" in (internal / ".scanops-release").read_text(encoding="utf-8")
    assert (internal / ".scanops-export-manifest").is_file()
    assert run("git", "show-ref", "--verify", "refs/tags/v2.3.3", cwd=internal).returncode == 0
    remote_tags = run("git", "ls-remote", "--tags", "--refs", str(internal_remote), cwd=internal).stdout
    assert "refs/tags/v2.3.3" in remote_tags


def test_export_can_initialize_a_missing_manifest_for_a_clean_legacy_checkout(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, _internal_remote = flow_fixture
    (source / "README.md").write_text("source README\n", encoding="utf-8")
    git("add", "README.md", cwd=source)
    git("commit", "-m", "source README", cwd=source)
    git("push", "origin", "main", cwd=source)
    git("tag", "v1.2.4", cwd=source)
    git("push", "origin", "refs/tags/v1.2.4", cwd=source)
    (internal / "README.md").write_text("legacy README\n", encoding="utf-8")
    git("add", "README.md", cwd=internal)
    git("commit", "-m", "legacy README", cwd=internal)
    (source / "AGENTS.md").write_text("local instructions\n", encoding="utf-8")
    (source / ".DS_Store").write_bytes(b"local metadata")
    (source / "untracked-secret.txt").write_text("must not export\n", encoding="utf-8")

    exported = run(
        "bash",
        str(source / "scripts" / "sync_internal_release.sh"),
        "--initialize-manifest",
        "--remote",
        "origin",
        str(internal),
        "v1.2.4",
        cwd=source,
    )

    assert "Initialized ownership" in exported.stdout
    assert (internal / "README.md").read_text(encoding="utf-8") == "source README\n"
    assert (internal / ".gitlab-ci.yml").read_text(encoding="utf-8") == "internal-ci\n"
    assert "INTERNAL_SECRET=keep" in (internal / ".env").read_text(encoding="utf-8")
    assert (internal / "internal-only.txt").read_text(encoding="utf-8") == "keep\n"
    manifest = (internal / ".scanops-export-manifest").read_text(encoding="utf-8")
    assert "README.md" in manifest.splitlines()
    assert "AGENTS.md" not in manifest
    assert ".DS_Store" not in manifest
    assert not (internal / "untracked-secret.txt").exists()


def test_export_manifest_initialization_rejects_dirty_destination(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, _internal_remote = flow_fixture
    (internal / "app.py").write_text("destination-only local file\n", encoding="utf-8")

    result = run(
        "bash",
        str(source / "scripts" / "sync_internal_release.sh"),
        "--initialize-manifest",
        "--remote",
        "origin",
        str(internal),
        "v1.2.3",
        cwd=source,
        check=False,
    )

    assert result.returncode != 0
    # The release wrapper refuses a dirty destination before the exporter's
    # own manifest-initialization check; either refusal is the contract.
    assert (
        "Destination has uncommitted changes" in result.stderr
        or "requires a clean destination repository" in result.stderr
    )
    assert (internal / "app.py").read_text(encoding="utf-8") == "destination-only local file\n"
    assert not (internal / ".scanops-export-manifest").exists()


def test_direct_export_is_disabled_and_release_sync_ignores_dirty_tracked_source(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, _internal_remote = flow_fixture
    (source / "app.py").write_text("SENTINEL_SECRET_FROM_DIRTY_TREE\n", encoding="utf-8")

    direct = run(
        "bash",
        str(source / "scripts" / "export_internal.sh"),
        str(internal),
        cwd=source,
        check=False,
    )
    assert direct.returncode == 2
    assert "Direct exports are disabled" in direct.stderr
    assert not (internal / "app.py").exists()

    run(
        "bash",
        str(source / "scripts" / "sync_internal_release.sh"),
        "--remote",
        "origin",
        str(internal),
        "v1.2.3",
        cwd=source,
    )
    assert (internal / "app.py").read_text(encoding="utf-8") == "RELEASE = 'source-v1.2.3'\n"
    assert "SENTINEL_SECRET_FROM_DIRTY_TREE" not in (internal / "app.py").read_text(encoding="utf-8")


def test_publish_dry_run_calculates_minor_without_mutating_destination(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, _internal_remote = flow_fixture
    before = run("git", "rev-parse", "HEAD", cwd=internal).stdout.strip()

    result = run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--bump",
        "minor",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        "--dry-run",
        str(internal),
        cwd=source,
    )

    assert "Internal release: v2.4.0" in result.stdout
    assert "Dry run: no files were exported, committed, tagged, or pushed." in result.stdout
    assert run("git", "rev-parse", "HEAD", cwd=internal).stdout.strip() == before
    assert not (internal / ".scanops-release").exists()


def test_publish_dry_run_does_not_fast_forward_a_destination_ahead_of_local(
    flow_fixture: tuple[Path, Path, Path],
    tmp_path: Path,
):
    source, internal, internal_remote = flow_fixture
    upstream = tmp_path / "internal-upstream"
    run("git", "clone", str(internal_remote), str(upstream), cwd=tmp_path)
    git("switch", "--track", "-c", "v2.0", "origin/v2.0", cwd=upstream)
    configure(upstream)
    (upstream / "remote-only.txt").write_text("remote update\n", encoding="utf-8")
    git("add", "remote-only.txt", cwd=upstream)
    git("commit", "-m", "advance internal remote", cwd=upstream)
    git("push", "origin", "v2.0", cwd=upstream)

    before = run("git", "rev-parse", "HEAD", cwd=internal).stdout.strip()
    source_state_before = _git_refs_and_fetch_head(source)
    internal_state_before = _git_refs_and_fetch_head(internal)
    result = run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--bump",
        "minor",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        "--dry-run",
        str(internal),
        cwd=source,
    )

    assert "Internal release: v2.4.0" in result.stdout
    assert run("git", "rev-parse", "HEAD", cwd=internal).stdout.strip() == before
    assert not (internal / "remote-only.txt").exists()
    assert not (internal / ".scanops-release").exists()
    assert _git_refs_and_fetch_head(source) == source_state_before
    assert _git_refs_and_fetch_head(internal) == internal_state_before


def test_publish_removes_stale_exported_files_but_preserves_internal_files(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, _internal_remote = flow_fixture

    run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--bump",
        "patch",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        str(internal),
        cwd=source,
    )

    git("rm", "obsolete.py", cwd=source)
    (source / "app.py").write_text("RELEASE = 'source-v1.2.4'\n", encoding="utf-8")
    git("commit", "-am", "remove obsolete deployable file", cwd=source)
    git("tag", "v1.2.4", cwd=source)
    git("push", "origin", "main", "--tags", cwd=source)

    result = run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.4",
        "--bump",
        "patch",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        str(internal),
        cwd=source,
    )

    assert "Internal release: v2.3.4" in result.stdout
    assert not (internal / "obsolete.py").exists()
    assert (internal / "app.py").read_text(encoding="utf-8") == "RELEASE = 'source-v1.2.4'\n"
    assert (internal / ".gitlab-ci.yml").read_text(encoding="utf-8") == "internal-ci\n"
    assert "INTERNAL_SECRET=keep" in (internal / ".env").read_text(encoding="utf-8")
    assert (internal / "internal-only.txt").read_text(encoding="utf-8") == "keep\n"
    assert "obsolete.py" not in (internal / ".scanops-export-manifest").read_text(encoding="utf-8")
    assert (internal / "docker-compose.yml").read_text(encoding="utf-8") == (
        "services:\n  app:\n    image: internal-release\n"
    )
    assert "docker-compose.yml" not in (internal / ".scanops-export-manifest").read_text(
        encoding="utf-8"
    )


def test_publish_excludes_ignored_build_directories_and_symlinks(
    flow_fixture: tuple[Path, Path, Path],
):
    source, internal, _internal_remote = flow_fixture

    run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--internal-tag",
        "v2.3.3",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        str(internal),
        cwd=source,
    )

    manifest = (internal / ".scanops-export-manifest").read_text(encoding="utf-8")
    assert not (internal / "node_modules").exists()
    assert not (internal / "app" / "static" / "next").exists()
    assert "node_modules" not in manifest
    assert "app/static/next" not in manifest


@pytest.mark.parametrize("script_name", [
    "export_internal.sh",
    "publish_internal_release.sh",
    "sync_internal_release.sh",
])
def test_release_scripts_reject_source_subdirectory_destinations(
    flow_fixture: tuple[Path, Path, Path],
    script_name: str,
):
    source, _internal, _internal_remote = flow_fixture
    destination = source / "scripts"
    script = source / "scripts" / script_name
    if script_name == "export_internal.sh":
        args = ["bash", str(script), str(destination)]
    elif script_name == "publish_internal_release.sh":
        args = [
            "bash", str(script),
            "--source-tag", "v1.2.3",
            "--internal-tag", "v2.3.3",
            "--source-remote", "origin",
            "--internal-remote", "origin",
            str(destination),
        ]
    else:
        args = ["bash", str(script), "--remote", "origin", str(destination), "v1.2.3"]

    result = run(*args, cwd=source, check=False)

    assert result.returncode == 2
    assert "must not overlap" in result.stderr


@pytest.mark.parametrize("script_name", [
    "export_internal.sh",
    "publish_internal_release.sh",
    "sync_internal_release.sh",
])
def test_release_scripts_reject_nested_repository_destinations(
    flow_fixture: tuple[Path, Path, Path],
    script_name: str,
):
    source, _internal, _internal_remote = flow_fixture
    destination = source / "nested-internal"
    destination.mkdir()
    run("git", "init", str(destination), cwd=source)
    script = source / "scripts" / script_name
    if script_name == "export_internal.sh":
        args = ["bash", str(script), str(destination)]
    elif script_name == "publish_internal_release.sh":
        args = [
            "bash", str(script),
            "--source-tag", "v1.2.3",
            "--internal-tag", "v2.3.3",
            "--source-remote", "origin",
            "--internal-remote", "origin",
            str(destination),
        ]
    else:
        args = ["bash", str(script), "--remote", "origin", str(destination), "v1.2.3"]

    result = run(*args, cwd=source, check=False)

    assert result.returncode == 2
    assert "must not overlap" in result.stderr


@pytest.mark.parametrize("destination_shape", ["file", "symlink", "directory"])
def test_publish_refuses_to_take_over_destination_owned_source_paths(
    flow_fixture: tuple[Path, Path, Path],
    destination_shape: str,
):
    source, internal, _internal_remote = flow_fixture
    destination_path = internal / "app.py"
    if destination_shape == "file":
        destination_path.write_text("internal-owned\n", encoding="utf-8")
    elif destination_shape == "symlink":
        (internal / "internal-app.py").write_text("internal-owned\n", encoding="utf-8")
        destination_path.symlink_to("internal-app.py")
    else:
        destination_path.mkdir()
        (destination_path / "internal-owned.txt").write_text("keep\n", encoding="utf-8")
    git("add", "-A", cwd=internal)
    git("commit", "-m", "claim source path internally", cwd=internal)

    result = run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--internal-tag",
        "v2.3.3",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        str(internal),
        cwd=source,
        check=False,
    )

    assert result.returncode == 2
    assert "already belongs to destination" in result.stderr or "destination symlink" in result.stderr
    if destination_shape == "file":
        assert destination_path.read_text(encoding="utf-8") == "internal-owned\n"
    elif destination_shape == "symlink":
        assert destination_path.is_symlink()
    else:
        assert (destination_path / "internal-owned.txt").read_text(encoding="utf-8") == "keep\n"


def test_publish_refuses_dirty_internal_checkout(flow_fixture: tuple[Path, Path, Path]):
    source, internal, _internal_remote = flow_fixture
    (internal / "internal-only.txt").write_text("uncommitted\n", encoding="utf-8")

    result = run(
        "bash",
        str(source / "scripts" / "publish_internal_release.sh"),
        "--source-tag",
        "v1.2.3",
        "--internal-tag",
        "v2.3.3",
        "--source-remote",
        "origin",
        "--internal-remote",
        "origin",
        str(internal),
        cwd=source,
        check=False,
    )

    assert result.returncode == 2
    assert "Destination has uncommitted changes" in result.stderr


def test_sync_accepts_a_source_worktree(flow_fixture: tuple[Path, Path, Path], tmp_path: Path):
    source, internal, _internal_remote = flow_fixture
    source_worktree = tmp_path / "source-worktree"
    run("git", "worktree", "add", "--detach", str(source_worktree), "v1.2.3", cwd=source)

    result = run(
        "bash",
        str(source_worktree / "scripts" / "sync_internal_release.sh"),
        "--remote",
        "origin",
        str(internal),
        "v1.2.3",
        cwd=source_worktree,
    )

    assert result.returncode == 0
    assert (source_worktree / ".git").is_file()
    assert (internal / "app.py").read_text(encoding="utf-8") == "RELEASE = 'source-v1.2.3'\n"


def test_deploy_dry_run_does_not_connect_or_require_a_host(
    tmp_path: Path,
):
    identity = tmp_path / "deploy-key"
    identity.write_text("not a real key\n", encoding="utf-8")
    identity.chmod(0o600)

    result = run(
        "bash",
        str(ROOT / "scripts" / "deploy_internal_release.sh"),
        "--host",
        "scanops.example.com",
        "--path",
        "/opt/secpipe-monitor/secpipe-monitor/",
        "--tag",
        "v2.3.3",
        "--identity-file",
        str(identity),
        "--dry-run",
        cwd=ROOT,
    )

    assert "Host:              scanops.example.com:22" in result.stdout
    assert "Remote checkout:   /opt/secpipe-monitor/secpipe-monitor/" in result.stdout
    assert "Compose services:  app pipeline_worker iac_worker" in result.stdout
    assert "no SSH connection or remote changes were made" in result.stdout


def _git_refs_and_fetch_head(repo: Path) -> tuple[str, bytes | None]:
    refs = run(
        "git",
        "for-each-ref",
        "--format=%(refname)=%(objectname)",
        cwd=repo,
    ).stdout
    fetch_head = Path(run("git", "rev-parse", "--git-path", "FETCH_HEAD", cwd=repo).stdout.strip())
    if not fetch_head.is_absolute():
        fetch_head = repo / fetch_head
    return refs, fetch_head.read_bytes() if fetch_head.exists() else None
