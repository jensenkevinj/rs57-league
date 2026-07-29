"""Phase 2's acceptance criterion: recomputing 2025 reproduces the ``RS57`` sheet exactly.

The whole point of this file. Every unit test in ``test_stats.py`` can pass while the rules
themselves are wrong — the survivor elimination rule, the stud window and the Most Points
window were all *unrecorded*, and each was settled by reproducing the sheet rather than by
reading a spec. This is where that reproduction lives.

Ground truth is ``fixtures/prize_cases.json``, hand-transcribed from the sheet. Prize money
appears nowhere in ESPN, so there is no second source: a fixture generated from the engine
would agree with the engine however wrong it was.

Phase 1 waived its equivalent check as redundant and that hid a real $5 bug. See §13
Corrections in the plan doc.

The ESPN side replays ``tests/data/espn_scoring_2025.json``, a trimmed recording of the live
season, so CI never touches the network. It is stored compactly rather than pretty-printed —
it is a machine recording of 3,206 player-weeks, not a file anyone reads by eye.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rs57.espn import build_scoring_season
from rs57.models import Position
from rs57.stats import award_prizes, compute_season_stats
from rs57.stats_sync import prize_schedule

DATA = Path(__file__).parent / "data"
FIXTURES = Path(__file__).parent.parent / "fixtures"


class ReplayScoringClient:
    """Stands in for ``EspnClient`` against the recorded season. Same surface, no network."""

    def __init__(self, year: int = 2025):
        self.year = year
        self._doc = json.loads((DATA / f"espn_scoring_{year}.json").read_text())

    def fetch_league(self):
        return self._doc["league"]

    def fetch_matchups(self):
        return self._doc["matchups"]

    def fetch_boxscore(self, week: int):
        return self._doc["boxscores"][str(week)]


@pytest.fixture(scope="module")
def sheet() -> dict:
    return json.loads((FIXTURES / "prize_cases.json").read_text())


@pytest.fixture(scope="module")
def scoring():
    return build_scoring_season(ReplayScoringClient(2025))


@pytest.fixture(scope="module")
def stats(scoring):
    return compute_season_stats(
        season=2025,
        scores=scoring.scores,
        matchups=scoring.matchups,
        player_weeks=scoring.player_weeks,
        regular_season_weeks=scoring.regular_season_weeks,
        final_ranks=scoring.final_ranks,
        playoff_seeds=scoring.playoff_seeds,
        espn_points=scoring.espn_points,
    )


@pytest.fixture(scope="module")
def payouts(stats, scoring):
    schedule = prize_schedule(2025)
    assert schedule is not None, "2025 prize amounts must be recorded in data/manual/payouts.json"
    awarded, _ = award_prizes(stats, schedule, scoring.final_ranks)
    return awarded


def test_the_recording_covers_the_whole_season(scoring):
    assert scoring.regular_season_weeks == 14
    assert {score.week for score in scoring.scores} == set(range(1, 18))
    assert len(scoring.scores) == 12 * 14 + 12 * 3  # 12 teams, 17 weeks, byes included


def test_every_weekly_high_score_matches_the_sheet(stats, sheet):
    expected = sheet["weekly_high_scores"]
    assert len(stats.weekly_highs) == 14, "weeks 1-14 only; the playoff weeks pay nothing"
    for high in stats.weekly_highs:
        row = expected[str(high.week)]
        assert high.manager_ids == (row["manager_id"],), f"week {high.week}"
        assert high.points == pytest.approx(row["points"]), f"week {high.week}"


def test_most_points_matches_the_sheet(stats, sheet):
    assert stats.season_points[0].manager_id == sheet["most_points"]["manager_id"]


def test_every_positional_stud_matches_the_sheet(stats, sheet):
    computed = {stud.position: stud for stud in stats.studs}
    assert len(computed) == 4
    for row in sheet["studs"]:
        stud = computed[Position(row["position"])]
        assert stud.manager_ids == (row["manager_id"],), row["position"]
        assert stud.player_name == row["player"], row["position"]
        assert stud.week == row["week"], row["position"]
        assert stud.points == pytest.approx(row["points"]), row["position"]


def test_the_stud_window_runs_past_the_regular_season(stats):
    """Not a restatement of the fixture: the WR and TE studs land in weeks 16 and 15, which
    is what proves the window is the whole season rather than the 14 the high scores use."""
    by_position = {stud.position: stud.week for stud in stats.studs}
    assert by_position[Position.WR] == 16
    assert by_position[Position.TE] == 15


def test_survivor_eliminations_match_the_sheet_week_for_week(stats, sheet):
    expected = sheet["survivor"]["eliminations"]
    assert len(stats.survivor_eliminations) == 11
    for elimination in stats.survivor_eliminations:
        assert elimination.manager_ids == (expected[str(elimination.week)],), (
            f"week {elimination.week}"
        )
    assert stats.survivor_winner_ids == (sheet["survivor"]["manager_id"],)


def test_unlucky_matches_the_sheet(stats, sheet):
    expected = sheet["unlucky"]
    assert stats.unlucky is not None
    assert stats.unlucky.manager_ids == (expected["manager_id"],)
    assert stats.unlucky.week == expected["week"]
    assert stats.unlucky.points == pytest.approx(expected["points"])


def test_the_three_placings_match_the_sheet(scoring, sheet):
    for rank, manager in sheet["placings"].items():
        assert scoring.final_ranks[manager] == int(rank)


def test_the_standings_reconcile_with_espns_own_totals(stats, scoring):
    """Both routes to points-for agree, so no week was dropped on the floor."""
    assert len(stats.standings) == 12
    for row in stats.standings:
        assert row.points_for == pytest.approx(scoring.espn_points[row.manager_id], abs=0.02)
    assert sum(row.wins + row.losses + row.ties for row in stats.standings) == 12 * 14


def test_the_franchise_names_are_the_ones_the_sheet_was_read_against(scoring, sheet):
    """Names are for eyeballing only — nothing keys on them, and one carries a double space."""
    recorded = {
        f"t{team['id']}": team["name"]
        for team in ReplayScoringClient(2025).fetch_league()["teams"]
    }
    assert recorded == sheet["franchise_names"]


def test_the_payouts_reproduce_the_sheets_totals_column(payouts, sheet):
    """The strongest single check here: the sheet's own per-manager totals, independently
    transcribed, reconstructed from 24 payout rows."""
    expected = {
        manager: amount
        for manager, amount in sheet["totals_by_manager"].items()
        if not manager.startswith("_")
    }
    totals: dict[str, int] = {manager: 0 for manager in sheet["franchise_names"]}
    for payout in payouts:
        if payout.winner_manager_id:
            totals[payout.winner_manager_id] += payout.amount
    assert totals == expected


def test_the_pot_adds_up_to_the_sheets_footer(payouts, sheet):
    assert sum(payout.amount for payout in payouts) == sheet["pot"]
    assert all(payout.winner_manager_id is not None for payout in payouts)


def test_no_error_survives_the_real_season(stats):
    """REVIEW items are expected — the consolation bracket is one. ERRORs are not."""
    assert not stats.blocked, [issue.message for issue in stats.issues]


def test_nothing_in_the_recording_carries_a_real_name(scoring):
    """The repo and the site are PUBLIC. ESPN's payload has ``members[].firstName`` in it and
    the recording must not, however convenient it would be for the acceptance check."""
    raw = (DATA / "espn_scoring_2025.json").read_text()
    assert "firstName" not in raw and "members" not in raw
    assert all(manager.startswith("t") for manager in scoring.espn_points)
