import ast
import subprocess

import pytest

from scripts.inject_fault import (
    SUPPORTED_SCENARIOS,
    _validate_repository,
    apply_fault,
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
    }

    source_lines = first_source.decode("utf-8").splitlines()
    assert source_lines[first.line - 1]
    if scenario == "tc01_unit_test":
        assert 'assert order["quantity"] == 4' in source_lines[first.line - 1]
    elif scenario == "tc02_compile_syntax":
        assert "app = create_app(" in source_lines[first.line - 1]
        with pytest.raises(SyntaxError):
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
