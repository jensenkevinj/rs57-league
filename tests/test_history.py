"""The frozen-season store and the backfill importer.

The two guards here are the point of the module: ``data/history/`` has one writer, and a
season already in it is never written again. Both are mutation-checked below — removing the
guard has to make a specific test fail, or the test is decoration.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from rs57.backfill import (
    FEE_ALLOCATIONS,
    SLOT_ORDER,
    BackfillError,
    build_claims,
    check_completed,
)
from rs57.history import FrozenSeasonError, HistoryStore, OwnershipError
from rs57.models import AcquisitionSource, KeeperSlot, RosterEntry

NOW = datetime(2026, 7, 30, 12, 0, 0)


@pytest.fixture
def store(tmp_path: Path) -> HistoryStore:
    (tmp_path / "history").mkdir(parents=True)
    (tmp_path / "manual").mkdir(parents=True)
    (tmp_path / "derived").mkdir(parents=True)
    return HistoryStore(data_dir=tmp_path)


def claim_row(season: int, player_id: int, slot: str = "K1", salary: int = 10) -> dict:
    return {
        "season": season,
        "manager_id": "t1",
        "espn_player_id": player_id,
        "slot": slot,
        "fee_allocated": 0,
        "computed_salary": salary,
    }


# --------------------------------------------------------------- the freeze


def test_a_frozen_season_is_never_written_twice(store: HistoryStore):
    """The whole reason this directory has its own writer.

    An importer run twice is the ordinary way a second write gets attempted, and the second
    run is the one with less information. It raises rather than returning a flag, so a caller
    cannot carry on having ignored it.
    """
    store.write_season(2024, {"claims": []})
    with pytest.raises(FrozenSeasonError, match="already exists"):
        store.write_season(2024, {"claims": [claim_row(2024, 1)]})


def test_the_refusal_leaves_the_original_untouched(store: HistoryStore):
    store.write_season(2024, {"claims": [claim_row(2024, 1, salary=42)]})
    with pytest.raises(FrozenSeasonError):
        store.write_season(2024, {"claims": []})
    assert store.claims(2024)[0].computed_salary == 42


def test_writing_outside_history_is_refused(store: HistoryStore):
    """The mirror of the admin tool's guard. The failure mode is a plausible refactor, not a
    typo — this module reads data/derived/ on every import run."""
    for escape in ("../manual/claims.json", "../derived/2025.json", "../../secrets.json"):
        with pytest.raises(OwnershipError):
            store.path(escape)


def test_about_prose_is_written_and_survives_a_read(store: HistoryStore):
    store.write_season(2024, {"claims": []}, about=["why this season is here"])
    doc = json.loads((store.history / "2024.json").read_text(encoding="utf-8"))
    assert doc["_about"] == ["why this season is here"]
    assert doc["season"] == 2024


def test_a_written_season_is_deterministic_and_ends_in_a_newline(store: HistoryStore):
    path = store.write_season(
        2024, {"claims": [claim_row(2024, 2), claim_row(2024, 1)]}, about=["prose"]
    )
    body = path.read_text(encoding="utf-8")
    assert body.endswith("\n")
    assert body.index('"_about"') < body.index('"claims"'), "sorted keys"


# --------------------------------------------------------------- reading back


def test_a_claim_whose_season_contradicts_its_file_is_a_corrupted_file(store: HistoryStore):
    """The season key and the row's own field are redundant on purpose."""
    store.write_season(2024, {"claims": [claim_row(2023, 1)]})
    with pytest.raises(ValueError, match="says season 2023"):
        store.claims()


def test_prospect_ids_come_from_the_slot(store: HistoryStore):
    store.write_season(
        2024,
        {"claims": [claim_row(2024, 1, "PROSPECT"), claim_row(2024, 2, "K1")]},
    )
    assert store.prospect_ids() == {1}
    assert store.prospect_ids(before_season=2024) == set()


def test_started_history_unions_across_seasons(store: HistoryStore):
    """Retained as league history. No rule reads it since the never-started prospect rule was
    retired, but the seasons are frozen with it and it stays readable."""
    store.write_season(2023, {"started_player_ids": [1, 2]})
    store.write_season(2024, {"started_player_ids": [3]})
    assert store.started_player_ids() == {1, 2, 3}
    assert store.started_player_ids(through_season=2023) == {1, 2}


# --------------------------------------------------------------- the importer


def test_the_transcription_matches_espn_or_the_import_stops():
    """Names resolve against ESPN's own keeper picks for that team, and a miss raises.

    This is the one place a player name is load-bearing, which is exactly why a failure has to
    be loud: the workbook writes ``James Cook`` in 2024 and ``James Cook III`` in 2025 for the
    same person, and matching on name is what under-charged him $5 in the first place.
    """
    with pytest.raises(BackfillError, match="do not agree on who was kept"):
        build_claims(
            2025,
            keeper_names={1: {99: "Somebody Else"}},
            prior_base={99: 5},
            prior_keeper_ids=frozenset(),
            prior_prospect_ids=frozenset(),
        )


def test_a_kept_player_with_no_carried_in_price_stops_the_import():
    """No price means no computed_salary, and a claim with a guessed salary is worse than
    no claim — check_base_continuity would audit against the guess."""
    names = {
        allocation.espn_team_id: {
            index: name for index, (name, _) in enumerate(allocation.keepers, start=1)
        }
        for allocation in FEE_ALLOCATIONS[2025]
    }
    with pytest.raises(BackfillError, match="carried-in price is unknown"):
        build_claims(
            2025,
            keeper_names=names,
            prior_base={},
            prior_keeper_ids=frozenset(),
            prior_prospect_ids=frozenset(),
        )


def test_computed_salary_is_reconstructed_not_read_from_espn():
    """base + fee + tax, applied to LAST season's price.

    Taking it from ESPN's own bidAmount would make check_base_continuity tautological: next
    season's keeperValue equals that bid by construction, so the audit would report a clean
    ratchet having compared a number against itself.
    """
    names = {1: {10: "Alpha", 11: "Beta"}}
    allocation = type(FEE_ALLOCATIONS[2025][0])(
        espn_team_id=1, keepers=(("Alpha", 5), ("Beta", 0))
    )
    original = FEE_ALLOCATIONS.get(1999)
    FEE_ALLOCATIONS[1999] = (allocation,)
    try:
        claims, _ = build_claims(
            1999,
            keeper_names=names,
            prior_base={10: 20, 11: 7},
            prior_keeper_ids=frozenset({10}),
            prior_prospect_ids=frozenset(),
        )
    finally:
        if original is None:
            del FEE_ALLOCATIONS[1999]

    by_player = {claim.espn_player_id: claim for claim in claims}
    assert by_player[10].computed_salary == 20 + 5 + 5, "base + fee + tax"
    assert by_player[11].computed_salary == 7, "no fee, and no tax on a first keep"
    assert by_player[10].slot is SLOT_ORDER[0]


def test_a_prospect_keep_takes_no_fee_and_no_tax():
    names = {1: {10: "Alpha", 20: "Rookie"}}
    allocation = type(FEE_ALLOCATIONS[2025][0])(
        espn_team_id=1, keepers=(("Alpha", 0),), prospect="Rookie"
    )
    FEE_ALLOCATIONS[1998] = (allocation,)
    try:
        claims, _ = build_claims(
            1998,
            keeper_names=names,
            prior_base={10: 20, 20: 3},
            # In ESPN's keeper set for last season, which would tax him — a prospect keep
            # never sets the flag, so he must come back out at $3 and not $8.
            prior_keeper_ids=frozenset({10, 20}),
            prior_prospect_ids=frozenset({20}),
        )
    finally:
        del FEE_ALLOCATIONS[1998]

    prospect = next(c for c in claims if c.slot is KeeperSlot.PROSPECT)
    assert prospect.computed_salary == 3
    assert prospect.fee_allocated == 0


def test_a_season_with_no_fee_allocations_tab_records_no_claims():
    """2019-2023 predate the tabs. ESPN records who was kept and the price, but not the slot
    and not the fee split, so a claim for those seasons can only be invented."""
    claims, notes = build_claims(
        2021,
        keeper_names={},
        prior_base={},
        prior_keeper_ids=frozenset(),
        prior_prospect_ids=frozenset(),
    )
    assert claims == []
    assert notes == []


def test_an_unknowable_tax_leaves_the_salary_unpriced_rather_than_guessed():
    """When the previous season has no Fee Allocations tab, nobody knows which of its keeps
    was the PROSPECT — and a prospect keep sets no $5 tax. Assuming taxed over-prices every
    one of them by exactly $5, which then reads as a real discontinuity. Three did.

    ``check_base_continuity`` skips unpriced claims, so these are not audited rather than
    audited against a guess. The slot and fee, which the tab does record, are kept.
    """
    names = {1: {10: "WasKept", 11: "WasNotKept"}}
    allocation = type(FEE_ALLOCATIONS[2025][0])(
        espn_team_id=1, keepers=(("WasKept", 5), ("WasNotKept", 0))
    )
    FEE_ALLOCATIONS[1997] = (allocation,)
    try:
        claims, notes = build_claims(
            1997,
            keeper_names=names,
            prior_base={10: 20, 11: 7},
            prior_keeper_ids=frozenset({10}),
            prior_prospect_ids=frozenset(),
            prior_prospects_known=False,
        )
    finally:
        del FEE_ALLOCATIONS[1997]

    by_player = {claim.espn_player_id: claim for claim in claims}
    assert by_player[10].computed_salary is None, "he was kept; the tax could be either"
    assert by_player[10].fee_allocated == 5, "the fee is recorded and is not a guess"
    assert by_player[11].computed_salary == 7, "never kept, so untaxed for certain"
    assert any("no computed_salary" in note for note in notes), "and it is reported"


def test_a_known_prospect_list_prices_everything():
    names = {1: {10: "WasKept", 11: "WasNotKept"}}
    allocation = type(FEE_ALLOCATIONS[2025][0])(
        espn_team_id=1, keepers=(("WasKept", 5), ("WasNotKept", 0))
    )
    FEE_ALLOCATIONS[1996] = (allocation,)
    try:
        claims, notes = build_claims(
            1996,
            keeper_names=names,
            prior_base={10: 20, 11: 7},
            prior_keeper_ids=frozenset({10}),
            prior_prospect_ids=frozenset(),
            prior_prospects_known=True,
        )
    finally:
        del FEE_ALLOCATIONS[1996]

    assert all(claim.computed_salary is not None for claim in claims)
    assert not any("no computed_salary" in note for note in notes)


# --------------------------------------------------------------- only completed seasons


class _FakeClient:
    def __init__(self, year: int, drafted: bool, final_ranks: bool):
        self.year = year
        self._drafted = drafted
        self._final = final_ranks

    def fetch_draft_detail(self):
        return {"drafted": self._drafted}

    def fetch_league(self):
        rank = {"rankCalculatedFinal": 1} if self._final else {}
        return {"teams": [{"id": 1, **rank}]}


def test_an_undrafted_season_cannot_be_frozen():
    with pytest.raises(BackfillError, match="has not drafted"):
        check_completed(_FakeClient(2026, drafted=False, final_ranks=False))


def test_a_season_in_progress_cannot_be_frozen():
    """Drafted but unfinished. Rosters are still moving and the box scores are partial —
    freezing would record a September snapshot as the settled result of the year."""
    with pytest.raises(BackfillError, match="still\n?\\s*in progress"):
        check_completed(_FakeClient(2026, drafted=True, final_ranks=False))


def test_a_completed_season_passes():
    check_completed(_FakeClient(2025, drafted=True, final_ranks=True))


# --------------------------------------------------------------- mutation checks


def test_the_freeze_guard_is_what_stops_the_second_write(store: HistoryStore):
    """Mutation check: without the exists() refusal, the second write lands and the frozen
    record is gone. Demonstrated by writing the file directly, which is what the guard
    prevents."""
    store.write_season(2024, {"claims": [claim_row(2024, 1, salary=42)]})
    (store.history / "2024.json").write_text('{"season": 2024, "claims": []}\n')
    assert store.claims(2024) == [], "the unguarded path really does destroy the record"


def test_a_roster_round_trips_through_the_models(store: HistoryStore):
    entry = RosterEntry(
        season=2024,
        manager_id="t1",
        espn_player_id=1,
        acquired_at=NOW,
        base_salary=12,
        kept_prior_year=True,
        source=AcquisitionSource.DRAFT,
    )
    store.write_season(2024, {"roster_carried_in": [entry]})
    doc = json.loads((store.history / "2024.json").read_text(encoding="utf-8"))
    assert RosterEntry(**doc["roster_carried_in"][0]) == entry
