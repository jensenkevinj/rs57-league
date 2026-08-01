"""Write one season of derived stats to ``data/derived/{year}-stats.json``.

    python -m rs57.stats_sync --year 2025 --dry-run

The nightly Action owns ``data/derived/``. Locally use ``--dry-run``, which reports without
writing so it cannot leave an untracked file for someone to commit by accident.

Separate file from ``{year}.json`` on purpose. The keeper sync and the stats sync read
different ESPN views and fail for different reasons, and NO FILE HAS TWO WRITERS — one writer
per file keeps a broken box score from being able to blank a season of salaries.

Winners are recorded as ``manager_id``, which is keyed on ``espn_team_id``. **No real name
enters this file**, and the repo holds no mapping from one to the other.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from rs57.espn import EspnClient, EspnError, SyncedScoring, build_scoring_season
from rs57.keeper_rules import Severity
from rs57.models import Payout, PrizeSchedule, dump_json
from rs57.stats import (
    SeasonStats,
    StatIssue,
    StatIssueCode,
    award_prizes,
    compute_season_stats,
)

DATA = Path(__file__).resolve().parent.parent / "data"
DERIVED = DATA / "derived"
PAYOUTS = DATA / "manual" / "payouts.json"


def prize_schedule(season: int, path: Path = PAYOUTS) -> PrizeSchedule | None:
    """The recorded prize amounts for ``season``, or ``None`` if the season is not recorded.

    Read-only: ``data/manual/`` is the human-owned store and the Action must never write it.

    ``None`` is a real answer, not a failure. 2023's $9.29 weekly prize cannot be represented
    in integer dollars, so that season is deliberately absent — its stats still compute, it
    just gets no payout rows.
    """
    if not path.exists():
        return None
    seasons = json.loads(path.read_text(encoding="utf-8")).get("seasons") or {}
    amounts = seasons.get(str(season))
    if amounts is None:
        return None
    return PrizeSchedule(season=season, **amounts)


def stats_document(
    stats: SeasonStats, scoring: SyncedScoring, payouts: list[Payout], issues: list[StatIssue]
) -> dict[str, Any]:
    """The on-disk shape of a derived season's stats.

    REVIEW issues are written into the file rather than printed and forgotten. A consumer that
    renders a prize list has to be able to see which entries nobody has checked — a REVIEW item
    must never read as though it had been.
    """
    return {
        "season": stats.season,
        "source": {
            "regular_season_weeks": scoring.regular_season_weeks,
            "weeks_with_results": sorted({score.week for score in scoring.scores}),
            # The bracket's size, which is what says how many rounds the playoffs take. The
            # site names the round from it; without it the page says only "Playoffs".
            "playoff_team_count": scoring.playoff_team_count,
        },
        "standings": list(stats.standings),
        "weekly_high_scores": list(stats.weekly_highs),
        "season_points": list(stats.season_points),
        "positional_studs": list(stats.studs),
        "survivor": {
            "eliminations": list(stats.survivor_eliminations),
            "winner_manager_ids": list(stats.survivor_winner_ids),
        },
        "unlucky": stats.unlucky,
        "payouts": payouts,
        "review": {
            "consolation_winner_manager_ids": list(stats.consolation_winner_ids),
            "warnings": list(scoring.warnings),
            "issues": [
                {
                    "code": issue.code,
                    "severity": issue.severity,
                    "message": issue.message,
                    "manager_id": issue.manager_id,
                    "week": issue.week,
                    "position": issue.position,
                }
                for issue in issues
            ],
        },
    }


def sync_stats(
    year: int, *, out_dir: Path = DERIVED, write: bool = True
) -> tuple[SeasonStats, SyncedScoring, list[Payout], list[StatIssue]]:
    """Read ``year``'s scoring from ESPN, derive every stat, and award the prizes."""
    client = EspnClient.from_env(year)
    scoring = build_scoring_season(client)

    stats = compute_season_stats(
        season=year,
        scores=scoring.scores,
        matchups=scoring.matchups,
        player_weeks=scoring.player_weeks,
        regular_season_weeks=scoring.regular_season_weeks,
        final_ranks=scoring.final_ranks,
        playoff_seeds=scoring.playoff_seeds,
        playoff_team_count=scoring.playoff_team_count,
        espn_points=scoring.espn_points,
    )

    issues = list(stats.issues)
    payouts: list[Payout] = []
    schedule = prize_schedule(year)
    if schedule is None:
        # Not an error. 2023 is deliberately unrecorded — its $9.29 weekly prize cannot be
        # an integer dollar — and its stats are still worth deriving.
        issues.append(
            StatIssue(
                StatIssueCode.NO_PRIZE_SCHEDULE,
                Severity.REVIEW,
                f"no prize amounts recorded for {year} in data/manual/payouts.json, so the "
                f"stats were derived but nobody was awarded anything",
            )
        )
    else:
        payouts, prize_issues = award_prizes(stats, schedule, scoring.final_ranks)
        issues += prize_issues

    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        dump_json(stats_document(stats, scoring, payouts, issues), out_dir / f"{year}-stats.json")
    return stats, scoring, payouts, issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive one season's stats and prizes to data/derived/."
    )
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and report, write nothing"
    )
    args = parser.parse_args(argv)

    try:
        stats, scoring, payouts, issues = sync_stats(args.year, write=not args.dry_run)
    except EspnError as exc:
        # EspnError messages never carry credentials; see EspnClient._get.
        print(f"stats sync failed: {exc}", file=sys.stderr)
        return 1

    print(f"{stats.season}: {len(scoring.matchups)} completed matchups, "
          f"{len(scoring.player_weeks)} player-weeks")
    print(f"  regular season: weeks 1-{scoring.regular_season_weeks}")

    for high in stats.weekly_highs:
        print(f"  W{high.week:<2} high  {'/'.join(high.manager_ids):<12} {high.points:>7.2f}")
    if stats.season_points:
        top = stats.season_points[0]
        print(f"  most points  {top.manager_id:<12} {top.points:>7.2f}")
    for stud in stats.studs:
        print(
            f"  {stud.position:<3} stud    {'/'.join(stud.manager_ids):<12} "
            f"{stud.points:>7.2f}  {stud.player_name} W{stud.week}"
        )
    if stats.unlucky:
        print(
            f"  unlucky      {'/'.join(stats.unlucky.manager_ids):<12} "
            f"{stats.unlucky.points:>7.2f}  W{stats.unlucky.week}"
        )
    print(f"  survivor     {'/'.join(stats.survivor_winner_ids) or '(none)'}")

    if payouts:
        print(f"  payouts: ${sum(payout.amount for payout in payouts)} across {len(payouts)} rows")

    for warning in scoring.warnings:
        print(f"  REVIEW: {warning}")
    for issue in issues:
        if issue.severity is Severity.REVIEW:
            print(f"  REVIEW: [{issue.code}] {issue.message}")
    for issue in issues:
        if issue.severity is Severity.ERROR:
            print(f"  ERROR:  [{issue.code}] {issue.message}", file=sys.stderr)

    if args.dry_run:
        print("  dry run, nothing written")
    else:
        print(f"  wrote {DERIVED / f'{stats.season}-stats.json'}")
    return 1 if stats.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
