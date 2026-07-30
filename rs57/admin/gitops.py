"""The commit button: show the diff, then commit and push ``data/manual/``.

This is the reverse of the guard in ``.github/workflows/nightly.yml``. That workflow stages
``data/derived/`` and ``site/`` and fails if anything else appears in the index; this stages
``data/manual/`` and fails if anything else appears in the index. Neither trusts the other to
have behaved.

Two things this deliberately does not do:

* **It never stages a path it was not asked to.** ``git add -- data/manual`` and nothing wider.
  A dirty ``data/derived/`` — which is exactly what a local ``rs57.sync`` leaves behind — is
  reported so the commissioner can see it is being left alone, not swept in.
* **It never commits without the diff having been rendered first.** The preview *is* the review
  step. There is no pull request between this button and a public repo, so if the diff is not
  read, nothing is.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

OWNED = "data/manual/"
"""The only path prefix this tool may commit. Everything else has another writer."""

FORBIDDEN = ("data/derived/", "data/history/", "site/")
"""Named for the error message. ``derived/`` and ``site/`` are the nightly Action's;
``history/`` is frozen once a season completes."""


class GitError(RuntimeError):
    """A git command failed, or was about to commit something it does not own."""


@dataclass(frozen=True)
class Change:
    status: str
    path: str

    @property
    def owned(self) -> bool:
        return self.path.startswith(OWNED)

    @property
    def label(self) -> str:
        return {
            "??": "new",
            " M": "modified",
            "M ": "modified",
            "MM": "modified",
            "A ": "added",
            " D": "deleted",
            "D ": "deleted",
        }.get(self.status, self.status.strip() or "changed")


@dataclass(frozen=True)
class CommitPreview:
    """What the button would commit, and what it would leave alone."""

    branch: str
    changes: tuple[Change, ...]
    diff: str
    other_changes: tuple[Change, ...]
    remote: str | None
    error: str | None = None

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    @property
    def file_count(self) -> int:
        return len(self.changes)


@dataclass
class Git:
    """Git over one working tree. Every call is explicit; nothing runs on import."""

    repo: Path = ROOT
    _ran: list[list[str]] = field(default_factory=list, repr=False)

    def run(self, *args: str, check: bool = True) -> str:
        """Run a git command in the repo and return stdout.

        ``check=False`` is for the queries where a non-zero exit is an answer rather than a
        failure — ``git remote get-url`` on a repo with no remote, for one.
        """
        self._ran.append(list(args))
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        if check and result.returncode != 0:
            raise GitError(
                f"git {' '.join(args)} failed ({result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result.stdout

    # -- reading ----------------------------------------------------------------

    def branch(self) -> str:
        return self.run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def remote(self) -> str | None:
        url = self.run("remote", "get-url", "origin", check=False).strip()
        return url or None

    def changes(self) -> list[Change]:
        """Every change in the working tree, owned or not.

        ``-uall`` lists the files inside an untracked directory instead of collapsing them to
        ``data/derived/``. Both the ownership check and the "left alone, deliberately" list are
        about specific files, and a bare directory name tells the reader nothing about which.
        """
        out = self.run("status", "--porcelain", "-uall")
        found: list[Change] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            found.append(Change(status=line[:2], path=line[3:].strip().strip('"')))
        return found

    def preview(self) -> CommitPreview:
        """The diff of ``data/manual/``, including files git has never seen.

        ``--intent-to-add`` is what makes a brand-new ``claims.json`` show up as a diff instead
        of as an untracked path with no content. It records the path in the index without
        staging its content, so this stays a read of the working tree.
        """
        error = None
        try:
            self.run("add", "--intent-to-add", "--", OWNED.rstrip("/"), check=False)
            diff = self.run("diff", "--", OWNED.rstrip("/"))
        except GitError as exc:
            diff, error = "", str(exc)

        every = self.changes()
        return CommitPreview(
            branch=self.branch(),
            changes=tuple(change for change in every if change.owned),
            diff=diff,
            other_changes=tuple(change for change in every if not change.owned),
            remote=self.remote(),
            error=error,
        )

    # -- writing ----------------------------------------------------------------

    def commit_and_push(self, message: str, *, push: bool = True) -> list[str]:
        """Stage ``data/manual/``, verify the index, commit, and push.

        The index check is not a formality. It runs *after* staging and *before* committing,
        and it is the last thing standing between a refactor that widened a path and a commit
        that overwrites a file the nightly Action owns.
        """
        log: list[str] = []
        self.run("add", "--", OWNED.rstrip("/"))

        staged = [line for line in self.run("diff", "--cached", "--name-only").splitlines() if line]
        if not staged:
            raise GitError("nothing to commit in data/manual/")

        trespass = [path for path in staged if not path.startswith(OWNED)]
        if trespass:
            # Leave the index as it is rather than tidying up: whoever reads this message needs
            # to see what was staged, and `git reset` would hide it.
            forbidden = [p for p in trespass if p.startswith(FORBIDDEN)]
            raise GitError(
                f"refusing to commit: {len(trespass)} staged path(s) outside {OWNED} "
                f"({', '.join(trespass[:5])})."
                + (
                    f" {', '.join(forbidden)} belongs to the nightly Action or to frozen "
                    f"history — this tool must never write it."
                    if forbidden
                    else ""
                )
            )

        log.append(f"staged {len(staged)} file(s): {', '.join(staged)}")
        self.run("commit", "-m", message)
        log.append(self.run("log", "-1", "--oneline").strip())

        if not push:
            log.append("not pushed (push disabled)")
            return log
        if self.remote() is None:
            log.append("no origin remote — committed locally, nothing pushed")
            return log
        self.run("push")
        log.append(f"pushed to {self.branch()}")
        return log


def commit_message(season: int, summary: str) -> str:
    """A subject line that says which season and what changed.

    The nightly Action's commits all read "Nightly: ...", so a manual commit that says what a
    human did keeps the two apart in the log at a glance.
    """
    return f"Admin: {summary} ({season})"
