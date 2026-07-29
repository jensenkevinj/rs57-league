"""The scoring-side ESPN mapping: what it reads, and what it refuses to read.

Synthetic payloads here, on purpose — these pin the mapper's *decisions* (which stat row is
the real one, which lineup slots are started, what counts as a played game). The real recorded
season lives in ``test_prize_fixtures.py``.
"""

from __future__ import annotations

import pytest

from rs57.espn import ACTUAL_STAT_SOURCE, BENCH_SLOT_IDS, EspnError, build_scoring_season
from rs57.models import PlayoffTier, Position


def team(team_id: int, **extra):
    row = {
        "id": team_id,
        "name": f"Team {team_id}",
        "points": 0.0,
        "playoffSeed": team_id,
        "rankCalculatedFinal": team_id,
    }
    row.update(extra)
    return row


def player_entry(player_id: int, slot: int, points: float, position_id: int = 3, week: int = 1):
    return {
        "lineupSlotId": slot,
        "playerPoolEntry": {
            "player": {
                "id": player_id,
                "fullName": f"Player {player_id}",
                "defaultPositionId": position_id,
                "stats": [
                    {"scoringPeriodId": week, "statSourceId": 1, "appliedTotal": 999.0},
                    {"scoringPeriodId": week, "statSourceId": ACTUAL_STAT_SOURCE,
                     "appliedTotal": points},
                ],
            }
        },
    }


class FakeClient:
    """Minimal stand-in. ``teams`` defaults to a full 12, since a short league is fatal."""

    year = 2025

    def __init__(self, matchups, boxscores=None, teams=None, regular_weeks=14):
        self._matchups = matchups
        self._boxscores = boxscores or {}
        self._teams = teams if teams is not None else [team(i) for i in range(1, 13)]
        self._regular_weeks = regular_weeks

    def fetch_league(self):
        return {
            "settings": {"scheduleSettings": {"matchupPeriodCount": self._regular_weeks}},
            "teams": self._teams,
        }

    def fetch_matchups(self):
        return self._matchups

    def fetch_boxscore(self, week: int):
        return self._boxscores.get(week, [])


def matchup(week, home, home_points, away=None, away_points=None, winner="HOME", tier="NONE"):
    row = {
        "matchupPeriodId": week,
        "playoffTierType": tier,
        "winner": winner,
        "home": {"teamId": home, "totalPoints": home_points},
    }
    if away is not None:
        row["away"] = {"teamId": away, "totalPoints": away_points}
    return row


class TestWhatCountsAsPlayed:
    def test_an_undecided_game_is_not_scored(self):
        """ESPN publishes the whole season from preseason with every game UNDECIDED at 0.0.
        Carrying those through would award the weekly high score to nobody, at nothing."""
        synced = build_scoring_season(
            FakeClient([matchup(1, 1, 0.0, 2, 0.0, winner="UNDECIDED")])
        )
        assert synced.matchups == ()
        assert synced.scores == ()
        assert any("UNDECIDED" in warning for warning in synced.warnings)

    def test_a_scored_playoff_bye_is_played_even_though_it_stays_undecided(self):
        """A bye has no opponent to beat, so ESPN never marks it decided — but the top seeds
        really did score that week."""
        synced = build_scoring_season(
            FakeClient(
                [matchup(15, 9, 144.82, winner="UNDECIDED", tier="WINNERS_BRACKET")]
            )
        )
        assert len(synced.matchups) == 1
        assert synced.matchups[0].is_bye
        assert synced.matchups[0].loser is None, "a bye cannot make anyone Unlucky"

    def test_an_unscored_bye_is_still_just_a_fixture(self):
        synced = build_scoring_season(
            FakeClient([matchup(15, 9, 0.0, winner="UNDECIDED", tier="WINNERS_BRACKET")])
        )
        assert synced.matchups == ()

    def test_a_short_regular_season_is_reported(self):
        synced = build_scoring_season(FakeClient([matchup(1, 1, 100.0, 2, 90.0)]))
        assert any("of 14 regular-season weeks" in w for w in synced.warnings)


class TestPlayerWeeks:
    def _client(self, entries):
        return FakeClient(
            [matchup(1, 1, 100.0, 2, 90.0)],
            boxscores={
                1: [
                    {
                        "matchupPeriodId": 1,
                        "home": {"teamId": 1, "rosterForCurrentScoringPeriod": {"entries": entries}},
                    }
                ]
            },
        )

    def test_the_actual_score_is_read_never_the_projection(self):
        synced = build_scoring_season(self._client([player_entry(11, slot=4, points=22.5)]))
        assert [week.points for week in synced.player_weeks] == [22.5]

    @pytest.mark.parametrize("slot", sorted(BENCH_SLOT_IDS))
    def test_bench_and_ir_are_not_started(self, slot):
        synced = build_scoring_season(self._client([player_entry(11, slot=slot, points=50.0)]))
        assert synced.player_weeks[0].started is False

    @pytest.mark.parametrize("slot", [0, 2, 4, 6, 16, 23])
    def test_every_other_slot_is_started_including_flex(self, slot):
        synced = build_scoring_season(self._client([player_entry(11, slot=slot, points=5.0)]))
        assert synced.player_weeks[0].started is True

    def test_a_player_with_no_actual_stat_row_scored_nothing(self):
        entry = player_entry(11, slot=4, points=0.0)
        entry["playerPoolEntry"]["player"]["stats"] = []
        synced = build_scoring_season(self._client([entry]))
        assert synced.player_weeks[0].points == 0.0

    def test_an_unmapped_position_is_skipped_with_a_warning_not_a_crash(self):
        """Only four positions pay a stud prize, and a skipped starter still shows up in the
        lineup-total cross-check, so this need not be fatal."""
        synced = build_scoring_season(
            self._client([player_entry(11, slot=4, points=9.0, position_id=5)])
        )
        assert synced.player_weeks == ()
        assert any("unmapped defaultPositionId [5]" in w for w in synced.warnings)

    def test_positions_map_onto_the_model(self):
        synced = build_scoring_season(
            self._client([player_entry(11, slot=0, points=30.0, position_id=1)])
        )
        assert synced.player_weeks[0].position is Position.QB


class TestDegradedResponses:
    def test_a_short_league_refuses_to_derive_anything(self):
        with pytest.raises(EspnError, match="refusing to derive stats"):
            build_scoring_season(FakeClient([], teams=[team(1), team(2)]))

    def test_a_missing_matchup_period_count_is_fatal(self):
        """That number decides which weeks three of the prizes cover; guessing 14 would
        silently mis-award them the season the league changes it."""
        with pytest.raises(EspnError, match="matchupPeriodCount"):
            build_scoring_season(FakeClient([], regular_weeks=0))

    def test_an_unknown_playoff_tier_is_schema_drift(self):
        with pytest.raises(EspnError, match="playoffTierType"):
            build_scoring_season(FakeClient([matchup(1, 1, 100.0, 2, 90.0, tier="MYSTERY")]))


class TestFranchiseKeying:
    def test_managers_are_keyed_on_espn_team_id_never_on_a_name(self):
        synced = build_scoring_season(FakeClient([matchup(1, 1, 100.0, 2, 90.0)]))
        assert synced.matchups[0].home_manager_id == "t1"
        assert synced.matchups[0].away_manager_id == "t2"
        assert set(synced.final_ranks) == {f"t{i}" for i in range(1, 13)}

    def test_tiers_map_onto_the_model(self):
        synced = build_scoring_season(
            FakeClient(
                [matchup(17, 1, 100.0, 2, 90.0, tier="LOSERS_CONSOLATION_LADDER")]
            )
        )
        assert synced.matchups[0].tier is PlayoffTier.LOSERS_CONSOLATION_LADDER
