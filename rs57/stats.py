"""Derived league stats and the prize engine.

**Pure.** No I/O, no ESPN, no reads from ``data/``. Everything arrives as arguments, exactly
like ``keeper_rules``. ``rs57.espn`` maps ESPN's payloads onto the models this module consumes;
the dependency only ever points that way.

Where the definitions come from
-------------------------------

None of these prizes are written down anywhere. The rules below were **reproduced against the
``RS57`` sheet**, not inferred from the plan doc, and each one is pinned by the season that
settles it:

* **Weekly high score** — top score of the week, weeks 1-14 only. ESPN's
  ``scheduleSettings.matchupPeriodCount`` is 14 and the sheet pays exactly 14 of them.
* **Most Points (Season)** — regular season only, weeks 1-14. 2025 cannot tell the difference,
  since the same franchise wins under either window, but **2023 can**: the two windows name
  *different* franchises there, and the sheet's winner is the one weeks 1-14 picks. The tab
  header reading "Thru: Week 17" is wrong.
* **Positional stud** — the best single *started* week by any player at the position, over the
  **whole season including the playoff weeks**. 2025's WR stud is W16 and its TE stud is W15,
  so this window is emphatically not the 14 weeks the high scores use.
* **Survivor** — the lowest score among the still-alive is eliminated each week, last one
  standing wins. Weeks 1 to ``teams - 1``. 2025's eleven eliminations reproduce in exact order.
* **Unlucky** — the single highest score all season that still lost its matchup. Once per
  season, not once per week; computing it weekly would pay it fourteen times. Regular season
  only, by commissioner decision (2026-07-28) — the sheet cannot settle the window, because
  1-14 and 1-17 give the same answer in all three recorded seasons.

Ties
----

No tie appears anywhere in three seasons of the sheet, so there is no precedent and this is the
one rule here that is a decision rather than a reproduction: **the prize is split evenly**
(commissioner, 2026-07-28). Money is integer dollars, so a prize that does not divide pays the
floor to each winner and reports the remainder as a REVIEW issue rather than rounding it away
or turning the money into a float.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from rs57.keeper_rules import Severity
from rs57.models import (
    Matchup,
    Payout,
    PlayerWeek,
    PlayoffTier,
    Position,
    PrizeSchedule,
    SeasonPoints,
    StandingRow,
    StudAward,
    SurvivorElimination,
    UnluckyAward,
    WeeklyHigh,
    WeeklyScore,
)

STUD_POSITIONS: tuple[Position, ...] = (Position.QB, Position.RB, Position.WR, Position.TE)
"""The four positions that pay a stud prize. The league also rosters DEF; it wins nothing."""

SCORE_TOLERANCE = 0.02
"""Cents of slack when cross-checking two of ESPN's own totals against each other. ESPN
rounds fantasy points to two places in more than one place, and an exact float compare would
cry wolf on every team."""


class StatIssueCode(StrEnum):
    MISSING_WEEK = "missing_week"
    TIE_SPLIT = "tie_split"
    TIE_REMAINDER = "tie_remainder"
    SURVIVOR_TIE = "survivor_tie"
    SURVIVOR_NO_WINNER = "survivor_no_winner"
    NO_STUD_FOR_POSITION = "no_stud_for_position"
    NO_UNLUCKY = "no_unlucky"
    LINEUP_POINTS_MISMATCH = "lineup_points_mismatch"
    STANDINGS_POINTS_MISMATCH = "standings_points_mismatch"
    FINAL_RANK_MISSING = "final_rank_missing"
    CHAMPION_BRACKET_MISMATCH = "champion_bracket_mismatch"
    CONSOLATION_WINNER_UNCONFIRMED = "consolation_winner_unconfirmed"
    PRIZE_TOTAL_MISMATCH = "prize_total_mismatch"
    NO_PRIZE_SCHEDULE = "no_prize_schedule"


@dataclass(frozen=True)
class StatIssue:
    """Same two severities as ``keeper_rules``, and the same contract.

    ERROR blocks. REVIEW needs the commissioner's eyes and renders as unverified — it must
    never pass silently as though somebody had checked it.
    """

    code: StatIssueCode
    severity: Severity
    message: str
    manager_id: str | None = None
    week: int | None = None
    position: Position | None = None


def split_prize(amount: int, winners: int) -> tuple[int, int]:
    """Split a prize evenly into whole dollars. Returns ``(each, remainder)``.

    The remainder is never dropped and never rounded into somebody's share — the caller
    reports it, because all money here is integer dollars and a three-way split of $10 is a
    question for the commissioner, not for a floor division to answer quietly.
    """
    if winners <= 0:
        raise ValueError("cannot split a prize between no winners")
    return amount // winners, amount % winners


def _leaders(points: Mapping[str, float], *, lowest: bool = False) -> tuple[tuple[str, ...], float]:
    """Every manager tied at the best score, and that score. Sorted for stable output."""
    best = min(points.values()) if lowest else max(points.values())
    return tuple(sorted(m for m, p in points.items() if p == best)), best


def _by_week(scores: Iterable[WeeklyScore]) -> dict[int, dict[str, float]]:
    table: dict[int, dict[str, float]] = defaultdict(dict)
    for score in scores:
        table[score.week][score.manager_id] = score.points
    return table


def weekly_high_scores(
    scores: Iterable[WeeklyScore], weeks: Sequence[int]
) -> tuple[list[WeeklyHigh], list[StatIssue]]:
    """Top score in each of ``weeks``. Weeks 1-14 — the playoff weeks pay nothing here."""
    scores = list(scores)
    table = _by_week(scores)
    season = next((score.season for score in scores), 0)
    highs: list[WeeklyHigh] = []
    issues: list[StatIssue] = []
    for week in weeks:
        if not table.get(week):
            issues.append(
                StatIssue(
                    StatIssueCode.MISSING_WEEK,
                    Severity.ERROR,
                    f"no scores recorded for week {week}; its $ prize cannot be awarded",
                    week=week,
                )
            )
            continue
        winners, points = _leaders(table[week])
        highs.append(WeeklyHigh(season=season, week=week, manager_ids=winners, points=points))
    return highs, issues


def season_points(scores: Iterable[WeeklyScore], weeks: Sequence[int]) -> list[SeasonPoints]:
    """Points scored across ``weeks``, highest first.

    Regular season only — see the module docstring for the 2023 evidence. This is the same
    number ESPN reports as ``team.points``, which is what ``check_standings_against_espn``
    uses to catch a week being dropped on the floor.
    """
    wanted = set(weeks)
    totals: dict[str, float] = defaultdict(float)
    season = 0
    for score in scores:
        if score.week in wanted:
            totals[score.manager_id] += score.points
            season = score.season
    return [
        SeasonPoints(season=season, manager_id=manager, points=round(points, 2))
        for manager, points in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def positional_studs(
    player_weeks: Iterable[PlayerWeek],
) -> tuple[list[StudAward], list[StatIssue]]:
    """Best single started week per position, across the whole season.

    A benched player wins nothing however much he scored, which is why ``PlayerWeek.started``
    exists at all. Where two players tie at the top of a position, both are reported and the
    prize splits.
    """
    best: dict[Position, list[PlayerWeek]] = {}
    season = 0
    for week in player_weeks:
        season = week.season
        if not week.started or week.position not in STUD_POSITIONS:
            continue
        current = best.get(week.position)
        if current is None or week.points > current[0].points:
            best[week.position] = [week]
        elif week.points == current[0].points:
            current.append(week)

    awards: list[StudAward] = []
    issues: list[StatIssue] = []
    for position in STUD_POSITIONS:
        tied = best.get(position)
        if not tied:
            issues.append(
                StatIssue(
                    StatIssueCode.NO_STUD_FOR_POSITION,
                    Severity.ERROR,
                    f"no started {position} scored all season; the ${position} stud prize "
                    f"cannot be awarded",
                    position=position,
                )
            )
            continue
        top = tied[0]
        awards.append(
            StudAward(
                season=season,
                position=position,
                espn_player_id=top.espn_player_id,
                player_name=top.player_name,
                week=top.week,
                points=top.points,
                manager_ids=tuple(sorted({week.manager_id for week in tied})),
            )
        )
    return awards, issues


@dataclass(frozen=True)
class SurvivorResult:
    eliminations: tuple[SurvivorElimination, ...]
    winner_ids: tuple[str, ...]


def survivor(
    scores: Iterable[WeeklyScore], *, max_weeks: int | None = None
) -> tuple[SurvivorResult, list[StatIssue]]:
    """Lowest score among the still-alive goes out each week; last standing wins.

    The window is not a constant: it runs until one franchise is left, which in a 12-team
    league is weeks 1-11 — and that is exactly what the sheet records. Deriving it from the
    number of franchises rather than hard-coding 11 means it survives the league changing size.

    A tie for lowest eliminates **everyone** tied, which can end the run early or wipe out the
    last survivors. Both cases are reported: nothing about a tie is decided quietly here.
    """
    scores = list(scores)
    table = _by_week(scores)
    season = next((score.season for score in scores), 0)
    alive = set(table.get(1, {}))
    limit = max_weeks if max_weeks is not None else len(alive) - 1

    eliminations: list[SurvivorElimination] = []
    issues: list[StatIssue] = []
    for week in range(1, limit + 1):
        if len(alive) <= 1:
            break
        live = {m: p for m, p in table.get(week, {}).items() if m in alive}
        if not live:
            issues.append(
                StatIssue(
                    StatIssueCode.MISSING_WEEK,
                    Severity.ERROR,
                    f"no scores for week {week}; survivor cannot be run",
                    week=week,
                )
            )
            break
        out, points = _leaders(live, lowest=True)
        if len(out) > 1:
            issues.append(
                StatIssue(
                    StatIssueCode.SURVIVOR_TIE,
                    Severity.REVIEW,
                    f"week {week} ends in a {len(out)}-way tie for lowest score at "
                    f"{points}; all of them are eliminated, which has never happened before "
                    f"and needs the commissioner's eye",
                    week=week,
                )
            )
        eliminations.append(
            SurvivorElimination(
                season=season, week=week, manager_ids=out, points=points
            )
        )
        alive -= set(out)

    if len(alive) != 1:
        message = (
            f"survivor ended with {len(alive)} franchises standing, not 1 — the prize splits "
            f"between them"
            if alive
            else "survivor eliminated everyone; there is no winner to pay"
        )
        issues.append(
            StatIssue(StatIssueCode.SURVIVOR_NO_WINNER, Severity.REVIEW, message)
        )
    return SurvivorResult(tuple(eliminations), tuple(sorted(alive))), issues


def unlucky(
    matchups: Iterable[Matchup], weeks: Sequence[int]
) -> tuple[UnluckyAward | None, list[StatIssue]]:
    """The highest score that still lost, once for the season.

    A season-long award. The sheet's row beneath it holds a single week and a single score,
    which is the tell — computing it per week would pay $20 fourteen times over.

    Needs matchup results, not just weekly totals: a big score only counts if it lost. A tie
    is not a loss, so ``Matchup.loser`` returns nothing for one.
    """
    wanted = set(weeks)
    losses: list[tuple[float, int, str]] = []
    season = 0
    for matchup in matchups:
        season = matchup.season
        if matchup.week not in wanted:
            continue
        beaten = matchup.loser
        if beaten is not None:
            losses.append((beaten[1], matchup.week, beaten[0]))

    if not losses:
        return None, [
            StatIssue(
                StatIssueCode.NO_UNLUCKY,
                Severity.ERROR,
                "no completed losses in the regular season; Unlucky cannot be awarded",
            )
        ]

    top = max(points for points, _, _ in losses)
    tied = sorted((week, manager) for points, week, manager in losses if points == top)
    return (
        UnluckyAward(
            season=season,
            week=tied[0][0],
            manager_ids=tuple(sorted(manager for _, manager in tied)),
            points=top,
        ),
        [],
    )


def standings(
    matchups: Iterable[Matchup],
    weeks: Sequence[int],
    *,
    final_ranks: Mapping[str, int] | None = None,
    playoff_seeds: Mapping[str, int] | None = None,
) -> list[StandingRow]:
    """Regular-season records, computed from the matchups rather than read off ESPN.

    Computing them is what makes ESPN's own ``team.points`` a *second* source instead of the
    only one — see ``check_standings_against_espn``. Ordered by wins then points for, which is
    the league's own tiebreak (``playoffSeedingRule: TOTAL_POINTS_SCORED``).
    """
    wanted = set(weeks)
    wins: dict[str, int] = defaultdict(int)
    losses: dict[str, int] = defaultdict(int)
    ties: dict[str, int] = defaultdict(int)
    points_for: dict[str, float] = defaultdict(float)
    points_against: dict[str, float] = defaultdict(float)
    season = 0

    for matchup in matchups:
        season = matchup.season
        if matchup.week not in wanted or matchup.is_bye:
            continue
        home, away = matchup.home_manager_id, matchup.away_manager_id
        assert away is not None  # not a bye
        points_for[home] += matchup.home_points
        points_for[away] += matchup.away_points or 0.0
        points_against[home] += matchup.away_points or 0.0
        points_against[away] += matchup.home_points
        if matchup.tied:
            ties[home] += 1
            ties[away] += 1
        else:
            beaten = matchup.loser
            assert beaten is not None  # not a bye and not tied
            winner = away if beaten[0] == home else home
            wins[winner] += 1
            losses[beaten[0]] += 1

    final_ranks = final_ranks or {}
    playoff_seeds = playoff_seeds or {}
    rows = [
        StandingRow(
            season=season,
            manager_id=manager,
            wins=wins[manager],
            losses=losses[manager],
            ties=ties[manager],
            points_for=round(points_for[manager], 2),
            points_against=round(points_against[manager], 2),
            final_rank=final_ranks.get(manager),
            playoff_seed=playoff_seeds.get(manager),
        )
        for manager in sorted(points_for)
    ]
    return sorted(rows, key=lambda row: (-row.wins, -row.points_for, row.manager_id))


def consolation_winner(
    matchups: Iterable[Matchup], final_ranks: Mapping[str, int] | None = None
) -> tuple[tuple[str, ...], list[StatIssue]]:
    """Who most likely won the consolation bracket, reported for confirmation.

    Feeds ``Season.consolation_winner_id``, which waives that franchise's keeper fees for the
    following year — so it is deliberately **reported, never auto-populated**. Two things make
    it unsafe to infer:

    * A 12-team league runs *two* consolation ladders (``WINNERS_CONSOLATION_LADDER`` for teams
      knocked out of the playoffs, ``LOSERS_CONSOLATION_LADDER`` for the six that missed) and
      nothing in ESPN says which one the league means by "the consolation bracket".
    * A ladder is not a bracket. Its last week is several parallel placement games, not one
      final, so "who won the last game" has more than one answer.

    So this reports the best-finishing franchise in the losers' ladder — which is the ladder's
    actual champion, by ESPN's own final ranking — and always draws a REVIEW. Reading it off by
    one waives the wrong team's fees for a whole season.
    """
    ladder = [
        matchup for matchup in matchups if matchup.tier is PlayoffTier.LOSERS_CONSOLATION_LADDER
    ]
    if not ladder:
        return (), []

    entrants = {matchup.home_manager_id for matchup in ladder}
    entrants |= {m.away_manager_id for m in ladder if m.away_manager_id is not None}
    final_week = max(matchup.week for matchup in ladder)

    ranked = {m: r for m, r in (final_ranks or {}).items() if m in entrants}
    if ranked:
        best = min(ranked.values())
        found = tuple(sorted(m for m, r in ranked.items() if r == best))
        detail = f"finished {best}th overall, the best of the {len(entrants)} in that ladder"
    else:
        found = ()
        detail = "could not be identified — no final ranking was available"

    return found, [
        StatIssue(
            StatIssueCode.CONSOLATION_WINNER_UNCONFIRMED,
            Severity.REVIEW,
            f"losers' consolation ladder (through week {final_week}): "
            f"{', '.join(found) or 'nobody'} {detail}. Confirm this is the bracket whose "
            f"winner gets next season's fees waived before setting consolation_winner_id",
            week=final_week,
        )
    ]


def check_lineup_totals(
    player_weeks: Iterable[PlayerWeek], matchups: Iterable[Matchup]
) -> list[StatIssue]:
    """Every started player's points should sum to the team's score for that week.

    This is the check that would catch a misread ``lineupSlotId`` — the single assumption the
    stud prize rests on. If bench players were counted as starters, or a flex slot were read as
    a bench, the sums stop matching. Cheap, and the only witness the started/benched split has.
    """
    lineup: dict[tuple[int, str], float] = defaultdict(float)
    for week in player_weeks:
        if week.started:
            lineup[(week.week, week.manager_id)] += week.points

    reported: dict[tuple[int, str], float] = {}
    for matchup in matchups:
        reported[(matchup.week, matchup.home_manager_id)] = matchup.home_points
        if matchup.away_manager_id is not None:
            reported[(matchup.week, matchup.away_manager_id)] = matchup.away_points or 0.0

    issues: list[StatIssue] = []
    for key, summed in sorted(lineup.items()):
        week, manager = key
        if key not in reported:
            continue
        if abs(summed - reported[key]) > SCORE_TOLERANCE:
            issues.append(
                StatIssue(
                    StatIssueCode.LINEUP_POINTS_MISMATCH,
                    Severity.REVIEW,
                    f"started lineup sums to {summed:.2f} but the matchup reports "
                    f"{reported[key]:.2f} — the started/benched split may be misread, which "
                    f"is what the positional stud prize rests on",
                    manager_id=manager,
                    week=week,
                )
            )
    return issues


def check_standings_against_espn(
    rows: Sequence[StandingRow], espn_points: Mapping[str, float]
) -> list[StatIssue]:
    """Cross-check computed points for against the total ESPN reports for the team.

    Two independent routes to the same number: ours sums the matchup scores week by week,
    ESPN's is on the team record. A dropped week shows up here and nowhere else.
    """
    issues: list[StatIssue] = []
    for row in rows:
        reported = espn_points.get(row.manager_id)
        if reported is None:
            continue
        if abs(row.points_for - reported) > SCORE_TOLERANCE:
            issues.append(
                StatIssue(
                    StatIssueCode.STANDINGS_POINTS_MISMATCH,
                    Severity.REVIEW,
                    f"computed {row.points_for:.2f} points for, ESPN reports "
                    f"{reported:.2f} — a week may be missing from the schedule read",
                    manager_id=row.manager_id,
                )
            )
    return issues


def check_champion_won_the_final(
    matchups: Sequence[Matchup], final_ranks: Mapping[str, int]
) -> list[StatIssue]:
    """The franchise ranked 1st should have won the last game of the winners' bracket.

    ESPN's ``rankCalculatedFinal`` pays $500, so it gets a second witness rather than being
    trusted on its own.
    """
    bracket = [
        matchup
        for matchup in matchups
        if matchup.tier is PlayoffTier.WINNERS_BRACKET and not matchup.is_bye
    ]
    champions = [manager for manager, rank in final_ranks.items() if rank == 1]
    if not bracket or not champions:
        return []

    final_week = max(matchup.week for matchup in bracket)
    victors: set[str] = set()
    for matchup in bracket:
        if matchup.week != final_week or matchup.tied:
            continue
        beaten = matchup.loser
        assert beaten is not None
        victors.add(
            matchup.away_manager_id
            if beaten[0] == matchup.home_manager_id
            else matchup.home_manager_id
        )
    if champions[0] in victors:
        return []
    return [
        StatIssue(
            StatIssueCode.CHAMPION_BRACKET_MISMATCH,
            Severity.REVIEW,
            f"ESPN ranks {champions[0]} first, but the winners' bracket final in week "
            f"{final_week} was won by {', '.join(sorted(victors)) or 'nobody'}",
            manager_id=champions[0],
            week=final_week,
        )
    ]


@dataclass(frozen=True)
class SeasonStats:
    """Everything Phase 2 derives for one season, plus what it could not vouch for."""

    season: int
    regular_season_weeks: tuple[int, ...]
    standings: tuple[StandingRow, ...]
    weekly_highs: tuple[WeeklyHigh, ...]
    season_points: tuple[SeasonPoints, ...]
    studs: tuple[StudAward, ...]
    survivor_eliminations: tuple[SurvivorElimination, ...]
    survivor_winner_ids: tuple[str, ...]
    unlucky: UnluckyAward | None
    consolation_winner_ids: tuple[str, ...]
    issues: tuple[StatIssue, ...]

    @property
    def blocked(self) -> bool:
        return any(issue.severity is Severity.ERROR for issue in self.issues)

    @property
    def review_issues(self) -> tuple[StatIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.REVIEW)


def compute_season_stats(
    *,
    season: int,
    scores: Sequence[WeeklyScore],
    matchups: Sequence[Matchup],
    player_weeks: Sequence[PlayerWeek],
    regular_season_weeks: int,
    final_ranks: Mapping[str, int] | None = None,
    playoff_seeds: Mapping[str, int] | None = None,
    espn_points: Mapping[str, float] | None = None,
) -> SeasonStats:
    """Run every derived stat for one season, in one pass, with all its cross-checks.

    ``regular_season_weeks`` comes from ESPN's ``scheduleSettings.matchupPeriodCount`` rather
    than a constant. It decides three of the six prizes — weekly highs, Most Points, and
    Unlucky — and the league has changed it before, in the season that also produced the $9.29
    weekly prize.
    """
    weeks = list(range(1, regular_season_weeks + 1))
    issues: list[StatIssue] = []

    highs, high_issues = weekly_high_scores(scores, weeks)
    issues += high_issues
    studs, stud_issues = positional_studs(player_weeks)
    issues += stud_issues
    survived, survivor_issues = survivor(scores)
    issues += survivor_issues
    unlucky_award, unlucky_issues = unlucky(matchups, weeks)
    issues += unlucky_issues
    consolation, consolation_issues = consolation_winner(matchups, final_ranks)
    issues += consolation_issues

    rows = standings(matchups, weeks, final_ranks=final_ranks, playoff_seeds=playoff_seeds)
    issues += check_lineup_totals(player_weeks, matchups)
    if espn_points:
        issues += check_standings_against_espn(rows, espn_points)
    if final_ranks:
        issues += check_champion_won_the_final(matchups, final_ranks)
        for rank in (1, 2, 3):
            if rank not in final_ranks.values():
                issues.append(
                    StatIssue(
                        StatIssueCode.FINAL_RANK_MISSING,
                        Severity.ERROR,
                        f"no franchise holds final rank {rank}; the placing prize for it "
                        f"cannot be awarded",
                    )
                )

    return SeasonStats(
        season=season,
        regular_season_weeks=tuple(weeks),
        standings=tuple(rows),
        weekly_highs=tuple(highs),
        season_points=tuple(season_points(scores, weeks)),
        studs=tuple(studs),
        survivor_eliminations=survived.eliminations,
        survivor_winner_ids=survived.winner_ids,
        unlucky=unlucky_award,
        consolation_winner_ids=consolation,
        issues=tuple(issues),
    )


def _award(
    season: int, label: str, amount: int, winners: Sequence[str]
) -> tuple[list[Payout], list[StatIssue]]:
    """One prize, split evenly between however many franchises tied for it."""
    if not winners:
        return [Payout(season=season, label=label, amount=amount)], []

    each, remainder = split_prize(amount, len(winners))
    payouts = [
        Payout(season=season, label=label, amount=each, winner_manager_id=manager)
        for manager in winners
    ]
    issues: list[StatIssue] = []
    if len(winners) > 1:
        issues.append(
            StatIssue(
                StatIssueCode.TIE_SPLIT,
                Severity.REVIEW,
                f"{label} is a {len(winners)}-way tie ({', '.join(winners)}); ${amount} "
                f"splits to ${each} each",
            )
        )
    if remainder:
        issues.append(
            StatIssue(
                StatIssueCode.TIE_REMAINDER,
                Severity.REVIEW,
                f"{label}: ${amount} does not divide between {len(winners)} winners — "
                f"${remainder} is unallocated and needs the commissioner to place it",
            )
        )
    return payouts, issues


def award_prizes(
    stats: SeasonStats,
    schedule: PrizeSchedule,
    final_ranks: Mapping[str, int] | None = None,
) -> tuple[list[Payout], list[StatIssue]]:
    """Turn the derived stats into ``Payout`` rows.

    Winners are ``manager_id`` — which is keyed on ``espn_team_id``, never on a person. Tying
    a payout to a human being is a separate mapping that this repo deliberately does not hold.

    A prize with no identifiable winner still produces a row, with ``winner_manager_id`` unset,
    so the season's pot always adds up to the recorded total and a missing winner is visible
    rather than absent.
    """
    payouts: list[Payout] = []
    issues: list[StatIssue] = []
    ranks = final_ranks or {}
    by_rank = {rank: manager for manager, rank in ranks.items()}

    for rank, label, amount in (
        (1, "Champion", schedule.champion),
        (2, "2nd Place", schedule.second),
        (3, "3rd Place", schedule.third),
    ):
        winner = by_rank.get(rank)
        rows, found = _award(stats.season, label, amount, [winner] if winner else [])
        payouts += rows
        issues += found

    leaders: list[str] = []
    if stats.season_points:
        best = stats.season_points[0].points
        leaders = [row.manager_id for row in stats.season_points if row.points == best]
    rows, found = _award(stats.season, "Most Points (Season)", schedule.most_points, leaders)
    payouts += rows
    issues += found

    rows, found = _award(
        stats.season, "Survivor", schedule.survivor, list(stats.survivor_winner_ids)
    )
    payouts += rows
    issues += found

    for stud in stats.studs:
        rows, found = _award(
            stats.season, f"{stud.position} Stud", schedule.stud, list(stud.manager_ids)
        )
        payouts += rows
        issues += found

    if stats.unlucky is not None:
        rows, found = _award(
            stats.season, "Unlucky", schedule.unlucky, list(stats.unlucky.manager_ids)
        )
        payouts += rows
        issues += found

    for high in stats.weekly_highs:
        rows, found = _award(
            stats.season,
            f"Week {high.week} High Score",
            schedule.weekly_high,
            list(high.manager_ids),
        )
        payouts += rows
        issues += found

    expected = schedule.total(len(stats.regular_season_weeks), len(STUD_POSITIONS))
    actual = sum(payout.amount for payout in payouts)
    if actual != expected:
        issues.append(
            StatIssue(
                StatIssueCode.PRIZE_TOTAL_MISMATCH,
                Severity.REVIEW,
                f"payouts total ${actual}, the recorded prize structure totals ${expected} — "
                f"the difference is money nobody has been assigned",
            )
        )
    return payouts, issues
