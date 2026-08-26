"""Small factories so the tests read as rules, not as model construction."""

from __future__ import annotations

from datetime import datetime

import pytest

from rs57.models import (
    AcquisitionSource,
    CashTrade,
    KeeperClaim,
    KeeperSlot,
    RosterEntry,
    SalaryOverride,
)

SEASON = 2026
SEASON_START = datetime(2025, 8, 5, 12, 0)
TRADE_DEADLINE = datetime(2025, 11, 26, 12, 0)


def entry(
    player_id: int,
    base: int,
    *,
    kept: bool = False,
    manager: str = "m1",
    season: int = SEASON,
    acquired: datetime | None = None,
    source: AcquisitionSource = AcquisitionSource.DRAFT,
) -> RosterEntry:
    return RosterEntry(
        season=season,
        manager_id=manager,
        espn_player_id=player_id,
        acquired_at=acquired or SEASON_START,
        base_salary=base,
        kept_prior_year=kept,
        source=source,
    )


def claim(
    player_id: int,
    slot: KeeperSlot,
    *,
    fee: int = 0,
    manager: str = "m1",
    season: int = SEASON,
    computed: int | None = None,
) -> KeeperClaim:
    return KeeperClaim(
        season=season,
        manager_id=manager,
        espn_player_id=player_id,
        slot=slot,
        fee_allocated=fee,
        computed_salary=computed,
    )


def override(
    player_id: int,
    actual: int,
    *,
    season: int = SEASON,
    reverted: bool = False,
    unpaired_ok: bool = False,
    created: datetime | None = None,
    reason: str = "draft cash trade",
    trade_id: str | None = None,
    trade_ids: tuple[str, ...] = (),
) -> SalaryOverride:
    """``trade_id`` is sugar for the one-trade case, which is most of them.

    An override can be a leg of several trades — that is what netting produces — so
    ``trade_ids`` is the real field and takes a tuple.
    """
    return SalaryOverride(
        espn_player_id=player_id,
        season=season,
        actual_salary=actual,
        reason=reason,
        created_at=created or SEASON_START,
        reverted=reverted,
        unpaired_ok=unpaired_ok,
        trade_ids=trade_ids or ((trade_id,) if trade_id else ()),
    )


def trade(
    trade_id: str = "T1",
    *,
    amount: int = 5,
    payer: str = "m1",
    payee: str = "m2",
    draft_year: int = SEASON,
    note: str = "",
) -> CashTrade:
    """``amount`` dollars of draft budget move FROM ``payer`` TO ``payee``.

    The receiving side is the one ESPN under-charges, so its leg's delta is positive. Named
    payer/payee here rather than from/to because ``from`` is a keyword and a test that reads
    ``from_=`` obscures the one thing these cases are about.
    """
    return CashTrade(
        id=trade_id,
        draft_year=draft_year,
        from_manager_id=payer,
        to_manager_id=payee,
        amount=amount,
        agreed_at=SEASON_START,
        note=note,
    )


@pytest.fixture
def deadline() -> datetime:
    return TRADE_DEADLINE
