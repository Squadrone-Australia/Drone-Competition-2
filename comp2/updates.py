"""Tell the operator when COMP2 has a newer version, and what an update changed.

The code itself is only ever updated by ``scripts/install.*`` (the "Start COMP2"
launcher), on start. A copy that is left running, or started some other way,
never hears that ``main`` moved on. So the server asks GitHub once, in the
background, and the page says so. After an update, the page also lists the
commit messages since the version this copy last ran: the changelog.

Everything here is best effort. Offline, rate-limited, or installed some way
that has no version to compare, it says nothing rather than something wrong.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

#: Where the code comes from: the same repository the installers pull.
API = "https://api.github.com/repos/Squadrone-Australia/Drone-Competition-2"
BRANCH = "main"
#: The checkout the running code was imported from.
ROOT = Path(__file__).resolve().parent.parent
#: Written by the zip-download path of ``scripts/install.*``: which commit the
#: files came from, when there is no ``.git`` to ask.
ZIP_MARKER = ".comp2-version"
#: Written here: the version this copy last ran and showed the changelog for.
LAST_RUN_MARKER = ".comp2-last-run"
#: The page lists this many commits, then counts the rest.
MAX_COMMITS = 30
TIMEOUT_S = 10.0

_SHA = re.compile(r"[0-9a-f]{40}")
# Attribution lines are for the repository, not for whoever reads the changelog.
_TRAILER = re.compile(r"^(co-authored-by|signed-off-by):", re.IGNORECASE)


def local_version(root: Path = ROOT) -> str | None:
    """The commit the running code is, or None when it cannot be told."""
    if (root / ".git").exists():
        out = _git(root, "rev-parse", "HEAD")
    else:
        try:
            out = (root / ZIP_MARKER).read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError):
            return None
    out = (out or "").strip()
    return out if _SHA.fullmatch(out) else None


def changes_since_last_run(root: Path = ROOT) -> dict | None:
    """What changed since this copy last ran, or None when nothing did.

    The first run has nothing to compare with, so it shows no changelog. A
    move backwards, or onto history that does not follow on, has no commits in
    between and shows none either. Call :func:`remember_run` once the result
    has been shown, so that each changelog appears for one session only.
    """
    current = local_version(root)
    if current is None:
        return None
    try:
        previous = (root / LAST_RUN_MARKER).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        previous = None
    if not previous or not _SHA.fullmatch(previous):
        remember_run(current, root)
        return None
    if previous == current:
        return None
    if (root / ".git").exists():
        commits = _git_log(root, f"{previous}..{current}")
    else:
        compare = _compare(previous, current)
        if compare is None or compare.get("status") != "ahead":
            commits = None
        else:
            commits = _commits_from_api(compare)
    if not commits:
        remember_run(current, root)
        return None
    return _summary("updated", commits, from_sha=previous, to_sha=current)


def remember_run(version: str, root: Path = ROOT) -> None:
    """Record that ``version``'s changelog has been shown."""
    try:
        (root / LAST_RUN_MARKER).write_text(version + "\n", encoding="ascii")
    except OSError as exc:
        log.debug("could not record the version that ran: %s", exc)


def check_for_update(root: Path = ROOT) -> dict | None:
    """The commits on ``main`` that this copy does not have yet, or None."""
    current = local_version(root)
    if current is None:
        return None
    compare = _compare(current, BRANCH)
    # "behind" is a developer's branch ahead of main, and "diverged" is a copy
    # with commits of its own: the launcher's fast-forward cannot apply either,
    # so telling them to restart for an update would not be true.
    if compare is None or compare.get("status") != "ahead":
        return None
    commits = _commits_from_api(compare)
    if not commits:
        return None
    return _summary(
        "update_available", commits, total=int(compare.get("ahead_by") or 0)
    )


def _summary(kind, commits, *, total=None, from_sha=None, to_sha=None) -> dict:
    # Newest first, as a changelog reads.
    commits = list(reversed(commits))
    msg = {
        "type": kind,
        "total": max(total or 0, len(commits)),
        "commits": commits[:MAX_COMMITS],
    }
    if from_sha:
        msg["from"] = from_sha[:7]
    if to_sha:
        msg["to"] = to_sha[:7]
    return msg


def _entry(sha: str, message: str) -> dict:
    subject, _, body = message.strip().partition("\n")
    body = "\n".join(
        line for line in body.splitlines() if not _TRAILER.match(line.strip())
    ).strip()
    return {"sha": sha[:7], "subject": subject.strip(), "body": body}


def _git(root: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("git %s failed: %s", args[0], exc)
        return None
    return done.stdout if done.returncode == 0 else None


def _git_log(root: Path, span: str) -> list[dict] | None:
    """Commits in ``span``, oldest first, merges left out."""
    out = _git(root, "log", "--no-merges", "--reverse", "--format=%H%x1f%B%x1e", span)
    if out is None:
        return None
    commits = []
    for record in out.split("\x1e"):
        sha, sep, message = record.strip().partition("\x1f")
        if sep and _SHA.fullmatch(sha):
            commits.append(_entry(sha, message))
    return commits


def _compare(base: str, head: str) -> dict | None:
    """GitHub's comparison of two commits, or None when it cannot be had."""
    request = urllib.request.Request(
        f"{API}/compare/{base}...{head}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "comp2-update-check",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            return json.load(reply)
    except Exception as exc:  # offline, rate-limited, unknown commit: all quiet
        log.debug("update check failed: %s", exc)
        return None


def _commits_from_api(compare: dict) -> list[dict]:
    """The compared commits, oldest first, merges left out."""
    commits = []
    for c in compare.get("commits") or []:
        if len(c.get("parents") or []) > 1:
            continue
        sha = c.get("sha") or ""
        message = (c.get("commit") or {}).get("message") or ""
        if sha and message:
            commits.append(_entry(sha, message))
    return commits
