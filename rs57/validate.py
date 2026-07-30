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
    PROSPECT_RULES_TIGHTENED,
    Severity,
    check_base_continuity,
    check_override_balance,
    validate_team_claims,
)
from rs57.models import (
    FranchiseName,
    KeeperClaim,
    KeeperSlot,
    Payment,
    Player,
    PrizeSchedule,
    RosterEntry,
    SalaryOverride,
    Season,
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
    base_field: str = ""
    """Which ESPN field ``base_salary`` came from. Load-bearing for the ratchet audit and for
    nothing else: ``keeperValue`` is the price each player carried INTO the season, which is
    what last season's claims are audited against, while ``keeperValueFuture`` is what this
    season's auction charged. Comparing the latter against last season's claims compares two
    numbers that are not supposed to be equal."""


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
        base_field=(doc.get("source") or {}).get("base_salary_field") or "",
    )


def validate_claims(
    claims: Sequence[KeeperClaim],
    eligible_roster: dict[int, list[RosterEntry]],
    label: str,
    report: Report,
    history: LoadedHistory | None = None,
    fees_waived: dict[int, str] | None = None,
) -> None:
    """Run the keeper engine's own validation over recorded claims, season by season.

    ``eligible_roster`` maps a claim's season to **the roster as it stood when keepers were
    declared**. For the season being decided that is the pre-draft derived file; for a frozen
    season it is the declaration roster reconstructed from ESPN's draft record.

    Neither adjacent roster snapshot will do, and both fail quietly. The previous season's ends
    before the offseason trades, so a keeper who changed hands reads as claimed by a manager
    who never had him — five did across 2024 and 2025. This season's own end-of-year roster has
    already lost every keeper dropped during it, Christian Kirk among them.

    A season with no such roster is reported as unchecked rather than checked against whatever
    snapshot happened to be nearest.
    """
    fees_waived = fees_waived or {}
    by_season: dict[int, dict[str, list[KeeperClaim]]] = {}
    for claim in claims:
        by_season.setdefault(claim.season, {}).setdefault(claim.manager_id, []).append(claim)

    checked = 0
    for season, by_manager in sorted(by_season.items()):
        roster = eligible_roster.get(season)
        if roster is None:
            report.skip(
                f"{label}: {season}'s claims were NOT validated against a roster — the "
                f"{season - 1} snapshot they were chosen from is not on disk, so player "
                f"membership, fee tiers and the prospect rules are all unchecked for that "
                f"season"
            )
            continue
        waived_by = (history.fees_waived.get(season) if history else None) or fees_waived.get(
            season
        )
        # A prospect must be a rookie, and a repeat claim is the only detectable form of that
        # — ESPN carries no rookie year. It is applied only from the season the rule tightened:
        # second-year prospects were legal before, and the record holds one.
        repeats: set[int] = set()
        if history and season >= PROSPECT_RULES_TIGHTENED:
            repeats = {
                claim.espn_player_id
                for claim in history.claims
                if claim.slot is KeeperSlot.PROSPECT and claim.season < season
            }
        for manager, team_claims in sorted(by_manager.items()):
            team_roster = [entry for entry in roster if entry.manager_id == manager]
            for issue in validate_team_claims(
                team_claims,
                team_roster,
                fees_waived=manager == waived_by,
                prior_prospect_ids=repeats,
            ):
                message = f"{label}: {season}: {manager}: [{issue.code}] {issue.message}"
                if issue.severity is Severity.ERROR:
                    report.error(message)
                else:
                    report.review(message)
        checked += 1

    if checked:
        report.ok(
            f"{label}: keeper claims validated for {checked} season(s), each against the "
            f"roster they were declared from"
        )


@dataclass
class LoadedHistory:
    """Everything the frozen seasons contribute."""

    claims: list[KeeperClaim]
    overrides: list[SalaryOverride]
    rosters: dict[int, list[RosterEntry]]
    """Season to the roster its keepers were DECLARED from, reconstructed from ESPN's draft
    record. What that season's own claims are checked against."""
    full_rosters: dict[int, list[RosterEntry]]
    """Season to its whole roster, priced at what each player cost in it."""
    carried_in: dict[int, list[RosterEntry]]
    """Season to its roster priced at what each player carried IN, which is the *previous*
    season's ``keeperValueFuture``. The half of the ratchet ``data/derived/`` cannot hold for
    a season that has already drafted, because ESPN overwrites ``keeperValue`` at the draft."""
    started_by_season: dict[int, set[int]]
    """Season to the players any league team started in it.

    Retained as league history. It was collected for a prospect rule requiring that a prospect
    had never been started, and **that rule is retired** (commissioner, 2026-07-30) — prospects
    may be started now, and the only remaining stipulation is that they are rookies. Nothing
    validates against this today."""
    fees_waived: dict[int, str]
    """Season to the manager whose fees were waived in it, as the previous season's
    consolation winner. Without it a waived team reads as a $5 fee shortfall."""

    @classmethod
    def empty(cls) -> LoadedHistory:
        return cls(
            claims=[],
            overrides=[],
            rosters={},
            full_rosters={},
            carried_in={},
            started_by_season={},
            fees_waived={},
        )

def validate_history(report: Report) -> LoadedHistory:
    """Load the frozen seasons from ``data/history/``, if any have been written.

    The shape is deliberately permissive. An empty directory is reported as a skipped check,
    because the audits below cannot run without it and a silent pass would be a lie.
    """
    loaded = LoadedHistory.empty()
    files = _season_files(DATA / "history")
    if not files:
        report.skip(
            "data/history/ is empty — no recorded keeper claims to audit, so "
            "check_base_continuity has nothing to compare this season's bases against"
        )
        return loaded

    for path in files:
        try:
            doc = _load(path)
            year = int(doc.get("season") or path.stem)
            loaded.claims += [KeeperClaim(**row) for row in doc.get("claims") or []]
            loaded.overrides += [
                SalaryOverride(**row) for row in doc.get("overrides") or []
            ]
            # The declaration roster is what a season's own claims are checked against; it
            # comes from ESPN's draft record rather than from a roster snapshot taken on some
            # other day. See backfill's ``roster_at_declaration``.
            declared = [RosterEntry(**row) for row in doc.get("roster_at_declaration") or []]
            if declared:
                loaded.rosters[year] = declared
            full = [RosterEntry(**row) for row in doc.get("roster") or []]
            if full:
                loaded.full_rosters[year] = full
            carried = [RosterEntry(**row) for row in doc.get("roster_carried_in") or []]
            if carried:
                loaded.carried_in[year] = carried
            started = doc.get("started_player_ids")
            if started:
                loaded.started_by_season[year] = {int(pid) for pid in started}
            if doc.get("fees_waived_manager_id"):
                loaded.fees_waived[year] = doc["fees_waived_manager_id"]
        except (ValidationError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            report.error(f"{path.name}: does not load into the models — {exc}")

    report.ok(
        f"data/history/: {len(loaded.claims)} claims, {len(loaded.overrides)} overrides, "
        f"{len(loaded.full_rosters)} rosters, {len(loaded.carried_in)} carried-in rosters "
        f"loaded"
    )
    if loaded.started_by_season:
        years = sorted(loaded.started_by_season)
        report.ok(
            f"data/history/: started-lineup history for {years[0]}-{years[-1]}, retained as "
            f"league history — no rule validates against it since the never-started prospect "
            f"rule was retired"
        )
    return loaded


def validate_manual_records(report: Report) -> tuple[list[KeeperClaim], list[SalaryOverride]]:
    """Load the claims and overrides the admin tool records in ``data/manual/``.

    ``history/`` holds a *completed* season, frozen; ``manual/`` holds the season being decided.
    Both are read, because the ratchet audit compares one against the other and does not care
    which file a claim came from — a claim recorded last August is what this August's base has
    to match.

    A file that does not exist is not an error. Neither existed before Phase 4.
    """
    claims: list[KeeperClaim] = []
    overrides: list[SalaryOverride] = []

    claims_path = DATA / "manual" / "claims.json"
    if claims_path.exists():
        try:
            for year, rows in (_load(claims_path).get("seasons") or {}).items():
                for row in rows:
                    claim = KeeperClaim(**row)
                    if claim.season != int(year):
                        report.error(
                            f"claims.json: a row under season {year} says season {claim.season}"
                        )
                    claims.append(claim)
            report.ok(f"claims.json: {len(claims)} keeper claims load cleanly")
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            report.error(f"claims.json: {exc}")
    else:
        report.skip(
            "data/manual/claims.json does not exist — no keeper claims have been recorded, so "
            "the ratchet has nothing to be audited against"
        )

    overrides_path = DATA / "manual" / "overrides.json"
    if overrides_path.exists():
        try:
            for rows in (_load(overrides_path).get("seasons") or {}).values():
                overrides += [SalaryOverride(**row) for row in rows]
            report.ok(f"overrides.json: {len(overrides)} salary overrides load cleanly")
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            report.error(f"overrides.json: {exc}")

    seasons_path = DATA / "manual" / "seasons.json"
    if seasons_path.exists():
        try:
            rows = (_load(seasons_path).get("seasons") or {}).values()
            recorded = [Season(**row) for row in rows]
            report.ok(f"seasons.json: {len(recorded)} seasons of settings load cleanly")
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            report.error(f"seasons.json: {exc}")

    payments_path = DATA / "manual" / "payments.json"
    if payments_path.exists():
        try:
            rows = _load(payments_path).get("payments") or []
            payments = [Payment(**row) for row in rows]
            report.ok(f"payments.json: {len(payments)} payment records load cleanly")
        except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
            report.error(f"payments.json: {exc}")

    return claims, overrides


def audit_ratchet(
    carried_in_by_season: dict[int, list[RosterEntry]],
    claims: Sequence[KeeperClaim],
    overrides: Sequence[SalaryOverride],
    report: Report,
    unusable: dict[int, str] | None = None,
    names: dict[int, str] | None = None,
) -> None:
    """Run ``check_base_continuity`` season by season, each against the season before it.

    **The pairing is the whole check.** A kept player's base this season must equal his computed
    salary *last* season, so 2026's roster is audited against 2025's claims. Handing the engine
    every claim at once would compare 2026's base against 2026's own recorded salary — a number
    against itself — and report a clean ratchet having verified nothing.

    **And the base has to be the carried-in one.** ``carried_in_by_season`` holds rosters priced
    at ESPN's ``keeperValue`` — what each player carried *into* the season. That is the only
    number last season's claims can be audited against. A season that has drafted reports
    ``keeperValueFuture`` instead, which is what its own auction charged, and charging is the
    thing being audited: comparing it against the previous season's claims would silently flag
    every keeper by exactly that season's own fee and tax. That is not a hypothetical — the
    moment this year's auction runs, ``data/derived/`` for the current season flips from one
    field to the other, and a pairing that took whatever roster was to hand would start
    reporting garbage on the day the draft happened. Seasons with no usable base are reported
    in ``unusable`` rather than quietly dropped.
    """
    by_season: dict[int, list[KeeperClaim]] = {}
    for claim in claims:
        by_season.setdefault(claim.season, []).append(claim)

    audited: list[str] = []
    for year, roster in sorted(carried_in_by_season.items()):
        prior = by_season.get(year - 1)
        if not prior:
            continue
        for issue in check_base_continuity(roster, prior, overrides):
            # Named, not just numbered. A finding the commissioner has to take to the league
            # chat is unusable as "player 4035687", and looking each one up by hand is exactly
            # the friction that gets a REVIEW list ignored.
            who = names.get(issue.espn_player_id) if names else None
            label = f"{who} ({issue.espn_player_id})" if who else f"player {issue.espn_player_id}"
            report.review(
                f"base continuity: {year}: {issue.manager_id}: {label}: {issue.message}"
            )
        audited.append(str(year))

    for year, why in sorted((unusable or {}).items()):
        if by_season.get(year - 1):
            report.skip(
                f"check_base_continuity: {year} has recorded claims for {year - 1} to be "
                f"audited against, but {why}, so that pairing is UNVERIFIED"
            )

    if audited:
        report.ok(
            f"check_base_continuity ran for {', '.join(audited)}, each against the previous "
            f"season's recorded claims"
        )
        return

    recorded = sorted(by_season)
    if recorded:
        report.skip(
            f"check_base_continuity: claims exist for {', '.join(str(y) for y in recorded)} but "
            f"no synced season follows one of them, so the ratchet is UNVERIFIED. It audits "
            f"next season's bases against this season's claims — it can only run once the "
            f"following season is synced."
        )
    else:
        report.skip(
            "check_base_continuity: no keeper claims recorded, so this season's bases have "
            "nothing to be audited against — the ratchet is UNVERIFIED, not verified"
        )


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


def check_carried_in_prices(history: LoadedHistory, report: Report) -> None:
    """A season's carried-in prices must equal the previous season's charged prices.

    Two frozen seasons hold the same number from opposite ends: ``{Y}.roster_carried_in`` is
    what each player brought *into* Y, and ``{Y-1}.roster`` is what he cost *in* Y-1. Under the
    ratchet those are the same figure, so any disagreement means one of them was read from the
    wrong ESPN field.

    Which is not hypothetical. ``keeperValue`` looks like the carried-in price and is, right up
    until that season drafts — at which point ESPN overwrites it with ``keeperValueFuture``.
    Building a completed season's carried-in roster from it yields that season's own auction
    result, every keeper then appears to have carried in exactly what he was charged, and the
    ratchet audit reports the whole league as off by precisely its own fees and taxes. Nothing
    raises. This check is the tripwire.
    """
    pairs = 0
    for year, carried in sorted(history.carried_in.items()):
        prior = history.full_rosters.get(year - 1) or []
        charged = {entry.espn_player_id: entry.base_salary for entry in prior}
        if not charged:
            continue
        pairs += 1
        for entry in carried:
            was = charged.get(entry.espn_player_id)
            if was is not None and was != entry.base_salary:
                report.error(
                    f"data/history/{year}.json: player {entry.espn_player_id} carried in "
                    f"${entry.base_salary} but {year - 1} recorded him at ${was} — one of the "
                    f"two was read from the wrong ESPN field"
                )
    if pairs:
        report.ok(
            f"carried-in prices agree with the previous season's charged prices across "
            f"{pairs} season pair(s)"
        )


def _recorded_waivers(report: Report) -> dict[int, str]:
    """Season to the manager whose keeper fees are waived in it.

    ``Season.consolation_winner_id`` names the winner of **that** season's consolation bracket
    and the waiver lands the year after, so this shifts by one. Read it off by one year and
    every waiver applies to the wrong team for a whole season.
    """
    path = DATA / "manual" / "seasons.json"
    if not path.exists():
        return {}
    waivers: dict[int, str] = {}
    try:
        for row in (_load(path).get("seasons") or {}).values():
            season = Season(**row)
            if season.consolation_winner_id:
                waivers[season.year + 1] = season.consolation_winner_id
    except (ValidationError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report.error(f"seasons.json: could not read the fee waivers — {exc}")
    return waivers


def validate_manual(report: Report) -> None:
    """Check the hand-owned files parse and say what they claim to say."""
    # ``data/manual/prospects.json`` is gone: prospect keeps are now derived from frozen
    # claims where ``slot == PROSPECT``. A leftover copy would be a second source for the
    # same fact, and the two would drift.
    stale = DATA / "manual" / "prospects.json"
    if stale.exists():
        report.error(
            "data/manual/prospects.json still exists, but prospect keeps are now derived from "
            "the frozen claims in data/history/. Two records of the same fact will disagree — "
            "delete it."
        )

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
    derived_carried_in: dict[int, list[RosterEntry]] = {}
    player_names: dict[int, str] = {}
    unusable: dict[int, str] = {}
    if not seasons:
        report.skip(
            "data/derived/ holds no season files — nothing to cross-check. The nightly "
            "Action writes them; locally, run `python -m rs57.sync --year <yr> --dry-run`"
        )
    for path in seasons:
        loaded = validate_derived_season(path, report)
        if loaded is None:
            continue
        player_names.update(
            {player.espn_player_id: player.name for player in loaded.players}
        )
        franchises_by_season[loaded.season] = {
            franchise.manager_id for franchise in loaded.franchises
        }
        roster_by_season[loaded.season] = loaded.roster
        try:
            year = int(loaded.season)
        except ValueError:
            continue
        # Only a season that has NOT drafted reports carried-in prices in its derived file.
        if loaded.base_field == "keeperValue":
            derived_carried_in[year] = loaded.roster
        else:
            unusable[year] = (
                f"data/derived/{year}.json holds {loaded.base_field or 'an unrecorded field'}, "
                f"which is what that season's own auction charged rather than what its keepers "
                f"carried in"
            )

    history = validate_history(report)
    manual_claims, manual_overrides = validate_manual_records(report)

    # A season that has graduated into data/history/ may still be sitting in
    # data/manual/claims.json — the importer cannot clear it, because that file has one writer
    # and the importer is not it. Frozen wins, and the leftovers are reported rather than
    # validated a second time: counting both would report every claim as a duplicate slot and
    # a duplicate player, which is a wall of errors describing a filing job.
    frozen_seasons = {claim.season for claim in history.claims}
    graduated = sorted({c.season for c in manual_claims} & frozen_seasons)
    if graduated:
        manual_claims = [c for c in manual_claims if c.season not in frozen_seasons]
        report.review(
            f"data/manual/claims.json still holds claims for "
            f"{', '.join(str(y) for y in graduated)}, which data/history/ has already frozen. "
            f"The frozen copy is authoritative and the manual rows were ignored — clear those "
            f"season(s) out through the admin tool so there is one record, not two."
        )
    claims = history.claims + manual_claims
    overrides = history.overrides + manual_overrides
    all_roster = [entry for roster in roster_by_season.values() for entry in roster]

    # A season's keepers are chosen from the roster as it stood at the END of the season
    # before, so season Y's roster is what year Y+1's claims are checked against. A pre-draft
    # derived file already holds last season's roster carried forward, which is the same pool
    # for the season now being decided.
    carried_in = {**derived_carried_in, **history.carried_in}
    eligible_roster = dict(history.rosters)
    eligible_roster.update({year: roster for year, roster in derived_carried_in.items()})
    unusable = {year: why for year, why in unusable.items() if year not in carried_in}

    # The waiver for the season being decided is recorded in seasons.json against the season
    # before — the consolation winner of year Y has their fees waived in Y+1. Frozen seasons
    # carry their own.
    manual_waivers = _recorded_waivers(report)

    if history.claims:
        validate_claims(history.claims, eligible_roster, "data/history/", report, history)
    if manual_claims:
        validate_claims(
            manual_claims,
            eligible_roster,
            "data/manual/",
            report,
            history,
            fees_waived=manual_waivers,
        )

    # Called even with nothing to audit against: a ratchet that cannot run has to SAY it did
    # not run. Guarding this on having data is how the check goes quiet exactly when it is
    # least verified.
    check_carried_in_prices(history, report)

    if all_roster or claims:
        audit_ratchet(carried_in, claims, overrides, report, unusable, player_names)

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
