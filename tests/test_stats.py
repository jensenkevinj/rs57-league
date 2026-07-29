"""The derived stats engine, as rules rather than as model construction.

These are the unit tests. The end-to-end proof that the definitions are *right* is in
``test_prize_fixtures.py``, which replays the real 2025 season against the commissioner's
sheet — a rule can be implemented perfectly and still be the wrong rule.
"""

from __future__ import annotations

import pytest

from rs57.keeper_rules import Severity
from rs57.models import Matchup, PlayerWeek, PlayoffTier, Position, PrizeSchedule, WeeklyScore
from rs57.stats import (
    STUD_POSITIONS,
    StatIssueCode,
    award_prizes,
    check_champion_won_the_final,
    check_lineup_totals,
    check_standings_against_espn,
    compute_season_stats,
    consolation_winner,
    positional_studs,
    season_points,
    split_prize,
    standings,
    survivor,
    unlucky,
    weekly_high_scores,
)

SEASON = 2025


def score(week: int, manager: str, points: float) -> WeeklyScore:
    return WeeklyScore(season=SEASON, week=week, manager_id=manager, points=points)


def game(
    week: int,
    home: str,
    home_points: float,
    away: str | None = None,
    away_points: float | None = None,
    tier: PlayoffTier = PlayoffTier.NONE,
) -> Matchup:
    return Matchup(
        season=SEASON,
        week=week,
        tier=tier,
        home_manager_id=home,
        home_points=home_points,
        away_manager_id=away,
        away_points=away_points,
    )


def played(
    week: int,
    manager: str,
    points: float,
    *,
    position: Position = Position.WR,
    started: bool = True,
    player_id: int = 1,
    name: str = "A Player",
) -> PlayerWeek:
    return PlayerWeek(
        season=SEASON,
        week=week,
        manager_id=manager,
        espn_player_id=player_id,
        player_name=name,
        position=position,
        lineup_slot_id=4 if started else 20,
        started=started,
        points=points,
    )


def schedule(**overrides: int) -> PrizeSchedule:
    amounts = dict(
        champion=500,
        second=200,
        third=100,
        most_points=100,
        survivor=40,
        stud=25,
        unlucky=20,
        weekly_high=10,
    )
    amounts.update(overrides)
    return PrizeSchedule(season=SEASON, **amounts)


class TestWeeklyHighScores:
    def test_picks_the_top_score_each_week(self):
        scores = [score(1, "t1", 100.0), score(1, "t2", 120.5), score(2, "t1", 90.0)]
        highs, issues = weekly_high_scores(scores, [1, 2])
        assert [(h.week, h.manager_ids, h.points) for h in highs] == [
            (1, ("t2",), 120.5),
            (2, ("t1",), 90.0),
        ]
        assert issues == []

    def test_a_tie_reports_every_manager(self):
        highs, _ = weekly_high_scores([score(1, "t2", 99.0), score(1, "t1", 99.0)], [1])
        assert highs[0].manager_ids == ("t1", "t2")

    def test_a_week_with_no_scores_is_an_error_not_a_skip(self):
        highs, issues = weekly_high_scores([score(1, "t1", 100.0)], [1, 2])
        assert len(highs) == 1
        assert [i.code for i in issues] == [StatIssueCode.MISSING_WEEK]
        assert issues[0].severity is Severity.ERROR

    def test_weeks_outside_the_window_win_nothing(self):
        """The playoff weeks score points but pay no weekly high score prize."""
        scores = [score(14, "t1", 100.0), score(15, "t2", 400.0)]
        highs, _ = weekly_high_scores(scores, list(range(1, 15)))
        assert [h.week for h in highs] == [14]


class TestSeasonPoints:
    def test_sums_only_the_window_and_ranks_descending(self):
        scores = [
            score(1, "t1", 100.0), score(2, "t1", 100.0), score(15, "t1", 500.0),
            score(1, "t2", 150.0), score(2, "t2", 100.0),
        ]
        rows = season_points(scores, [1, 2])
        assert [(r.manager_id, r.points) for r in rows] == [("t2", 250.0), ("t1", 200.0)]

    def test_the_window_can_change_the_winner(self):
        """Exactly the 2023 case: 1-14 and 1-17 name different managers, and the sheet
        says the regular season is what counts."""
        scores = [
            score(1, "t1", 100.0), score(15, "t1", 5.0),
            score(1, "t2", 90.0), score(15, "t2", 300.0),
        ]
        assert season_points(scores, [1])[0].manager_id == "t1"
        assert season_points(scores, [1, 15])[0].manager_id == "t2"


class TestPositionalStuds:
    def test_best_single_started_week_wins_not_the_season_total(self):
        weeks = [
            played(1, "t1", 30.0, player_id=1, name="Steady"),
            played(2, "t1", 30.0, player_id=1, name="Steady"),
            played(3, "t2", 45.0, player_id=2, name="Spiky"),
        ]
        studs, _ = positional_studs(weeks)
        wr = next(s for s in studs if s.position is Position.WR)
        assert (wr.player_name, wr.week, wr.points, wr.manager_ids) == ("Spiky", 3, 45.0, ("t2",))

    def test_a_benched_monster_week_wins_nothing(self):
        weeks = [
            played(1, "t1", 20.0, player_id=1),
            played(1, "t2", 99.0, player_id=2, started=False),
        ]
        studs, _ = positional_studs(weeks)
        wr = next(s for s in studs if s.position is Position.WR)
        assert wr.manager_ids == ("t1",) and wr.points == 20.0

    def test_defense_scores_but_wins_no_stud_prize(self):
        weeks = [played(1, "t1", 60.0, position=Position.DEF), played(1, "t2", 10.0)]
        studs, issues = positional_studs(weeks)
        assert Position.DEF not in {s.position for s in studs}
        assert {i.position for i in issues} == {Position.QB, Position.RB, Position.TE}

    def test_a_position_nobody_started_is_an_error(self):
        studs, issues = positional_studs([played(1, "t1", 10.0)])
        assert [s.position for s in studs] == [Position.WR]
        assert all(i.severity is Severity.ERROR for i in issues)

    def test_two_players_tied_at_the_top_both_win(self):
        weeks = [
            played(1, "t1", 40.0, player_id=1, name="One"),
            played(2, "t2", 40.0, player_id=2, name="Two"),
        ]
        studs, _ = positional_studs(weeks)
        wr = next(s for s in studs if s.position is Position.WR)
        assert wr.manager_ids == ("t1", "t2")


class TestSurvivor:
    def test_lowest_of_the_still_alive_goes_out_each_week(self):
        """A manager already eliminated cannot be eliminated again, however badly they
        score — which is what makes this different from 'lowest score in the league'."""
        scores = [
            score(1, "t1", 10.0), score(1, "t2", 20.0), score(1, "t3", 30.0),
            score(2, "t1", 0.0), score(2, "t2", 25.0), score(2, "t3", 15.0),
        ]
        result, issues = survivor(scores)
        assert [(e.week, e.manager_ids) for e in result.eliminations] == [
            (1, ("t1",)),
            (2, ("t3",)),
        ]
        assert result.winner_ids == ("t2",)
        assert issues == []

    def test_the_window_follows_the_league_size(self):
        scores = [score(week, f"t{i}", float(i * 10 + week)) for week in range(1, 12) for i in range(1, 13)]
        result, issues = survivor(scores)
        assert len(result.eliminations) == 11
        assert len(result.winner_ids) == 1
        assert issues == []

    def test_a_tie_for_lowest_eliminates_both_and_draws_a_review(self):
        scores = [
            score(1, "t1", 10.0), score(1, "t2", 10.0), score(1, "t3", 30.0),
            score(2, "t3", 5.0),
        ]
        result, issues = survivor(scores)
        assert result.eliminations[0].manager_ids == ("t1", "t2")
        assert result.winner_ids == ("t3",)
        assert StatIssueCode.SURVIVOR_TIE in {i.code for i in issues}
        assert all(i.severity is Severity.REVIEW for i in issues)


class TestUnlucky:
    def test_the_highest_losing_score_wins_it(self):
        games = [
            game(1, "t1", 150.0, "t2", 160.0),  # t1 loses with 150
            game(2, "t3", 200.0, "t4", 10.0),   # t3 WINS with 200 — not a candidate
        ]
        award, issues = unlucky(games, [1, 2])
        assert (award.manager_ids, award.points, award.week) == (("t1",), 150.0, 1)
        assert issues == []

    def test_it_is_awarded_once_a_season_not_once_a_week(self):
        games = [game(w, "t1", 100.0 + w, "t2", 200.0) for w in range(1, 15)]
        award, _ = unlucky(games, list(range(1, 15)))
        assert award.week == 14 and award.points == 114.0

    def test_a_tie_is_not_a_loss(self):
        games = [game(1, "t1", 180.0, "t2", 180.0), game(1, "t3", 50.0, "t4", 60.0)]
        award, _ = unlucky(games, [1])
        assert award.manager_ids == ("t3",)

    def test_playoff_losses_are_outside_the_window(self):
        games = [game(1, "t1", 100.0, "t2", 110.0), game(15, "t3", 190.0, "t4", 200.0)]
        award, _ = unlucky(games, list(range(1, 15)))
        assert award.manager_ids == ("t1",)

    def test_no_completed_loss_is_an_error(self):
        award, issues = unlucky([], [1])
        assert award is None
        assert issues[0].severity is Severity.ERROR


class TestStandings:
    def test_records_and_points_come_from_the_matchups(self):
        games = [
            game(1, "t1", 100.0, "t2", 90.0),
            game(2, "t1", 80.0, "t2", 95.0),
            game(3, "t1", 70.0, "t2", 70.0),
        ]
        rows = {row.manager_id: row for row in standings(games, [1, 2, 3])}
        assert (rows["t1"].wins, rows["t1"].losses, rows["t1"].ties) == (1, 1, 1)
        assert rows["t1"].points_for == 250.0
        assert rows["t1"].points_against == 255.0

    def test_a_bye_counts_for_nothing(self):
        rows = standings([game(15, "t1", 140.0)], [15])
        assert rows == []

    def test_espn_cross_check_catches_a_dropped_week(self):
        rows = standings([game(1, "t1", 100.0, "t2", 90.0)], [1])
        clean = check_standings_against_espn(rows, {"t1": 100.0, "t2": 90.0})
        assert clean == []
        dirty = check_standings_against_espn(rows, {"t1": 240.0, "t2": 90.0})
        assert [i.code for i in dirty] == [StatIssueCode.STANDINGS_POINTS_MISMATCH]
        assert dirty[0].severity is Severity.REVIEW


class TestCrossChecks:
    def test_started_points_must_sum_to_the_team_score(self):
        weeks = [played(1, "t1", 60.0, player_id=1), played(1, "t1", 40.0, player_id=2)]
        games = [game(1, "t1", 100.0, "t2", 90.0)]
        assert check_lineup_totals(weeks, games) == []

    def test_a_bench_player_counted_as_a_starter_shows_up_here(self):
        """The only witness the started/benched split has, and the stud prize rests on it."""
        weeks = [
            played(1, "t1", 60.0, player_id=1),
            played(1, "t1", 40.0, player_id=2),
            played(1, "t1", 25.0, player_id=3),  # a bench player wrongly marked started
        ]
        issues = check_lineup_totals(weeks, [game(1, "t1", 100.0, "t2", 90.0)])
        assert [i.code for i in issues] == [StatIssueCode.LINEUP_POINTS_MISMATCH]

    def test_rounding_slack_does_not_cry_wolf(self):
        weeks = [played(1, "t1", 100.01, player_id=1)]
        assert check_lineup_totals(weeks, [game(1, "t1", 100.0, "t2", 90.0)]) == []

    def test_champion_is_cross_checked_against_the_bracket(self):
        final = [game(17, "t1", 120.0, "t2", 100.0, tier=PlayoffTier.WINNERS_BRACKET)]
        assert check_champion_won_the_final(final, {"t1": 1, "t2": 2}) == []
        wrong = check_champion_won_the_final(final, {"t2": 1, "t1": 2})
        assert [i.code for i in wrong] == [StatIssueCode.CHAMPION_BRACKET_MISMATCH]

    def test_consolation_winner_is_reported_never_asserted(self):
        ladder = [
            game(17, "t5", 90.0, "t6", 80.0, tier=PlayoffTier.LOSERS_CONSOLATION_LADDER),
            game(17, "t7", 70.0, "t8", 60.0, tier=PlayoffTier.LOSERS_CONSOLATION_LADDER),
        ]
        found, issues = consolation_winner(ladder, {"t5": 7, "t6": 9, "t7": 8, "t8": 11})
        assert found == ("t5",)
        assert [i.code for i in issues] == [StatIssueCode.CONSOLATION_WINNER_UNCONFIRMED]
        assert issues[0].severity is Severity.REVIEW


class TestSplitPrize:
    def test_an_even_split(self):
        assert split_prize(40, 2) == (20, 0)

    def test_an_uneven_split_leaves_a_remainder_rather_than_a_float(self):
        assert split_prize(10, 3) == (3, 1)

    def test_a_single_winner_takes_it_all(self):
        assert split_prize(500, 1) == (500, 0)

    def test_no_winners_is_a_programming_error(self):
        with pytest.raises(ValueError):
            split_prize(10, 0)


def _season(scores, matchups, player_weeks, **kwargs):
    return compute_season_stats(
        season=SEASON,
        scores=scores,
        matchups=matchups,
        player_weeks=player_weeks,
        regular_season_weeks=kwargs.pop("regular_season_weeks", 1),
        **kwargs,
    )


class TestAwardPrizes:
    def _one_week_season(self):
        scores = [score(1, "t1", 100.0), score(1, "t2", 90.0)]
        matchups = [game(1, "t1", 100.0, "t2", 90.0)]
        weeks = [
            played(1, "t1", 100.0, position=position, player_id=index)
            for index, position in enumerate(STUD_POSITIONS)
        ]
        return _season(scores, matchups, weeks, final_ranks={"t1": 1, "t2": 2, "t3": 3})

    def test_every_prize_produces_a_row_and_the_pot_adds_up(self):
        stats = self._one_week_season()
        payouts, issues = award_prizes(stats, schedule(), {"t1": 1, "t2": 2, "t3": 3})
        assert sum(payout.amount for payout in payouts) == schedule().total(1)
        assert StatIssueCode.PRIZE_TOTAL_MISMATCH not in {i.code for i in issues}

    def test_winners_are_recorded_as_manager_ids_never_names(self):
        stats = self._one_week_season()
        payouts, _ = award_prizes(stats, schedule(), {"t1": 1, "t2": 2, "t3": 3})
        champion = next(p for p in payouts if p.label == "Champion")
        assert champion.winner_manager_id == "t1"
        assert all(
            payout.winner_manager_id is None or payout.winner_manager_id.startswith("t")
            for payout in payouts
        )

    def test_a_tied_prize_splits_evenly_and_says_so(self):
        scores = [score(1, "t1", 100.0), score(1, "t2", 100.0)]
        matchups = [game(1, "t1", 100.0, "t2", 100.0)]
        stats = _season(scores, matchups, [played(1, "t1", 100.0)])
        payouts, issues = award_prizes(stats, schedule(), {})
        week_one = [p for p in payouts if p.label == "Week 1 High Score"]
        assert sorted(p.winner_manager_id for p in week_one) == ["t1", "t2"]
        assert {p.amount for p in week_one} == {5}
        assert StatIssueCode.TIE_SPLIT in {i.code for i in issues}

    def test_an_indivisible_split_reports_the_remainder_rather_than_losing_it(self):
        scores = [score(1, f"t{i}", 100.0) for i in (1, 2, 3)]
        matchups = [game(1, "t1", 100.0, "t2", 100.0)]
        stats = _season(scores, matchups, [played(1, "t1", 100.0)])
        payouts, issues = award_prizes(stats, schedule(), {})
        week_one = [p for p in payouts if p.label == "Week 1 High Score"]
        assert [p.amount for p in week_one] == [3, 3, 3]
        remainder = next(
            i
            for i in issues
            if i.code is StatIssueCode.TIE_REMAINDER and "Week 1 High Score" in i.message
        )
        assert "$1 is unallocated" in remainder.message
        assert remainder.severity is Severity.REVIEW

    def test_a_prize_with_no_winner_still_holds_its_money(self):
        """So the pot always reconciles and a missing winner is visible, not absent."""
        stats = _season(
            [score(1, "t1", 100.0)], [game(1, "t1", 100.0, "t2", 90.0)], [played(1, "t1", 100.0)]
        )
        payouts, _ = award_prizes(stats, schedule(), {})
        champion = next(p for p in payouts if p.label == "Champion")
        assert champion.winner_manager_id is None and champion.amount == 500

    def test_amounts_come_from_the_season_not_from_a_constant(self):
        """2023 paid Survivor $50 where 2025 pays $40. Nothing here may hard-code either."""
        stats = self._one_week_season()
        payouts, _ = award_prizes(stats, schedule(survivor=50), {"t1": 1, "t2": 2, "t3": 3})
        assert next(p for p in payouts if p.label == "Survivor").amount == 50


class TestSeasonStats:
    def test_a_missing_placing_blocks(self):
        stats = _season(
            [score(1, "t1", 100.0)], [game(1, "t1", 100.0, "t2", 90.0)], [played(1, "t1", 100.0)],
            final_ranks={"t1": 1, "t2": 2},
        )
        assert stats.blocked
        assert StatIssueCode.FINAL_RANK_MISSING in {i.code for i in stats.issues}

    def test_review_issues_are_separable_and_do_not_block(self):
        """A complete season carrying one unverified item: it reports, it does not block.

        The mismatch is deliberate — t1's started players sum to 120 against a reported 100.
        """
        stats = _season(
            [score(1, "t1", 100.0), score(1, "t2", 90.0)],
            [game(1, "t1", 100.0, "t2", 90.0)],
            [
                played(1, "t1", 30.0, position=position, player_id=index)
                for index, position in enumerate(STUD_POSITIONS)
            ],
        )
        assert not stats.blocked
        assert [i.code for i in stats.review_issues] == [
            StatIssueCode.LINEUP_POINTS_MISMATCH
        ]
