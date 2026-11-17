import subprocess
import sys
from pathlib import Path

import pytest
from check_private_strings import find_problems

TOOL = Path(__file__).resolve().parent.parent / "tools" / "check_private_strings.py"
BS = "\\"


def _diff(*added, path="tests/x.py"):
    return f"+++ b/{path}\n" + "".join(f"+{line}\n" for line in added)


def _kinds(diff, terms=None):
    return [kind for _, kind, _ in find_problems(diff, terms)]


def test_flags_user_profile_paths_in_both_slash_styles():
    assert _kinds(_diff("p = 'C:" + BS + "Users" + BS + "alice" + BS + "x'")) == ["user-profile path"]
    assert _kinds(_diff("p = '/C:/Users/alice/Projects'")) == ["user-profile path"]  # secret-scan: allow


def test_placeholder_user_names_pass():
    assert _kinds(_diff("p = 'C:" + BS + "Users" + BS + "YourName" + BS + "x'", "q = 'C:/Users/<you>/x'")) == []


def test_flags_home_paths_but_not_placeholders():
    assert _kinds(_diff("p = '/home/" + "alice/x'")) == ["home path"]
    assert _kinds(_diff("p = '/home/user/x'", "q = '/home/<you>/x'")) == []


def test_flags_email_addresses_except_examples_and_noreply():
    assert _kinds(_diff("a = 'bob" + "@mail.test'")) == ["email address"]
    assert _kinds(_diff("a = 'bob" + "@example.com'", "b = '123+bob" + "@users.noreply.github.com'")) == []


def test_flags_real_looking_ids_but_not_test_fixtures():
    real = "-".join(["3fa326b7", "c770", "80e0", "89c3", "c92459e4cd1b"])
    assert _kinds(_diff(f"PAGE = '{real}'")) == ["id (Notion page or database id?)"]
    assert _kinds(_diff(f"PAGE = '{real.replace('-', '')}'")) == ["id (Notion page or database id?)"]
    assert _kinds(_diff("DB = 'aaaaaaaa-0000-0000-0000-000000000001'")) == []


def test_configured_terms_are_case_insensitive():
    assert _kinds(_diff("host = 'Some-Host-1'"), ["some-host"]) == ["configured term"]
    assert _kinds(_diff("host = 'other'"), ["some-host"]) == []


def test_allow_marker_skips_a_line():
    assert _kinds(_diff("p = '/home/" + "alice/x'  # secret-scan: allow")) == []


def test_only_added_lines_are_checked():
    diff = "+++ b/a.py\n-old = '/home/" + "alice/x'\n context = '/home/" + "alice/y'\n"
    assert find_problems(diff) == []


def test_reports_the_file_path():
    assert find_problems(_diff("p = '/home/" + "alice/x'", path="docs/a.md"))[0][0] == "docs/a.md"


@pytest.fixture
def repo(tmp_path):
    for cmd in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=tmp_path, check=True)
    return tmp_path


def _run_hook(repo):
    return subprocess.run([sys.executable, str(TOOL)], cwd=repo, capture_output=True, text=True)


def test_hook_blocks_a_leak_and_passes_a_clean_commit(repo):
    (repo / "ok.txt").write_text("nothing private here\n")
    subprocess.run(["git", "add", "ok.txt"], cwd=repo, check=True)
    assert _run_hook(repo).returncode == 0

    (repo / "bad.txt").write_text("path = /home/" + "alice/data\n")
    subprocess.run(["git", "add", "bad.txt"], cwd=repo, check=True)
    result = _run_hook(repo)
    assert result.returncode == 1
    assert "bad.txt" in result.stderr and "home path" in result.stderr


def test_hook_reads_configured_terms_from_git_config(repo):
    subprocess.run(["git", "config", "secretscan.internalterms", "my-host, other"], cwd=repo, check=True)
    (repo / "a.txt").write_text("server is my-host\n")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    result = _run_hook(repo)
    assert result.returncode == 1 and "configured term" in result.stderr
