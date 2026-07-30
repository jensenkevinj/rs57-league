"""The commit button: show the diff, then commit and push ``data/manual/``.

This is the reverse of the guard in ``.github/workflows/nightly.yml``. That workflow stages
``data/derived/`` and ``site/`` and fails if anything else appears in the index; this stages
``data/manual/`` and fails if anything else appears in the index. Neither trusts the other to
have behaved.

Three things this deliberately does not do:

* **It never stages a path it was not asked to.** ``git add -- data/manual`` and nothing wider.
  A dirty ``data/derived/`` — which is exactly what a local ``rs57.sync`` leaves behind — is
  reported so the commissioner can see it is being left alone, not swept in.
* **It never commits without the diff having been rendered first.** The preview *is* the review
  step. There is no pull request between this button and a public repo, so if the diff is not
  read, nothing is.
* **It never writes anything in order to render the preview.** See ``Git.preview`` — the
  ``--intent-to-add`` this used to run left index entries behind that another commit would
  write out as empty files.
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

    def _new_file_diff(self, path: str) -> str:
        """A ``git diff``-shaped rendering of a file git has never seen, read from the tree.

        Not byte-identical to what git would print for an added file, and it does not need to
        be — it needs to be readable and it needs to be a *read*. The commit page is the only
        review step between this tool and a public repo, so a new file has to show its
        contents, not just its name.
        """
        try:
            body = (self.repo / path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return (
                f"diff --git a/{path} b/{path}\nnew file\n--- /dev/null\n+++ b/{path}\n"
                f"@@ unreadable: {exc} @@"
            )
        lines = body.splitlines()
        return "\n".join(
            [
                f"diff --git a/{path} b/{path}",
                "new file",
                "--- /dev/null",
                f"+++ b/{path}",
                f"@@ -0,0 +1,{len(lines)} @@",
                *(f"+{line}" for line in lines),
            ]
        )

    def preview(self) -> CommitPreview:
        """The diff of ``data/manual/``, including files git has never seen.

        **This runs no command that changes the repository.** An earlier version reached for
        ``git add --intent-to-add`` to make a brand-new ``claims.json`` show up in ``git
        diff``. For the button's own flow that was harmless, because ``commit_and_push``
        stages the real content immediately after — but the entries stayed in the index once
        the page was closed, and **an intent-to-add entry that gets committed is committed as
        an empty file**. Any other commit made while the tool was open swept them in and
        blanked them. That is not hypothetical; it happened while committing Phase 4. What
        made it nasty is that ``git diff --cached --name-only`` did not list the paths, so
        they looked absent from the staged set right up until the commit wrote them empty.

        New files are rendered from ``git status`` plus a direct read instead. A preview that
        mutates the repository in order to describe the repository is the wrong shape however
        safe the mutation happens to be today — and ``data/history/`` now has a writer, where
        an empty file committed over a frozen season is exactly the loss the freeze exists to
        prevent.
        """
        error = None
        every: list[Change] = []
        try:
            every = self.changes()
            diff = self.run("diff", "--", OWNED.rstrip("/"))
        except GitError as exc:
            diff, error = "", str(exc)

        untracked = [
            change for change in every if change.owned and change.status.strip() == "??"
        ]
        diff = "\n".join(
            part
            for part in [diff.rstrip("\n"), *(self._new_file_diff(c.path) for c in untracked)]
            if part
        )

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
