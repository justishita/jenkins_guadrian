import ast
import subprocess

import pytest

from scripts.inject_fault import (
    SUPPORTED_SCENARIOS,
    _multibranch_build_url,
    _validate_repository,
    apply_fault,
    trigger_multibranch_build,
)


def _copy_target_app(repo, source_root):
    import shutil

    for relative in (
        "target_app/app/main.py",
        "target_app/tests/test_orders.py",
    ):
        destination = repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative, destination)


@pytest.mark.parametrize("scenario", SUPPORTED_SCENARIOS)
def test_fault_scenarios_are_idempotent_and_report_ground_truth(
    tmp_path,
    scenario,
):
    from pathlib import Path

    repo = tmp_path / "repo"
    repo.mkdir()
    source_root = Path(__file__).resolve().parents[2]
    _copy_target_app(repo, source_root)

    first = apply_fault(repo, scenario)
    first_source = (repo / first.file).read_bytes()
    second = apply_fault(repo, scenario)

    assert first == second
    assert (repo / first.file).read_bytes() == first_source
    assert first.line > 0
    assert first.expected_failure_type in {
        "code_test_failure",
        "build_compilation_failure",
        "dependency_regression",
        "flaky_test",
    }

    source_lines = first_source.decode("utf-8").splitlines()
    assert source_lines[first.line - 1]
    if scenario == "tc01_unit_test":
        assert 'assert order["quantity"] == 4' in source_lines[first.line - 1]
    elif scenario == "tc02_compile_syntax":
        assert "app = create_app(" in source_lines[first.line - 1]
        with pytest.raises(SyntaxError):
            ast.parse(first_source.decode("utf-8"))
    elif scenario == "tc10_flaky":
        assert "assert random.random() < 0.5" in source_lines[first.line - 1]
        ast.parse(first_source.decode("utf-8"))
    else:
        assert "_jenkins_fault_injection_missing_module_" in source_lines[first.line - 1]
        ast.parse(first_source.decode("utf-8"))


def test_fault_creation_refuses_main(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "test"],
        check=True,
        capture_output=True,
    )

    with pytest.raises(RuntimeError, match="refusing to run on main"):
        _validate_repository(repo)


def test_multibranch_trigger_url_double_encodes_branch_slash() -> None:
    url = _multibranch_build_url("http://jenkins:8080", "fault/tc10_flaky-abc", job_token="secret")
    assert url == (
        "http://jenkins:8080/job/target-app/job/fault%252Ftc10_flaky-abc/build?token=secret"
    )


def test_trigger_posts_eight_builds_with_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[object] = []

    class _Resp:
        headers = {"Location": "http://jenkins:8080/queue/item/1/"}
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self) -> bytes:
            return b'{"crumb":"c","crumbRequestField":"Jenkins-Crumb"}'

    def fake_urlopen(req, timeout=15):
        captured.append(req)
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    locations = trigger_multibranch_build(
        "fault/tc10_flaky-abc",
        runs=8,
        jenkins_url="http://jenkins:8080",
        username="user",
        api_token="api-token",
        job_token="job-token",
    )

    assert len(locations) == 8
    build_posts = [req for req in captured if req.get_method() == "POST"]
    assert len(build_posts) == 8
    assert build_posts[0].get_full_url().endswith(
        "/job/target-app/job/fault%252Ftc10_flaky-abc/build?token=job-token"
    )
    assert build_posts[0].get_header("Authorization")
