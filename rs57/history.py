"""The only writer of ``data/history/``.

Three directories, three writers, and none of them overlaps: the nightly Action owns
``data/derived/``, the admin tool owns ``data/manual/`` through ``admin.store.ManualStore``,
and this owns ``data/history/``. NO FILE HAS TWO WRITERS. A third writer with a fourth
directory would not be an excuse to relax that, so this carries the same guard the admin tool
does — :meth:`HistoryStore.path` refuses any path that escapes ``data/history/``.

It also carries one the others do not need.

Frozen means frozen
-------------------

``data/history/`` is written **once per completed season and then never again**. Not by a
re-sync, not by a "fix", not by this module's own importer run twice.
:meth:`HistoryStore.write_season` therefore *raises* on an existing file rather than warning
— a warning is something you read after the write has already happened.

This is the one directory in the repo where a second write is not untidy but destructive.
``derived/`` can be rebuilt from ESPN and ``manual/`` is under review before every commit;
a frozen season is the only record of what a manager was actually charged in a year ESPN has
since stopped agreeing about. Correcting one is a deliberate act: delete the file in a commit
that says why, and write it again.

Reading
-------

``validate.py`` reads claims and overrides from ``history/`` and ``manual/`` alike and does not
care which file a row came from — the ratchet audit compares one season against the next, and
a claim recorded last August is what this August's base has to match.

Writes **merge into the loaded document**, the way ``ManualStore`` does, so an ``_about`` key
survives. Every file here carries one, because a frozen season is exactly the thing nobody
will remember the provenance of.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rs57.models import KeeperClaim, KeeperSlot, SalaryOverride, dump_json

DATA = Path(__file__).resolve().parent.parent / "data"


class OwnershipError(RuntimeError):
    """A write was aimed outside ``data/history/``.

    The mirror of ``admin.store.OwnershipError``, and raised for the same reason: the failure
    mode is not a typo but a plausible refactor, in a module that reads ``data/derived/`` on
    every import run and therefore always has a derived path within arm's reach of a write.
    """


class FrozenSeasonError(RuntimeError):
    """A completed season already on disk was about to be written a second time."""


@dataclass(frozen=True)
class HistoryStore:
    """Reads and writes ``data/history/``. Reads nothing else, writes nothing else.

    ``data_dir`` is injectable so tests get a real store over a temporary tree rather than a
    mocked one — the guards are part of what is under test.
    """

    data_dir: Path = DATA

    # -- paths ------------------------------------------------------------------

    @property
    def history(self) -> Path:
        return self.data_dir / "history"

    def path(self, name: str) -> Path:
        """The full path of a file in ``data/history/``, refusing anything that escapes it."""
        candidate = (self.history / name).resolve()
        history = self.history.resolve()
        if candidate == history or history not in candidate.parents:
            raise OwnershipError(
                f"{candidate} is not inside {history} — this module writes data/history/ and "
                f"nothing else. data/derived/ belongs to the nightly Action and data/manual/ "
                f"to the admin tool."
            )
        return candidate

    def season_path(self, year: int) -> Path:
        return self.path(f"{year}.json")

    # -- reading ----------------------------------------------------------------

    def seasons(self) -> list[int]:
        """Every season frozen on disk, oldest first."""
        if not self.history.exists():
            return []
        found: list[int] = []
        for path in self.history.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                found.append(int(path.stem))
            except ValueError:
                continue
        return sorted(found)

    def exists(self, year: int) -> bool:
        return self.season_path(year).exists()

    def load(self, year: int) -> dict[str, Any]:
        """One frozen season as a plain dict, or ``{}`` when it has not been written."""
        path = self.season_path(year)
        if not path.exists():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"data/history/{year}.json is not a JSON object")
        return loaded

    def claims(self, season: int | None = None) -> list[KeeperClaim]:
        """Recorded claims from the frozen seasons.

        The season key and each row's own ``season`` field must agree, the same redundancy
        ``ManualStore`` keeps: a row stays loadable into ``KeeperClaim`` on its own, and a
        disagreement is a corrupted file rather than something to guess at.
        """
        out: list[KeeperClaim] = []
        for year in self.seasons():
            if season is not None and year != season:
                continue
            for row in self.load(year).get("claims") or []:
                claim = KeeperClaim(**row)
                if claim.season != year:
                    raise ValueError(
                        f"history/{year}.json: a claim row says season {claim.season}"
                    )
                out.append(claim)
        return out

    def overrides(self, season: int | None = None) -> list[SalaryOverride]:
        out: list[SalaryOverride] = []
        for year in self.seasons():
            if season is not None and year != season:
                continue
            out += [SalaryOverride(**row) for row in self.load(year).get("overrides") or []]
        return out

    def prospect_ids(self, before_season: int | None = None) -> set[int]:
        """Players kept in the PROSPECT slot in a frozen season.

        This is what retires ``data/manual/prospects.json``: the legacy file recorded these by
        hand only because no claim carried a slot. Now that completed seasons do, the answer
        is derived from ``slot == PROSPECT`` instead of transcribed.
        """
        return {
            claim.espn_player_id
            for claim in self.claims()
            if claim.slot is KeeperSlot.PROSPECT
            and (before_season is None or claim.season < before_season)
        }

    def started_player_ids(self, through_season: int | None = None) -> set[int]:
        """Every player any league team has ever *started*, across the frozen seasons.

        League history, read by nothing today. It was collected for a prospect rule requiring
        that a prospect had never been started by any team in the league, and that rule is
        **retired** (commissioner, 2026-07-30): prospects may be started, and the only
        remaining stipulation is that they are rookies.

        The horizon is ESPN's, not a choice — box scores answer back to 2019 and no further.
        """
        found: set[int] = set()
        for year in self.seasons():
            if through_season is not None and year > through_season:
                continue
            found |= {int(pid) for pid in self.load(year).get("started_player_ids") or []}
        return found

    # -- writing ----------------------------------------------------------------

    def write_season(
        self,
        year: int,
        document: dict[str, Any],
        *,
        about: Iterable[str] = (),
    ) -> Path:
        """Freeze one completed season. Refuses outright if it is already on disk.

        The refusal is the point, and it is a raise rather than a return value so that a
        caller cannot carry on having ignored it. An importer run twice is the ordinary way
        this gets attempted, and the second run is exactly the one with less information.
        """
        path = self.season_path(year)
        if path.exists():
            raise FrozenSeasonError(
                f"data/history/{year}.json already exists and data/history/ is written once "
                f"per completed season, then frozen. Nothing re-writes a frozen season — not "
                f"a re-sync, not a fix, not this importer run twice. To correct one, delete "
                f"the file in a commit that says why, then import it again."
            )
        doc: dict[str, Any] = {}
        if about:
            doc["_about"] = list(about)
        doc.update(document)
        doc["season"] = year
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(doc, path)
        return path
