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


class Payout(Base):
    season: int
    label: str
    amount: NonNegMoney
    winner_manager_id: str | None = None
    paid: bool = False


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
