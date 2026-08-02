"""Read ``data/derived/``. **Read only** — this module has no write path at all.

``data/derived/`` belongs to the nightly Action. The admin tool needs it on every screen
because a claim cannot be priced without the roster ESPN reports, so the safe arrangement is a
loader with nowhere to write to: there is no ``dump``, no ``Path.write_text``, and a test
asserts as much.

Franchise names, players and rosters all come from ``{year}.json``, keyed per season. Names
change yearly and one of them carries a double space, so nothing here keys on a name.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rs57.models import FranchiseName, Payout, Player, RosterEntry
from rs57.origins import ORIGINS_FILENAME, load_player_origins

ROOT = Path(__file__).resolve().parent.parent.parent
DERIVED = ROOT / "data" / "derived"


@dataclass(frozen=True)
class DerivedSeason:
    """One season's keeper file, loaded into models."""

    season: int
    drafted: bool
    base_salary_field: str
    trade_deadline: datetime | None
    franchises: tuple[FranchiseName, ...]
    players: tuple[Player, ...]
    roster: tuple[RosterEntry, ...]
    warnings: tuple[str, ...] = ()
    waiver_base_mismatches: tuple[int, ...] = ()

    @property
    def names(self) -> dict[str, str]:
        return {row.manager_id: row.name for row in self.franchises}

    @property
    def player_by_id(self) -> dict[int, Player]:
        return {player.espn_player_id: player for player in self.players}

    @property
    def manager_ids(self) -> tuple[str, ...]:
        """Every franchise with a roster, ordered by team id rather than by name."""
        seen = {entry.manager_id for entry in self.roster}
        return tuple(sorted(seen, key=lambda mid: (len(mid), mid)))

    def roster_for(self, manager_id: str) -> tuple[RosterEntry, ...]:
        return tuple(entry for entry in self.roster if entry.manager_id == manager_id)

    def name_of(self, manager_id: str) -> str:
        return self.names.get(manager_id, manager_id)


@dataclass(frozen=True)
class Derived:
    """The derived directory, as the admin tool sees it."""

    derived_dir: Path = DERIVED
    _cache: dict[int, tuple[float, DerivedSeason]] = field(default_factory=dict, repr=False)
    """Keyed on the file's mtime, not just the year.

    The tool runs for hours and a re-sync is a normal thing to do while it is open — after the
    auction, ``base_salary`` moves from ``keeperValue`` to ``keeperValueFuture`` for the whole
    league. A cache that ignored mtime would keep pricing claims off the roster as it was when
    the first page loaded, and say nothing about it.
    """

    _origins_cache: dict[str, tuple[float, dict[int, int]]] = field(
        default_factory=dict, repr=False
    )
    """The same mtime discipline, for ``player-origins.json``. Running the origins sync while
    the tool is open is normal when a waiver add needs a draft class."""

    def seasons(self) -> list[int]:
        """Years with a keeper file, ascending. Ignores the ``-stats`` companion files."""
        if not self.derived_dir.exists():
            return []
        years = []
        for path in sorted(self.derived_dir.glob("*.json")):
            stem = path.stem
            if stem.startswith(".") or stem.endswith("-stats"):
                continue
            if stem.isdigit():
                years.append(int(stem))
        return sorted(years)

    def current_season(self) -> int | None:
        """The most recent synced season — the one whose keepers are being decided."""
        years = self.seasons()
        return years[-1] if years else None

    def load(self, season: int) -> DerivedSeason | None:
        path = self.derived_dir / f"{season}.json"
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        cached = self._cache.get(season)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        doc = json.loads(path.read_text(encoding="utf-8"))
        source = doc.get("source") or {}
        review = doc.get("review") or {}
        raw_deadline = source.get("trade_deadline")
        loaded = DerivedSeason(
            season=season,
            drafted=bool(source.get("drafted")),
            base_salary_field=str(source.get("base_salary_field") or "unknown"),
            trade_deadline=datetime.fromisoformat(raw_deadline) if raw_deadline else None,
            franchises=tuple(FranchiseName(**row) for row in doc.get("franchises") or []),
            players=tuple(Player(**row) for row in doc.get("players") or []),
            roster=tuple(RosterEntry(**row) for row in doc.get("roster") or []),
            warnings=tuple(review.get("warnings") or []),
            waiver_base_mismatches=tuple(review.get("waiver_base_mismatches") or []),
        )
        self._cache[season] = (mtime, loaded)
        return loaded

    def first_nfl_seasons(self) -> dict[int, int]:
        """``espn_player_id -> first NFL season``, for prospect rule 1.

        Cached on the file's mtime for the same reason ``load`` is: the tool runs for hours,
        and running the origins sync while it is open is a normal thing to do when a waiver
        add needs a draft class. A cache that ignored mtime would keep saying "ESPN has
        nothing for him" after the sync had answered.

        An empty dict is returned for a missing file, and the caller passes it through as an
        empty mapping rather than ``None`` — so every prospect reports unverified instead of
        the rule quietly not running.
        """
        path = self.derived_dir / ORIGINS_FILENAME
        if not path.exists():
            return {}
        mtime = path.stat().st_mtime
        cached = self._origins_cache.get(ORIGINS_FILENAME)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        loaded = load_player_origins(self.derived_dir)
        self._origins_cache[ORIGINS_FILENAME] = (mtime, loaded)
        return loaded

    def payouts(self, season: int) -> list[Payout]:
        """The season's prize rows, as ``stats_sync`` derived them.

        A label can appear more than once — a tie splits a prize — and
        ``winner_manager_id`` can be ``None`` when nobody won it.
        """
        path = self.derived_dir / f"{season}-stats.json"
        if not path.exists():
            return []
        doc = json.loads(path.read_text(encoding="utf-8"))
        return [Payout(**row) for row in doc.get("payouts") or []]

    def derived_consolation_winners(self, season: int) -> tuple[str, ...]:
        """Who ``stats`` thinks won ``season``'s consolation bracket.

        Offered to the settings screen as a **suggestion to confirm**, never applied. A 12-team
        league runs two consolation ladders and ESPN does not say which one the league means;
        reading it wrong waives the wrong team's fees for a year.
        """
        path = self.derived_dir / f"{season}-stats.json"
        if not path.exists():
            return ()
        doc = json.loads(path.read_text(encoding="utf-8"))
        review = doc.get("review") or {}
        return tuple(review.get("consolation_winner_manager_ids") or [])
