"""The sync's wiring: which season answers for the base, the tax and the FAAB record.

``build_season`` is handed a keeper set and does as it is told. **Choosing that set is
``sync_season``'s job, and it had no test at all** — which is how the $5 tax came to be charged
against the wrong season's keepers for a whole auction. The tax is paid on top of a specific
base, so the season that supplied the base has to be the season that supplies the keepers:
``espn.base_season_for``, the same asymmetry as ``base_salary_field``.

These tests watch which years the sync actually opens a client for. That is the only way to
catch the off-by-one: every individual piece was correct, and the season handed to them was not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rs57.espn import EspnError, keeper_pick_ids
from rs57.sync import prospect_ids_for_season, sync_season

DATA = Path(__file__).resolve().parent / "data"


def _doc(year: int) -> dict:
    return json.loads((DATA / f"espn_{year}.json").read_text())


class Replay:
    """A recorded season with the two extra methods ``sync_season`` reaches for."""

    def __init__(self, year: int):
        self.year = year
        self._doc = _doc(year)

    def fetch_league(self):
        return self._doc["league"]

    def fetch_roster(self, team_id: int):
        return self._doc["rosters"][str(team_id)]

    def fetch_draft_detail(self):
        return self._doc["draft"]

    def fetch_pro_teams(self):
        return {int(k): v for k, v in self._doc["pro_teams"].items()}

    def fetch_transactions(self):
        # The FAAB witness is a different test's subject. Refusing it here leaves a warning,
        # which is the documented behaviour, and keeps these tests about the keeper set.
        raise EspnError("no recorded transactions")


@pytest.fixture
def watched(monkeypatch):
    """Replace the client factory and the prospect reader, recording every year asked for.

    Returns ``(client_years, prospect_years)``. Both lists are the assertion: the sync must ask
    one season for the keeper picks and the *same* season for which of them were prospects.
    """
    client_years: list[int] = []
    prospect_years: list[int] = []

    def from_env(year: int):
        client_years.append(year)
        return Replay(year)

    monkeypatch.setattr("rs57.sync.EspnClient.from_env", staticmethod(from_env))
    monkeypatch.setattr(
        "rs57.sync.prospect_ids_for_season",
        lambda season: prospect_years.append(season) or set(),
    )
    return client_years, prospect_years


def test_a_drafted_season_is_taxed_from_its_own_keeper_picks(tmp_path, watched):
    """2025 has drafted, so its base is its own auction result and so is its keeper set.

    The regression. Reaching back to 2024 here taxes every player who was kept into 2025, was
    *not* kept into 2026, and was bought again at the auction — Saquon Barkley at $62 + $5,
    kept by nobody. ``2024 not in client_years`` is the whole assertion: under the bug the sync
    opens a client for it.
    """
    client_years, prospect_years = watched
    season = sync_season(2025, out_dir=tmp_path, write=False)

    assert 2024 not in client_years, "a drafted season must not reach back a year for its tax"
    assert prospect_years == [2025], "prospects must come from the base's season too"

    picks = keeper_pick_ids(_doc(2025)["draft"])
    taxed = {e.espn_player_id for e in season.roster if e.kept_prior_year}
    assert taxed
    assert taxed <= picks


def test_an_undrafted_season_is_taxed_from_last_seasons_keeper_picks(tmp_path, watched):
    """2026 has not drafted, so its base is what carried in — and the tax carries in with it.

    Not a concession to the bug: before an auction ESPN reports ``keeperValue``, which is last
    season's price, so last season's keeper set is the one that pairs with it. This half was
    always right and must stay right.
    """
    client_years, prospect_years = watched
    season = sync_season(2026, out_dir=tmp_path, write=False)

    assert 2025 in client_years, "an undrafted season's tax comes from the season before it"
    assert prospect_years == [2025]

    picks = keeper_pick_ids(_doc(2025)["draft"])
    taxed = {e.espn_player_id for e in season.roster if e.kept_prior_year}
    assert taxed
    assert taxed <= picks


def test_one_client_serves_the_base_season_rather_than_two(tmp_path, watched):
    """A drafted season opens exactly one client. The keeper picks and the FAAB record are
    both the current season's, so a second client against the same year would be two round
    trips for one answer."""
    client_years, _ = watched
    sync_season(2025, out_dir=tmp_path, write=False)
    assert client_years == [2025]


# --------------------------------------------------------------------------------------
# Which of the four keeper picks was the PROSPECT — the half ESPN cannot answer
# --------------------------------------------------------------------------------------


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


def test_a_frozen_season_answers_from_history(tmp_path):
    _write(
        tmp_path / "history" / "2025.json",
        {"claims": [
            {"espn_player_id": 1, "slot": "PROSPECT"},
            {"espn_player_id": 2, "slot": "K1"},
        ]},
    )
    got = prospect_ids_for_season(2025, tmp_path / "history", tmp_path / "manual")
    assert got == {1}


def test_the_season_being_played_answers_from_the_admin_tools_claims(tmp_path):
    """The gap this closes. A season is frozen only after it ends, so asking history alone
    answers ``None`` for the current season forever and every prospect on it rides on a
    warning instead of being subtracted from the taxed set."""
    _write(
        tmp_path / "manual" / "claims.json",
        {"seasons": {"2026": [
            {"espn_player_id": 7, "slot": "PROSPECT"},
            {"espn_player_id": 8, "slot": "K2"},
        ]}},
    )
    got = prospect_ids_for_season(2026, tmp_path / "history", tmp_path / "manual")
    assert got == {7}


def test_history_wins_over_the_admin_tool_once_a_season_is_frozen(tmp_path):
    """One fact, one record. ``validate`` reports the duplication until the season is cleared
    out of the admin tool, and until then the frozen file is the one that counts."""
    _write(tmp_path / "history" / "2025.json",
           {"claims": [{"espn_player_id": 1, "slot": "PROSPECT"}]})
    _write(tmp_path / "manual" / "claims.json",
           {"seasons": {"2025": [{"espn_player_id": 99, "slot": "PROSPECT"}]}})
    assert prospect_ids_for_season(2025, tmp_path / "history", tmp_path / "manual") == {1}


def test_an_unrecorded_season_is_unknown_and_not_empty(tmp_path):
    """``None`` means "nobody knows, warn about it"; ``set()`` means "checked, there were
    none". Collapsing them lets an unrecorded prospect be taxed $5 in silence — which is the
    reason ``build_season`` carries a warning for exactly this case."""
    assert prospect_ids_for_season(2019, tmp_path / "history", tmp_path / "manual") is None


def test_a_recorded_season_with_no_prospect_is_checked_and_none(tmp_path):
    _write(tmp_path / "history" / "2025.json",
           {"claims": [{"espn_player_id": 1, "slot": "K1"}]})
    got = prospect_ids_for_season(2025, tmp_path / "history", tmp_path / "manual")
    assert got == set(), "a claims list with no prospect in it is an answer, not a gap"


def test_an_empty_claims_list_is_no_record_rather_than_no_prospects(tmp_path):
    """2019-2023 are frozen with ``"claims": []`` — they predate the workbook's Fee Allocations
    tabs, so nobody transcribed who was kept, let alone in which slot.

    Reading that as checked-and-none subtracts no prospects and issues no warning, which is the
    silent $5 this whole distinction exists to prevent. The frozen file existing is not the same
    as the frozen file answering.
    """
    _write(tmp_path / "history" / "2021.json", {"claims": []})
    assert prospect_ids_for_season(2021, tmp_path / "history", tmp_path / "manual") is None


def test_an_empty_frozen_season_still_defers_to_the_admin_tool(tmp_path):
    """No record in history is a reason to keep looking, not a reason to stop."""
    _write(tmp_path / "history" / "2026.json", {"claims": []})
    _write(tmp_path / "manual" / "claims.json",
           {"seasons": {"2026": [{"espn_player_id": 7, "slot": "PROSPECT"}]}})
    assert prospect_ids_for_season(2026, tmp_path / "history", tmp_path / "manual") == {7}
