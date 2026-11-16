import json
import subprocess

import pytest
from fastapi.testclient import TestClient

from comp2 import updates
from comp2.drone.mock import MockDrone
from comp2.server import create_app


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(root, message):
    (root / "f.txt").write_text(message, encoding="utf-8")
    git(root, "add", "f.txt")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    commit(tmp_path, "first")
    return tmp_path


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if anything here would really ask GitHub."""

    def refuse(*_args):
        raise AssertionError("tests must not reach GitHub")

    monkeypatch.setattr(updates, "_compare", refuse)


def api_commit(sha, message, parents=1):
    return {
        "sha": sha,
        "commit": {"message": message},
        "parents": [{}] * parents,
    }


# --- what changed since the last run ------------------------------------------


def test_first_run_shows_no_changelog_but_remembers_the_version(repo, no_network):
    assert updates.changes_since_last_run(repo) is None
    marker = (repo / updates.LAST_RUN_MARKER).read_text().strip()
    assert marker == updates.local_version(repo)


def test_an_update_lists_its_commit_messages_newest_first(repo, no_network):
    before = updates.local_version(repo)
    updates.remember_run(before, repo)
    commit(repo, "fix: one\n\nWhy it was broken.\n\nCo-Authored-By: Someone <a@b>")
    after = commit(repo, "feat: two")

    changes = updates.changes_since_last_run(repo)

    assert changes["type"] == "updated"
    assert (changes["from"], changes["to"]) == (before[:7], after[:7])
    assert [c["subject"] for c in changes["commits"]] == ["feat: two", "fix: one"]
    # The attribution trailer is for the repository, not the changelog's reader.
    assert changes["commits"][1]["body"] == "Why it was broken."
    # Not remembered until the page has it: see the server tests below.
    assert updates.changes_since_last_run(repo) is not None
    updates.remember_run(after, repo)
    assert updates.changes_since_last_run(repo) is None


def test_moving_backwards_shows_no_changelog(repo, no_network):
    old = updates.local_version(repo)
    new = commit(repo, "feat: newer")
    updates.remember_run(new, repo)
    git(repo, "checkout", "-q", old)
    assert updates.changes_since_last_run(repo) is None


def test_a_long_changelog_is_cut_and_counted(repo, no_network, monkeypatch):
    monkeypatch.setattr(updates, "MAX_COMMITS", 2)
    updates.remember_run(updates.local_version(repo), repo)
    for n in range(5):
        commit(repo, f"change {n}")
    changes = updates.changes_since_last_run(repo)
    assert changes["total"] == 5
    assert [c["subject"] for c in changes["commits"]] == ["change 4", "change 3"]


def test_a_zip_install_asks_github_what_changed(tmp_path, monkeypatch):
    old, new = "a" * 40, "b" * 40
    (tmp_path / updates.ZIP_MARKER).write_text(new)
    updates.remember_run(old, tmp_path)
    monkeypatch.setattr(
        updates,
        "_compare",
        lambda base, head: (
            {
                "status": "ahead",
                "ahead_by": 2,
                "commits": [
                    api_commit("c" * 40, "fix: thing"),
                    api_commit("d" * 40, "Merge pull request #9", parents=2),
                ],
            }
            if (base, head) == (old, new)
            else None
        ),
    )
    changes = updates.changes_since_last_run(tmp_path)
    assert [c["subject"] for c in changes["commits"]] == ["fix: thing"]


def test_no_version_to_compare_says_nothing(tmp_path, no_network):
    assert updates.local_version(tmp_path) is None
    assert updates.changes_since_last_run(tmp_path) is None
    assert updates.check_for_update(tmp_path) is None


# --- is there a newer main ----------------------------------------------------


@pytest.mark.parametrize("status", ["behind", "identical", "diverged"])
def test_only_a_copy_main_can_fast_forward_is_told_to_update(repo, monkeypatch, status):
    """A developer's branch ahead of main, or a copy with commits of its own,
    cannot be updated by the launcher, so it is not told to restart for one."""
    monkeypatch.setattr(
        updates,
        "_compare",
        lambda base, head: {"status": status, "commits": [api_commit("c" * 40, "x")]},
    )
    assert updates.check_for_update(repo) is None


def test_a_newer_main_is_announced_with_its_commits(repo, monkeypatch):
    here = updates.local_version(repo)
    asked = []

    def compare(base, head):
        asked.append((base, head))
        return {
            "status": "ahead",
            "ahead_by": 3,
            "commits": [api_commit("c" * 40, "one"), api_commit("d" * 40, "two")],
        }

    monkeypatch.setattr(updates, "_compare", compare)
    news = updates.check_for_update(repo)
    assert asked == [(here, "main")]
    assert news["type"] == "update_available"
    assert news["total"] == 3
    assert [c["subject"] for c in news["commits"]] == ["two", "one"]


def test_offline_says_nothing(repo, monkeypatch):
    monkeypatch.setattr(updates, "_compare", lambda base, head: None)
    assert updates.check_for_update(repo) is None


# --- the page ------------------------------------------------------------------

CHANGES = {
    "type": "updated",
    "total": 1,
    "commits": [{"sha": "1234567", "subject": "fix: x", "body": ""}],
    "from": "aaaaaaa",
    "to": "bbbbbbb",
}
NEWS = {
    "type": "update_available",
    "total": 1,
    "commits": [{"sha": "7654321", "subject": "feat: y", "body": ""}],
}


def collect_types(ws, want, limit=60):
    seen = {}
    for _ in range(limit):
        msg = ws.receive()
        if "text" in msg:
            data = json.loads(msg["text"])
            if data["type"] in want:
                seen[data["type"]] = data
                if set(seen) == set(want):
                    return seen
    raise AssertionError(f"never got {want}; got {sorted(seen)}")


def first_frame(ws, limit=60):
    for _ in range(limit):
        msg = ws.receive()
        if msg.get("bytes"):
            return msg["bytes"]
    raise AssertionError("no video frame arrived")


def test_the_page_gets_the_changelog_and_the_news(monkeypatch):
    remembered = []
    monkeypatch.setattr(updates, "changes_since_last_run", lambda: CHANGES)
    monkeypatch.setattr(updates, "check_for_update", lambda: NEWS)
    monkeypatch.setattr(updates, "local_version", lambda: "b" * 40)
    monkeypatch.setattr(updates, "remember_run", lambda v: remembered.append(v))
    app = create_app(MockDrone(), check_updates=True)
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            got = collect_types(ws, {"updated", "update_available"})
        # A page that connects later, in the same session, still gets both.
        with client.websocket_connect("/ws") as ws:
            assert collect_types(ws, {"updated", "update_available"}) == got
    assert got["updated"] == CHANGES and got["update_available"] == NEWS
    # Recorded once a page had it, so the next start does not repeat it.
    assert remembered == ["b" * 40]


def test_a_failing_check_takes_nothing_down(monkeypatch):
    def boom():
        raise RuntimeError("unexpected reply")

    monkeypatch.setattr(updates, "changes_since_last_run", boom)
    monkeypatch.setattr(updates, "check_for_update", boom)
    app = create_app(MockDrone(), check_updates=True)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        assert first_frame(ws)[:2] == b"\xff\xd8"


def test_apps_built_without_asking_never_check(monkeypatch):
    """Tests and embedders build apps too; they must not touch the network or
    write a marker into the checkout."""

    def refuse():
        raise AssertionError("checked for updates without being asked to")

    monkeypatch.setattr(updates, "changes_since_last_run", refuse)
    monkeypatch.setattr(updates, "check_for_update", refuse)
    app = create_app(MockDrone())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        assert first_frame(ws)[:2] == b"\xff\xd8"
