"""The commit-msg hook that keeps commit headers in Conventional Commits form (D-033)."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "commit_message", ROOT / "scripts/check_commit_message.py"
)
assert _spec and _spec.loader
commit_message = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(commit_message)


@pytest.mark.parametrize(
    "message",
    [
        "fix(n26): type Authorization by sign\n",
        "feat(web)!: replace the chart command\n\nWhy it changed.\n\nRefs: D-032\n",
        "docs: renumber decisions\n",
        "# a comment git adds\nci: run on pull requests and main\n",
        "Merge branch 'main' into cash-positions-chart\n",
        "fixup! fix(n26): type Authorization by sign\n",
        # `git commit -v`: the diff below the scissors line isn't part of the message
        "docs: fix a link\n# ------------------------ >8 ------------------------\ndiff --git\n",
    ],
)
def test_conventional_and_git_written_messages_pass(message):
    assert commit_message.find_problems(message) == []


@pytest.mark.parametrize(
    "message, problem",
    [
        ("Add a chart\n", "doesn't read"),
        ("feature(web): add a chart\n", "doesn't read"),  # not a known type
        ("feat(Web): add a chart\n", "doesn't read"),  # scope is lower-case
        ("feat(web):add a chart\n", "doesn't read"),
        ("feat(web): " + "x" * 62 + "\n", "over 72"),
        ("feat(web): add a chart.\n", "ends with a period"),
        ("feat(web): add a chart\nbody straight after\n", "no blank line"),
        ("# only a comment\n\n", "empty"),
    ],
)
def test_other_messages_are_refused_with_the_reason(message, problem):
    assert any(problem in p for p in commit_message.find_problems(message))


def test_the_hook_exits_nonzero_on_a_bad_message(tmp_path, capsys):
    message_file = tmp_path / "COMMIT_EDITMSG"
    message_file.write_text("wip\n")
    assert commit_message.main([str(message_file)]) == 1
    assert "type one of: feat, fix" in capsys.readouterr().out
