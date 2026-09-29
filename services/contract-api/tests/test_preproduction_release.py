from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy_preproduction.py"
SPEC = importlib.util.spec_from_file_location("deploy_preproduction", SCRIPT)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture(autouse=True)
def isolate_repository(monkeypatch, tmp_path):
    # The script runs on the deployment host; its tests also run inside the /app test image.
    monkeypatch.setattr(release, "REPOSITORY", tmp_path / "checkout")


def configuration():
    services = {
        name: {
            "image": "registry.example/contractops@sha256:" + "a" * 64,
            "environment": {"CONTRACTOPS_ENVIRONMENT": "production"},
        }
        for name in release.SERVICES
    }
    for name, key, role in (
        ("contract-api", "CONTRACTOPS_DATABASE_URL", "app"),
        ("contract-worker", "CONTRACTOPS_WORKER_DATABASE_URL", "worker"),
        ("migrate", "CONTRACTOPS_MIGRATION_DATABASE_URL", "migration"),
    ):
        services[name]["environment"][key] = f"postgresql://{role}:secret@db.example/db"
    return {"services": services}


@pytest.mark.parametrize("image", ["repo:latest", "repo:123", "repo@sha256:short", ""])
def test_rejects_mutable_or_inconsistent_images(image):
    config = configuration()
    config["services"]["ingestion-worker"]["image"] = image
    with pytest.raises(release.ReleaseError, match="immutable"):
        release.validate_config(config, "app", "worker")


def test_grant_roles_match_actual_connections():
    config = configuration()
    assert release.validate_config(config, "app", "worker").endswith("a" * 64)
    with pytest.raises(release.ReleaseError, match="distinct"):
        release.validate_config(config, "wrong-app", "worker")
    config["services"]["migrate"]["environment"]["CONTRACTOPS_MIGRATION_DATABASE_URL"] = (
        "postgresql://app:secret@db.example/db"
    )
    with pytest.raises(release.ReleaseError, match="distinct"):
        release.validate_config(config, "app", "worker")


@pytest.mark.parametrize("url", [
    "http://api.example", "https://user:secret@api.example", "https://api.example?key=secret",
    "https://api.example/prefix", "https://api.example:invalid", "https:///missing",
])
def test_rejects_unsafe_health_origins(url):
    with pytest.raises(release.ReleaseError, match="HTTPS origin"):
        release.validate_url(url)


def test_order_and_no_implicit_dependency_reexecution():
    steps = release.release_steps("app", "worker")
    names = [name for name, _ in steps]
    assert names[:4] == ["pull-image", "preflight-migration", "migrate", "runtime-grants"]
    assert names[4:8] == [f"preflight-{r}" for r in release.PREFLIGHTS]
    assert names[8] == "start-runtime"
    assert len(names[9:]) == 3
    for _, command in steps:
        if command[0] in ("run", "up"):
            assert "--no-deps" in command


def arguments(tmp_path):
    env_file = tmp_path / "release.env"
    env_file.touch()
    return ["--env-file", str(env_file), "--api-url", "https://api.example",
            "--app-role", "app", "--worker-role", "worker"]


def test_dry_run_never_migrates_or_starts(monkeypatch, tmp_path, capsys):
    runner = Mock(return_value=json.dumps(configuration()))
    monkeypatch.setattr(release, "run_command", runner)
    assert release.main(arguments(tmp_path)) == 0
    assert runner.call_count == 1
    assert "DRY RUN" in capsys.readouterr().out


def test_failed_migration_stops_before_grants_or_start(monkeypatch, tmp_path, capsys):
    stages = []

    def run(prefix, args, *, stage):
        stages.append(stage)
        if stage == "migrate":
            raise release.ReleaseError("migrate failed")
        return json.dumps(configuration()) if stage == "compose-config" else ""

    monkeypatch.setattr(release, "run_command", run)
    assert release.main([*arguments(tmp_path), "--apply"]) == 1
    assert stages == ["compose-config", "pull-image", "preflight-migration", "migrate"]
    assert "No automatic rollback" in capsys.readouterr().err


def test_success_requires_public_health_after_all_steps(monkeypatch, tmp_path):
    stages = []

    def run(prefix, args, *, stage):
        stages.append(stage)
        return json.dumps(configuration()) if stage == "compose-config" else ""

    monkeypatch.setattr(release, "run_command", run)
    monkeypatch.setattr(release, "check_public_health", lambda url: stages.append("public-health"))
    assert release.main([*arguments(tmp_path), "--apply"]) == 0
    assert stages == ["compose-config", *[n for n, _ in release.release_steps("app", "worker")],
                      "public-health"]


def test_errors_do_not_echo_subprocess_secrets(monkeypatch):
    runner = Mock(return_value=subprocess.CompletedProcess(
        [], 1, stdout="Bearer jwt-secret", stderr="postgresql://admin:password@db",
    ))
    monkeypatch.setenv("CONTRACTOPS_JWT_SECRET", "ambient-secret")
    monkeypatch.setattr(release.subprocess, "run", runner)
    with pytest.raises(release.ReleaseError) as error:
        release.run_command(["docker"], ["compose"], stage="config")
    assert "password" not in str(error.value) and "jwt-secret" not in str(error.value)
    assert "CONTRACTOPS_JWT_SECRET" not in runner.call_args.kwargs["env"]


def test_redirects_are_not_followed():
    with pytest.raises(release.ReleaseError, match="redirected"):
        release.NoRedirect().redirect_request(None, None, 302, "", {}, "http://other.example")


def test_public_health_requires_correct_payload(monkeypatch):
    response = Mock(status=200)
    response.read.return_value = b'{"status":"ok"}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = context
    monkeypatch.setattr(release, "build_opener", Mock(return_value=opener))
    with pytest.raises(release.ReleaseError, match="public-health-ready"):
        release.check_public_health("https://api.example")
