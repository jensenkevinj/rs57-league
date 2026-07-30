"""Pydantic models — the schema for everything stored under ``data/``.

Load JSON into these immediately; never pass raw dicts around. Write
``roster.players[3].salary``, not ``data["players"][3]["salary"]``.

**Models enforce types; ``keeper_rules`` enforces rules.** A model rejects a float where
dollars belong, or an unknown key from a schema drift. It does not decide whether a fee
allocation is legal — that is the engine's job, and it reports violations as structured
``ValidationIssue`` values rather than raising.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

# All money is integer dollars. No floats, no cents. ``strict`` matters: without it Pydantic
# happily coerces 12.0 -> 12 and 12.7 -> error, which means a float leaking in from a
# spreadsheet import would silently succeed for whole dollars and explode for cents.
Money = Annotated[int, Field(strict=True)]
NonNegMoney = Annotated[int, Field(strict=True, ge=0)]


class Position(StrEnum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    DEF = "DEF"


class KeeperSlot(StrEnum):
    K1 = "K1"
    K2 = "K2"
    K3 = "K3"
    PROSPECT = "PROSPECT"


class AcquisitionSource(StrEnum):
    DRAFT = "draft"
    WAIVER = "waiver"
    FAAB = "faab"
    TRADE = "trade"


class Base(BaseModel):
    """Shared config.

    ``extra="forbid"`` turns schema drift into a loud load-time failure instead of a silent
    ``None``. ``frozen=True`` keeps the engine honest about purity and makes models hashable.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Manager(Base):
    """A league member. Deliberately has NO email field — see ``data/private/``."""

    id: str
    display_name: str
    espn_team_id: int
    active: bool = True


class FranchiseName(Base):
    """Team names change every season, so they are keyed per-season and never used as an id.

    One of them carries a double space (``Belichick's  Spy``) that has already leaked into the
    spreadsheets. Key franchises on ``espn_team_id``.
    """

    manager_id: str
    season: int
    name: str


class Season(Base):
    """Per-season settings.

    ``consolation_winner_id`` is the manager who won **this** season's consolation bracket.
    Their keeper fees are waived for ``year + 1``. Read it off by one year and every waiver
    lands on the wrong team: the ``*`` beside ``Bijan's Mustard`` in the 2025 fee allocations
    comes from ``Season(year=2024).consolation_winner_id``.
    """

    year: int
    season_start: datetime | None = None
    trade_deadline: datetime | None = None
    keeper_deadline: datetime | None = None
    consolation_winner_id: str | None = None


class Player(Base):
    espn_player_id: int
    name: str
    position: Position
    nfl_team: str


class RosterEntry(Base):
    """One player on one manager's roster for one season.

    ``base_salary`` is **what this player cost his manager for THIS season** — not his original
    acquisition value. That distinction is the entire keeper ratchet and it is not obvious from
    the field name.

    Keepers are entered into ESPN's auction at their keeper price, so ESPN's per-season value
    already carries every prior fee and tax forward. Puka Nacua: acquired off waivers for $0,
    kept for $0 in 2024 (no tax on a first keep), kept for $5 in 2025, and ESPN reports his
    2025 base as $5. Next season's base is this season's computed salary.

    ``kept_prior_year`` is a property of the player's history, not of this roster. It survives
    a trade to a new manager and is cleared only by a drop. Modelled explicitly so it cannot
    silently drift.
    """

    season: int
    manager_id: str
    espn_player_id: int
    acquired_at: datetime
    base_salary: NonNegMoney
    kept_prior_year: bool
    source: AcquisitionSource


class SalaryOverride(Base):
    """The true salary for a player whose ESPN value has been deliberately distorted.

    This is **not** a patch for ESPN reporting a wrong acquisition value. Managers trade draft
    cash, ESPN has no native support for that, so the commissioner hand-edits a few player
    salaries on the two teams involved to make the budget math work and changes them back
    before the next draft.

    ``actual_salary`` is therefore authoritative and ESPN's value is the distorted one, for as
    long as ``reverted`` is False. Once the commissioner has restored ESPN, ``reverted`` flips
    to True and the override becomes history — ESPN wins again.

    This matters more than a one-year mispricing: under the ratchet, a distortion that never
    gets reverted bakes itself into the player's base and carries forward every year after.

    ``unpaired_ok`` suppresses the league-wide balance check for a row whose counterparty is
    unrecoverable. Cash trades move money between two teams, so un-reverted overrides should
    net to zero; exactly one historical row (Saquon Barkley, +$3) has a missing fourth player
    nobody can identify.
    """

    espn_player_id: int
    season: int
    actual_salary: NonNegMoney
    reason: str
    created_at: datetime
    reverted: bool = False
    unpaired_ok: bool = False


class KeeperClaim(Base):
    """A manager's declaration that they are keeping this player at this slot.

    ``fee_allocated`` is a plain ``Money``, not ``NonNegMoney``: a negative fee is a *rule*
    violation, and the engine reports it as ``NEGATIVE_FEE`` alongside every other rule
    problem rather than raising a type error from deep inside a JSON load.
    """

    season: int
    manager_id: str
    espn_player_id: int
    slot: KeeperSlot
    fee_allocated: Money
    computed_salary: NonNegMoney | None = None
    submitted_at: datetime | None = None


class WeeklyScore(Base):
    """Points are real numbers — this is the one place decimals are correct."""

    season: int
    week: int
    manager_id: str
    points: float


class PlayoffTier(StrEnum):
    """ESPN's ``playoffTierType``. Weeks 1-14 are all ``NONE``."""

    NONE = "none"
    WINNERS_BRACKET = "winners_bracket"
    WINNERS_CONSOLATION_LADDER = "winners_consolation_ladder"
    LOSERS_CONSOLATION_LADDER = "losers_consolation_ladder"


class Matchup(Base):
    """One head-to-head game. ``away_*`` is ``None`` for a playoff bye.

    The winner is **derived from the points**, not stored. ESPN reports a ``winner`` field and
    it agrees, but deriving it means ``Unlucky`` — the highest score that still lost — rests on
    the same numbers everything else does, and a tie can never be silently scored as a loss.
    """

    season: int
    week: int
    tier: PlayoffTier = PlayoffTier.NONE
    home_manager_id: str
    home_points: float
    away_manager_id: str | None = None
    away_points: float | None = None

    @property
    def is_bye(self) -> bool:
        return self.away_manager_id is None

    @property
    def tied(self) -> bool:
        return not self.is_bye and self.home_points == self.away_points

    @property
    def loser(self) -> tuple[str, float] | None:
        """The losing manager and their score, or ``None`` for a bye or a tie.

        A tie has no loser, so a tied score is never an ``Unlucky`` candidate. That is the
        rule the prize name implies: you have to actually lose.
        """
        if self.is_bye or self.tied:
            return None
        if self.home_points < self.away_points:  # type: ignore[operator]
            return self.home_manager_id, self.home_points
        return self.away_manager_id, self.away_points  # type: ignore[return-value]


class PlayerWeek(Base):
    """One player's actual scoring in one week, on one manager's roster.

    ``started`` is the whole point of the record: the positional stud prize follows the
    manager who *started* him, so a 50-point week on somebody's bench wins nothing.
    """

    season: int
    week: int
    manager_id: str
    espn_player_id: int
    player_name: str
    position: Position
    lineup_slot_id: int
    started: bool
    points: float


class StandingRow(Base):
    """A franchise's regular-season record, computed from the matchups rather than read off.

    ``final_rank`` is ESPN's ``rankCalculatedFinal`` — the playoff bracket's answer, which is
    what pays the champion, 2nd and 3rd. It is deliberately not derivable from this row: the
    regular-season record does not decide the money.
    """

    season: int
    manager_id: str
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float
    final_rank: int | None = None
    playoff_seed: int | None = None


class WeeklyHigh(Base):
    """The top score in one week. ``manager_ids`` holds more than one only on a tie."""

    season: int
    week: int
    manager_ids: tuple[str, ...]
    points: float


class SeasonPoints(Base):
    season: int
    manager_id: str
    points: float


class StudAward(Base):
    """The best single *started* week by any player at one position, all season.

    Not a season total — the sheet records a player, a week and a score, and 2025's WR stud
    (W16) and TE stud (W15) both land in the playoff weeks, so the window is the full season
    rather than the 14-week regular season the weekly high scores use.
    """

    season: int
    position: Position
    espn_player_id: int
    player_name: str
    week: int
    points: float
    manager_ids: tuple[str, ...]


class SurvivorElimination(Base):
    season: int
    week: int
    manager_ids: tuple[str, ...]
    points: float


class UnluckyAward(Base):
    """The highest score that still lost its matchup — one per season, not one per week."""

    season: int
    week: int
    manager_ids: tuple[str, ...]
    points: float


class PrizeSchedule(Base):
    """What each prize paid in one season.

    Prize money is league-specific and appears nowhere in ESPN, so this is hand-recorded from
    the ``RS57`` sheet and lives in ``data/manual/``. Amounts are **per-season, not
    constants**: 2023 paid Survivor $50 where 2025 pays $40.

    ``NonNegMoney`` throughout, which means 2023's $9.29 weekly high score cannot be
    represented. That is deliberate — it was a one-off from the 18-week change and the league
    is back on whole dollars. 2023 is a backfill problem for a later phase, not a reason to put
    floats into money.
    """

    season: int
    champion: NonNegMoney
    second: NonNegMoney
    third: NonNegMoney
    most_points: NonNegMoney
    survivor: NonNegMoney
    stud: NonNegMoney
    """Per position. Paid four times — QB, RB, WR, TE."""
    unlucky: NonNegMoney
    weekly_high: NonNegMoney
    """Per week. Paid once for each of the 14 regular-season weeks."""

    def total(self, weeks: int, positions: int = 4) -> int:
        """What the season's pot comes to. The sheet's column footer is the check."""
        return (
            self.champion
            + self.second
            + self.third
            + self.most_points
            + self.survivor
            + self.stud * positions
            + self.unlucky
            + self.weekly_high * weeks
        )


class Payout(Base):
    season: int
    label: str
    amount: NonNegMoney
    winner_manager_id: str | None = None
    paid: bool = False


class Payment(Base):
    """That a prize has actually been handed over, recorded by the admin tool.

    ``Payout`` rows carry a ``paid`` flag, but they are **derived**: ``stats.award_prizes``
    computes them and the nightly Action rewrites ``data/derived/{year}-stats.json`` every run,
    so a flag set there would vanish on the next sync. Payment is a human fact about the real
    world, so it lives in ``data/manual/`` and is joined to the derived rows on
    ``(season, label, winner_manager_id)``.

    All three parts of that key are needed: a tie splits one label across several winners, so
    ``(season, label)`` alone would mark both halves paid when only one had been.

    **No amount field, deliberately.** What a prize pays is already in
    ``data/manual/payouts.json`` and the split is ``stats``'s to compute; a second copy here
    could disagree with the first. And **no payment method, handle, or note** — this row is
    committed to a public repo.
    """

    season: int
    label: str
    winner_manager_id: str
    paid: bool = False
    paid_at: datetime | None = None


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (list, tuple)):
        return [_jsonable(item) for item in obj]
    if isinstance(obj, dict):
        return {key: _jsonable(value) for key, value in obj.items()}
    return obj


def json_dumps(obj: Any) -> str:
    """Serialize deterministically: sorted keys, stable indent, trailing newline.

    Every nightly commit re-serializes these files. Without stable ordering each one looks
    like the whole file changed and the Git history becomes useless — which throws away the
    main reason for storing JSON in the first place.
    """
    return json.dumps(_jsonable(obj), sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def dump_json(obj: Any, path: Path) -> None:
    """Write ``obj`` to ``path`` deterministically. The only sanctioned way to write data/."""
    path.write_text(json_dumps(obj), encoding="utf-8")
