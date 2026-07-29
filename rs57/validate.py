"""Validate everything under ``data/``, and fail CI when it does not hold together.

    python -m rs57.validate

Reads only. It never writes ``data/`` — the nightly Action owns ``derived/``, the admin tool
owns ``manual/``, and ``history/`` is frozen. NO FILE HAS TWO WRITERS, and a validator is not
one of them.

What it checks
--------------

* every file under ``data/`` loads into its model (``extra="forbid"``, so schema drift is a
  failure here rather than a ``None`` three modules downstream)
* cross-file references: no roster entry for an unknown franchise, no orphan player
* keeper claims: no duplicate slot, no duplicate player, fee sums match the tier
* the ratchet, via ``keeper_rules.check_base_continuity``
* draft-cash trades net to zero, via ``keeper_rules.check_override_balance``
* derived stats reference franchises that exist, and the payouts add up

**A skipped check is reported, never assumed passed.** ``data/history/`` is empty today, so
the two ``keeper_rules`` audits have nothing to compare against; they print as SKIPPED with
the reason. Silence would read exactly like success.

Exit codes: ``0`` clean, ``1`` at least one ERROR. REVIEW items never block by themselves —
that is what REVIEW means — but they are always printed and always counted, and ``--strict``
makes them blocking for anyone who wants a gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from rs57.keeper_rules import (
    Severity,
    check_base_continuity,
    check_override_balance,
    validate_team_claims,
)
from rs57.models import (
    FranchiseName,
    KeeperClaim,
    Player,
    PrizeSchedule,
    RosterEntry,
    SalaryOverride,
)

DATA = Path(__file__).resolve().parent.parent / "data"


@dataclass
class Report:
    """What the run found, and — just as important — what it could not check."""

    errors: list[str]
    reviews: list[str]
    skipped: list[str]
    checked: list[str]

    @classmethod
    def empty(cls) -> Report:
        return cls(errors=[], reviews=[], skipped=[], checked=[])

    def error(self, message: str) -> None:
        self.errors.append(message)

    def review(self, message: str) -> None:
        self.reviews.append(message)

    def skip(self, message: str) -> None:
        self.skipped.append(message)

    def ok(self, message: str) -> None:
        self.checked.append(message)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _season_files(directory: Path, suffix: str = ".json") -> list[Path]:
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob(f"*{suffix}") if not p.name.startswith("."))


@dataclass(frozen=True)
class LoadedSeason:
    """One derived season, already through the models."""

    season: str
    franchises: list[FranchiseName]
    players: list[Player]
    roster: list[RosterEntry]


def validate_derived_season(path: Path, report: Report) -> LoadedSeason | None:
    """Load one ``data/derived/{year}.json`` and check it refers only to itself."""
    try:
        doc = _load(path)
    except json.JSONDecodeError as exc:
        report.error(f"{path.name}: not valid JSON ({exc})")
        return None

    try:
        franchises = [FranchiseName(**row) for row in doc.get("franchises") or []]
        players = [Player(**row) for row in doc.get("players") or []]
        roster = [RosterEntry(**row) for row in doc.get("roster") or []]
    except (ValidationError, TypeError, KeyError) as exc:
        report.error(f"{path.name}: does not load into the models — {exc}")
        return None

    known_managers = {franchise.manager_id for franchise in franchises}
    known_players = {player.espn_player_id for player in players}

    for entry in roster:
        if entry.manager_id not in known_managers:
            report.error(
                f"{path.name}: roster entry for player {entry.espn_player_id} names manager "
                f"{entry.manager_id!r}, which has no franchise in this season"
            )
        if entry.espn_player_id not in known_players:
            report.error(
                f"{path.name}: roster entry for {entry.manager_id} names player "
                f"{entry.espn_player_id}, which is not in this season's player list"
            )

    rostered = {entry.espn_player_id for entry in roster}
    for orphan in sorted(known_players - rostered):
        report.error(f"{path.name}: player {orphan} appears in players but on no roster")

    if roster:
        report.ok(
            f"{path.name}: {len(roster)} roster entries across {len(franchises)} franchises, "
            f"all references resolve"
        )
    return LoadedSeason(
        season=str(doc.get("season")),
        franchises=franchises,
        players=players,
        roster=roster,
    )


def validate_claims(
    claims: Sequence[KeeperClaim],
    roster: Sequence[RosterEntry],
    overrides: Sequence[SalaryOverride],
    label: str,
    report: Report,
) -> None:
    """Run the keeper engine's own validation over recorded claims, team by team."""
    by_manager: dict[str, list[KeeperClaim]] = {}
    for claim in claims:
        by_manager.setdefault(claim.manager_id, []).append(claim)

    for manager, team_claims in sorted(by_manager.items()):
        team_roster = [entry for entry in roster if entry.manager_id == manager]
        for issue in validate_team_claims(team_claims, team_roster):
            message = f"{label}: {manager}: [{issue.code}] {issue.message}"
            if issue.severity is Severity.ERROR:
                report.error(message)
            else:
                report.review(message)
    if by_manager:
        report.ok(f"{label}: keeper claims validated for {len(by_manager)} teams")


def validate_history(report: Report) -> tuple[list[KeeperClaim], list[SalaryOverride]]:
    """Load recorded claims and overrides from ``data/history/``, if there are any.

    The shape is deliberately permissive: ``history/`` is written once per completed season and
    nothing has written one yet. An empty directory is reported as a skipped check, because the
    two audits below cannot run without it and a silent pass would be a lie.
    """
    claims: list[KeeperClaim] = []
    overrides: list[SalaryOverride] = []
    files = _season_files(DATA / "history")
    if not files:
        report.skip(
            "data/history/ is empty — no recorded keeper claims to audit, so "
            "check_base_continuity has nothing to compare this season's bases against"
        )
        return claims, overrides

    for path in files:
        try:
            doc = _load(path)
            claims += [KeeperClaim(**row) for row in doc.get("claims") or []]
            overrides += [SalaryOverride(**row) for row in doc.get("overrides") or []]
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError) as exc:
            report.error(f"{path.name}: does not load into the models — {exc}")
    report.ok(f"data/history/: {len(claims)} claims, {len(overrides)} overrides loaded")
    return claims, overrides


def validate_stats_season(path: Path, franchises: set[str], report: Report) -> None:
    """Check a derived stats file against the season's franchises and its own arithmetic."""
    try:
        doc = _load(path)
    except json.JSONDecodeError as exc:
        report.error(f"{path.name}: not valid JSON ({exc})")
        return

    named: set[str] = set()
    for row in doc.get("standings") or []:
        named.add(row["manager_id"])
    for row in doc.get("weekly_high_scores") or []:
        named.update(row["manager_ids"])
    for row in doc.get("positional_studs") or []:
        named.update(row["manager_ids"])
    for row in (doc.get("survivor") or {}).get("eliminations") or []:
        named.update(row["manager_ids"])
    for payout in doc.get("payouts") or []:
        if payout.get("winner_manager_id"):
            named.add(payout["winner_manager_id"])

    if franchises:
        for unknown in sorted(named - franchises):
            report.error(
                f"{path.name}: names manager {unknown!r}, which has no franchise in the "
                f"matching derived season file"
            )
    else:
        report.skip(
            f"{path.name}: no matching data/derived/{doc.get('season')}.json, so its "
            f"manager ids could not be checked against the season's franchises"
        )

    schedule = None
    season = doc.get("season")
    payouts_path = DATA / "manual" / "payouts.json"
    if payouts_path.exists() and season is not None:
        amounts = (_load(payouts_path).get("seasons") or {}).get(str(season))
        if amounts is not None:
            schedule = PrizeSchedule(season=season, **amounts)

    payouts = doc.get("payouts") or []
    if schedule is not None and payouts:
        weeks = (doc.get("source") or {}).get("regular_season_weeks") or 0
        expected = schedule.total(weeks)
        actual = sum(payout["amount"] for payout in payouts)
        if actual != expected:
            report.error(
                f"{path.name}: payouts total ${actual} but the recorded prize structure "
                f"totals ${expected}"
            )
        else:
            report.ok(f"{path.name}: {len(payouts)} payouts totalling ${actual}, as recorded")
        unassigned = [p["label"] for p in payouts if not p.get("winner_manager_id")]
        if unassigned:
            report.review(
                f"{path.name}: {len(unassigned)} prizes have no winner assigned "
                f"({', '.join(sorted(set(unassigned)))})"
            )
    elif payouts:
        report.skip(f"{path.name}: no prize amounts recorded for {season}; payouts unchecked")

    # REVIEW items the sync recorded are re-surfaced here rather than left in the file, so a
    # CI run never reports a season clean while unverified items sit inside it.
    for issue in ((doc.get("review") or {}).get("issues") or []):
        message = f"{path.name}: [{issue['code']}] {issue['message']}"
        if issue["severity"] == Severity.ERROR:
            report.error(message)
        else:
            report.review(message)
    for warning in ((doc.get("review") or {}).get("warnings") or []):
        report.review(f"{path.name}: {warning}")


def validate_manual(report: Report) -> None:
    """Check the hand-owned files parse and say what they claim to say."""
    prospects = DATA / "manual" / "prospects.json"
    if prospects.exists():
        try:
            seasons = _load(prospects).get("seasons") or {}
            for year, ids in seasons.items():
                int(year)
                if not all(isinstance(player_id, int) for player_id in ids):
                    report.error(
                        f"prospects.json: season {year} holds a non-integer player id — "
                        f"prospects are matched on espn_player_id, never on name"
                    )
            report.ok(f"prospects.json: {len(seasons)} seasons recorded")
        except (json.JSONDecodeError, ValueError) as exc:
            report.error(f"prospects.json: {exc}")
    else:
        report.skip("data/manual/prospects.json is missing — prospect keeps cannot be untaxed")

    payouts = DATA / "manual" / "payouts.json"
    if payouts.exists():
        try:
            seasons = _load(payouts).get("seasons") or {}
            for year, amounts in seasons.items():
                PrizeSchedule(season=int(year), **amounts)
            report.ok(f"payouts.json: {len(seasons)} seasons of prize amounts load cleanly")
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            report.error(f"payouts.json: {exc}")
    else:
        report.skip("data/manual/payouts.json is missing — no prize can be awarded")


def run(report: Report | None = None) -> Report:
    report = report or Report.empty()

    derived = _season_files(DATA / "derived")
    seasons = [path for path in derived if not path.stem.endswith("-stats")]
    stats_files = [path for path in derived if path.stem.endswith("-stats")]

    franchises_by_season: dict[str, set[str]] = {}
    roster_by_season: dict[str, list[RosterEntry]] = {}
    if not seasons:
        report.skip(
            "data/derived/ holds no season files — nothing to cross-check. The nightly "
            "Action writes them; locally, run `python -m rs57.sync --year <yr> --dry-run`"
        )
    for path in seasons:
        loaded = validate_derived_season(path, report)
        if loaded is None:
            continue
        franchises_by_season[loaded.season] = {
            franchise.manager_id for franchise in loaded.franchises
        }
        roster_by_season[loaded.season] = loaded.roster

    claims, overrides = validate_history(report)
    all_roster = [entry for roster in roster_by_season.values() for entry in roster]

    if claims and all_roster:
        validate_claims(claims, all_roster, overrides, "data/history/", report)
        for issue in check_base_continuity(all_roster, claims, overrides):
            report.review(f"base continuity: {issue.espn_player_id}: {issue.message}")
        report.ok("check_base_continuity ran against the recorded claims")
    elif all_roster:
        report.skip(
            "check_base_continuity: no prior-season keeper claims recorded, so this season's "
            "bases have nothing to be audited against — the ratchet is UNVERIFIED, not verified"
        )

    if overrides and all_roster:
        for issue in check_override_balance(all_roster, overrides):
            report.review(f"override balance: {issue.message}")
        report.ok("check_override_balance ran against the recorded overrides")
    else:
        report.skip(
            "check_override_balance: no salary overrides recorded, so no draft-cash trade "
            "could be checked for a missing leg"
        )

    for path in stats_files:
        season = path.stem.removesuffix("-stats")
        validate_stats_season(path, franchises_by_season.get(season, set()), report)

    validate_manual(report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate everything under data/.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail on REVIEW items too, not only on errors",
    )
    args = parser.parse_args(argv)

    report = run()

    for message in report.checked:
        print(f"  ok      {message}")
    for message in report.skipped:
        print(f"  SKIPPED {message}")
    for message in report.reviews:
        print(f"  REVIEW  {message}")
    for message in report.errors:
        print(f"  ERROR   {message}", file=sys.stderr)

    print(
        f"\n{len(report.checked)} checks passed, {len(report.skipped)} skipped, "
        f"{len(report.reviews)} awaiting review, {len(report.errors)} failed"
    )
    if report.reviews and not args.strict:
        # Loud on purpose. A REVIEW item is unverified, and a run that ends "0 failed" with
        # review items in it must not read as a clean bill of health.
        print(
            f"{len(report.reviews)} item(s) need the commissioner's eyes. They do not block, "
            f"and they have NOT been checked."
        )
    if report.errors:
        return 1
    return 1 if (args.strict and report.reviews) else 0


if __name__ == "__main__":
    raise SystemExit(main())
