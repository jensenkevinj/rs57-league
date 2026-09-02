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
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

# All money is integer dollars. No floats, no cents. ``strict`` matters: without it Pydantic
# happily coerces 12.0 -> 12 and 12.7 -> error, which means a float leaking in from a
# spreadsheet import would silently succeed for whole dollars and explode for cents.
Money = Annotated[int, Field(strict=True)]
NonNegMoney = Annotated[int, Field(strict=True, ge=0)]

# ---------------------------------------------------------------------------
# Time — stored in UTC, displayed in Eastern, and this is the only border
# ---------------------------------------------------------------------------

LEAGUE_TZ = ZoneInfo("America/New_York")
"""The league's wall clock. ESPN states its dates in it and so does every manager."""


def to_league_time(when: datetime | None) -> datetime | None:
    """A stored naive-UTC datetime as the league's own wall clock, still naive. Display only.

    ``espn._epoch_ms`` converts every ESPN instant to naive UTC deliberately — the models, the
    fixtures and the derived files are naive throughout, and ``keeper_rules`` compares a
    prospect's ``acquired_at`` against a ``trade_deadline`` directly, so mixing an aware value
    in would raise from inside the engine. That decision stands. What was missing is the way
    back: nothing converted to Eastern for display, so the 2026 home page published the draft
    as 9/4 when ESPN says 9/3 at 9pm ET. Both league dates fall in the evening, UTC has already
    rolled past midnight by then, and every one of them printed a day late.

    Naive out as well as in, on purpose. An aware return value would leak into a comparison
    against a naive one somewhere downstream — the exact failure being fixed here, not a second
    copy of it. Use the result to **print**, never to store and never to compare against
    anything that came off ESPN.

    A real tz database rather than a fixed offset: the trade deadline is in December and the
    draft is in September, so a hardcoded ``-5`` is wrong for half the calendar.
    """
    if when is None:
        return None
    return when.replace(tzinfo=UTC).astimezone(LEAGUE_TZ).replace(tzinfo=None)


def utc_now() -> datetime:
    """Now, as a naive UTC datetime — the only clock comparable to a stored ESPN instant.

    ``datetime.now()`` returns the *machine's* local time, and the admin console compared that
    against a naive-UTC keeper deadline. It erred in the safe direction — the console unlocked
    late, never early — but by the UTC offset, and it quietly assumed whoever ran the tool sat
    in the league's own timezone.
    """
    return datetime.now(UTC).replace(tzinfo=None)



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
    """A league member. Deliberately has NO email field and no real name.

    The repo and the site are public. Nothing identifying goes in either — franchise names
    keyed on ``espn_team_id`` are the whole published identity. A ``data/private/`` directory
    was considered for the sheet's first-name-to-team mapping and **deliberately never
    created**; the ``.gitignore`` entry is belt and braces for a path that does not exist.
    """

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

    ``draft_doodle_url`` is shown on the public home page while the season has not drafted yet
    (see ``site.build_site``) and is recorded and displayed only — nothing in the engine reads
    it. It has no ESPN equivalent, so it is the one date-adjacent field still typed by hand.

    ``draft_date`` and ``keeper_deadline`` used to live here too and do not any more
    (commissioner, 2026-08-26): both are ESPN facts (``draftSettings.date`` and
    ``.keeperDeadlineDate``), and a hand-typed copy had already drifted from what ESPN actually
    held. They are read straight off the nightly sync now — see
    ``rs57.admin.derived.DerivedSeason`` — and nothing in this tool can override them.
    """

    year: int
    season_start: datetime | None = None
    trade_deadline: datetime | None = None
    draft_doodle_url: str | None = None
    consolation_winner_id: str | None = None


class Player(Base):
    espn_player_id: int
    name: str
    position: Position
    nfl_team: str


class OriginSource(StrEnum):
    """Which ESPN field said when a player's NFL career began.

    Recorded rather than inferred, so a reader never has to guess which one answered — the
    same discipline as ``source.base_salary_field`` on a derived season.

    **Two of these are exact and one is a bound, and the difference decides what may be
    concluded from it.** ``draft.year`` and ``debutYear`` state when a career began.
    ``FIRST_STATS_SEASON`` is the earliest season ESPN has statistics for, which is an upper
    bound: a player who was on a roster but recorded nothing shows up a year late. Measured
    across the league it agreed with the draft class 159 times out of 162, and all three
    disagreements were late by exactly one season — Jauan Jennings, Calvin Austin III and John
    Metchie III, each of whom missed his rookie year.

    So a bound can prove somebody is **not** a rookie, and can never prove that he is. See
    ``EXACT_SOURCES``.
    """

    DRAFT_YEAR = "draft_year"
    DEBUT_YEAR = "debut_year"
    FIRST_STATS_SEASON = "first_stats_season"


EXACT_SOURCES = frozenset({OriginSource.DRAFT_YEAR, OriginSource.DEBUT_YEAR})
"""Sources that state a first season rather than bounding it.

Only these may decide that a player **is** a rookie. A bound is allowed to rule him out, which
is the safe direction: being wrong late marks a veteran unknown, being wrong early would make a
second-year player prospect-eligible and cost somebody money.
"""


class PlayerOrigin(Base):
    """The season a player's NFL career began — the fact the prospect rule turns on.

    ``first_nfl_season`` is **required and not nullable, deliberately**. A player ESPN cannot
    answer for is absent from the list rather than present with a null: "we do not know" is
    then the absence of a row, not a value that reads like one. That is the three-state model
    (eligible / not eligible / unknown) expressed in the schema, and it is why no caller can
    accidentally treat an unknown player as a rookie.

    The value is immutable — a draft class does not change — which is what makes the file
    merge-only and a player fetched once ever. (A ``FIRST_STATS_SEASON`` bound is immutable in
    the same way: the earliest season a player has statistics for cannot move backwards.)
    """

    espn_player_id: int
    first_nfl_season: int
    source: OriginSource

    @property
    def exact(self) -> bool:
        """Whether this states the first season or merely bounds it. See ``EXACT_SOURCES``."""
        return self.source in EXACT_SOURCES


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


class CashTrade(Base):
    """A trade in which draft cash changed hands, and the record of which way it went.

    ESPN has no native support for trading auction budget, so a cash trade is *expressed* as a
    handful of hand-edited player salaries — the ``SalaryOverride`` rows that point back here
    through ``trade_id``. Those rows are the legs; this is the trade itself.

    Recording the trade separately is what makes the audit precise. Without it the only
    available check is ``check_override_balance``, which nets every live override in the league
    and therefore cannot tell one balanced trade from two unrelated mistakes that happen to
    cancel. With it, each trade is balanced against **its own** declared amount and the two
    teams that agreed it, so a missing leg names the trade that is missing it.

    Direction is explicit and is the thing most worth getting right. ``amount`` dollars of draft
    budget move **from** ``from_manager_id`` **to** ``to_manager_id``. On the receiving team
    ESPN under-charges — its player salary is edited *below* the true figure, freeing budget —
    so that leg's ``actual_salary - espn_base`` is **positive**. The paying team's leg is
    negative by the same amount. The two sum to zero, which is what
    ``keeper_rules.check_cash_trades`` audits.

    Confirmed against the record: the 2025 workbook's Jonathan Taylor (``-1``, Bijan's Mustard)
    and Jaxon Smith-Njigba (``+1``, Jaxian McJigberson) legs are one $1 cash trade and cancel
    exactly. Saquon Barkley's ``+3`` is the orphan whose counterparty nobody can identify.

    ``draft_year`` is **the auction the cash moves at, not the season the deal was struck in**,
    and this is the one model where those differ. A trade agreed on 25 Nov 2025 spends its money
    at the 2026 auction, so it is ``draft_year=2026`` with ``agreed_at`` in 2025. Every other
    model here calls this field ``season`` because for a roster or a claim the two are the same
    integer — season Y's prices are the prices set at Y's auction — but a trade has a date of
    its own, and calling this ``season`` invites exactly the wrong reading of it.

    ``note`` is free text a human types and the site renders — the same injection path as
    ``SalaryOverride.reason``, and it gets the same treatment: stored exactly as typed, escaped
    at render time, never passed through ``|safe``.
    """

    id: str
    draft_year: int
    from_manager_id: str
    to_manager_id: str
    amount: NonNegMoney
    agreed_at: datetime
    note: str = ""


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

    ``season`` here **is** the draft year, and the screens say so. ESPN's season-Y payload holds
    the prices set at Y's auction, so a distortion recorded against season Y is a distortion of
    the Y draft. It keeps the name ``season`` because that is what it joins to — a
    ``RosterEntry`` for that season — and renaming it would rename the join on both sides of the
    ratchet audit for no change in meaning. See ``CashTrade.draft_year``, which is the one field
    where the two genuinely come apart.

    ``trade_ids`` names every ``CashTrade`` this row is a leg of — **several, not one**.

    A single salary edit routinely expresses more than one trade. If a franchise owes another
    $1 from one deal and $2 from a second, the commissioner edits one player by $3 rather than
    four players by scattered amounts; and if A pays B $5 while B pays C $5, B is skipped
    entirely and only A and C are touched. Modelling this as a single ``trade_id`` could not
    say either of those, and forced a choice of which trade to file the edit under.

    **What that costs, stated plainly:** once edits are netted, per-trade balancing is not
    possible even in principle — nothing in the data says which dollar belonged to which deal,
    and splitting one would be recording a number nobody decided. What stays exactly checkable
    is the net per franchise across the trades that share legs. See
    ``keeper_rules.check_cash_trades``.

    Empty on purpose for rows predating the trades file, and for a leg whose counterparty is
    unrecoverable. An unlinked live leg is a REVIEW, not an error: it still nets into the
    league-wide ``check_override_balance``, the weaker check that these links upgrade.

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
    trade_ids: tuple[str, ...] = ()


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


PAYOUT_LEDGER_FROM = 2026
"""First season whose prize money is settled through this app.

Every season before it was paid out and reconciled outside the repo and is **settled**
(commissioner, 2026-08-31). The boundary is recorded because the console must not print a red
"not paid" against a 2021 franchise: that is a debt the league does not have, and a screen that
invents one is worse than a screen that says nothing.
"""


class Payment(Base):
    """That a franchise has been paid out for a season, recorded by the admin tool.

    The mirror of :class:`Dues`, and keyed the same way — ``(season, manager_id)``. Dues come
    IN at the start of a season and the prize money goes OUT at the end of it.

    **Per franchise, not per prize.** Prizes accrue all season and are settled in one payment
    at the end of it, so "has the Week 6 high score been paid?" is a question nobody asks and
    could not answer: the money moves once, in aggregate (commissioner, 2026-08-31). What a
    franchise is owed is the sum of what it won, which ``stats`` already derives.

    **No amount field, deliberately.** What is owed is that sum, computed from the derived
    payout rows; copying it here would be a second figure that could disagree with the first.
    And no payment method, handle, or note — this row is committed to a public repo.

    Absence means unsettled, so a franchise that has not been paid has no row at all.
    """

    season: int
    manager_id: str
    paid: bool = False
    paid_at: datetime | None = None


class Dues(Base):
    """That one franchise has paid its buy-in for one season, recorded by the admin tool.

    Not ``Payment`` — that is a prize handed OUT to a winner. This is money paid IN. The two
    are the same season's two ends: dues at the start of a season, prizes at the finish, which
    is why one admin screen shows both.

    Neither is a keeper fee. Fees and the $5 tax are auction budget and never change hands;
    these are real dollars.

    Keyed on ``(season, manager_id)`` — a franchise pays once a season. Absence means unpaid,
    so an unpaid franchise has no row at all rather than one saying ``paid: false``.

    **No amount field, deliberately.** The buy-in is one figure the whole league knows and
    nothing in ``data/`` records it today; putting it on twelve rows would be twelve copies of
    a number nobody has written down once. And **no payment method, handle, or note** — this
    row is committed to a public repo.
    """

    season: int
    manager_id: str
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


STALE_WAIVER_WARNING = "disagree with the FAAB"
"""Marker for a sync warning whose premise has since expired. **Transitional.**

Until 2026-09-02 the sync compared every waiver add's base against the FAAB actually bid, and
kept comparing it after the keeper deadline — when ESPN's field has stopped being an acquisition
price and holds the keeper price the commissioner entered, fee and tax inside it. Every waiver
add carrying a fee then "disagreed" with its own bid by exactly that fee.

`espn.py` no longer writes it, but `data/derived/` belongs to the nightly Action and cannot be
corrected from anywhere else, so seasons synced before the fix still carry the sentence. The two
readers drop it while the season is in that window. **Delete this and both call sites once every
season file has been re-synced** — it matches on prose, which is why it is scoped this narrowly
and dated here rather than left to be discovered.
"""
