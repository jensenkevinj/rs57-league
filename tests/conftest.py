"""Small factories so the tests read as rules, not as model construction."""

from __future__ import annotations

from datetime import datetime

import pytest

from rs57.models import (
    AcquisitionSource,
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
) -> SalaryOverride:
    return SalaryOverride(
        espn_player_id=player_id,
        season=season,
        actual_salary=actual,
        reason=reason,
        created_at=created or SEASON_START,
        reverted=reverted,
        unpaired_ok=unpaired_ok,
    )


@pytest.fixture
def deadline() -> datetime:
    return TRADE_DEADLINE
