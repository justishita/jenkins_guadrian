"""scripts/prune_evidence.py: old incident folders go, recent and non-incident ones stay, dry run changes nothing."""

import os
import runpy
import time
from pathlib import Path

import pytest

SCRIPT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "prune_evidence.py"))
find_old, main = SCRIPT["find_old"], SCRIPT["main"]

OLD = "11111111-1111-4111-8111-111111111111"
NEW = "22222222-2222-4222-8222-222222222222"
MIXED = "33333333-3333-4333-8333-333333333333"
DAY = 86400


def make(root: Path, name: str, age_days: float, files=("metrics_agent.json",)) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    for file in files:
        path = directory / file
        path.write_text("{}", encoding="utf-8")
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))
    return directory


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "evidence"
    make(root, OLD, 45)
    make(root, NEW, 2)
    make(root, "not-an-incident", 400)  # never touched: not named like an incident
    return root


def test_only_old_incident_folders_are_found(root):
    assert [c.path.name for c in find_old([root], 30)] == [OLD]


def test_a_folder_with_any_recent_file_is_kept(tmp_path):
    root = tmp_path / "evidence"
    directory = make(root, MIXED, 90)
    recent = directory / "code_agent.json"
    recent.write_text("{}", encoding="utf-8")  # changed just now: the incident is still active
    assert find_old([root], 30) == []


def test_a_dry_run_deletes_nothing_and_says_how_to_apply(root, capsys):
    assert main(["--root", str(root), "--older-than-days", "30"]) == 0
    assert (root / OLD).exists()
    out = capsys.readouterr().out
    assert "would delete" in out and OLD in out and "--apply" in out


def test_apply_deletes_only_the_old_incident_folder(root, capsys):
    assert main(["--root", str(root), "--older-than-days", "30", "--apply"]) == 0
    assert not (root / OLD).exists()
    assert (root / NEW).exists() and (root / "not-an-incident").exists()
    assert "deleted 1 incident folder(s)" in capsys.readouterr().out


def test_a_missing_root_is_not_an_error(tmp_path, capsys):
    assert main(["--root", str(tmp_path / "nothing")]) == 0
    assert "would delete 0" in capsys.readouterr().out


def test_a_negative_window_is_refused(root):
    with pytest.raises(SystemExit):
        main(["--root", str(root), "--older-than-days", "-1"])
