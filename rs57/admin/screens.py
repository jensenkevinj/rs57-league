"""Turn engine output into screen context. No arithmetic on money lives past this module.

``compute_team_keepers`` prices a team **and** validates it in one pass, so it *is* the claim
screen: post the form, call the function, re-render the fragment. Nothing here recomputes a
salary, a fee tier or a tax — those come from ``keeper_rules`` and are handed to the templates
already computed, the same arrangement ``rs57.site`` uses. The templates loop and format.

The other half of this module's job is saying what has **not** been checked. Three keeper rules
cannot be verified from anything in ``data/``:

* prospect rule 2 (never started by any league team) needs box-score history — Phase 5. The
  engine already reports ``PROSPECT_START_HISTORY_UNVERIFIED`` for it.
* prospect rule 1 (at most one NFL season) needs seasons-played data that no file here holds.
  The engine's check silently passes when the mapping is empty, so this module adds the note.
* the fee waiver needs a recorded consolation winner. Until one exists, fees are priced in
  full and the screen says the waiver is unconfirmed.

A prospect screen that looks clean is lying. Every one of those renders as unverified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rs57.admin.derived import DerivedSeason
from rs57.admin.store import ManualStore
from rs57.keeper_rules import (
    KEEPER_TAX,
    MAX_KEEPERS,
    MAX_PROSPECTS,
    Severity,
    ValidationIssue,
    compute_team_keepers,
    effective_base_salary,
    fee_total_for,
    is_keeper_slot,
    keeper_salary,
)
from rs57.models import KeeperClaim, KeeperSlot, SalaryOverride

SLOT_CHOICES = (KeeperSlot.K1, KeeperSlot.K2, KeeperSlot.K3, KeeperSlot.PROSPECT)


@dataclass(frozen=True)
class Note:
    """Something on screen that nobody has verified.

    ``kind`` is ``"error"``, ``"review"`` or ``"info"``. The first two render as unverified and
    never as though they had passed.

    ``team_specific`` is False for a fact about the whole league — an unrecorded consolation
    winner, a sync warning about the season. Those belong on a team's own screen, because that is
    where the number they affect is being read, but the league report counts them once at the
    bottom rather than per team: twelve identical flags is how a real one stops being read.
    """

    kind: str
    message: str
    team_specific: bool = True


@dataclass(frozen=True)
class PlayerRow:
    """One rostered player, and what keeping him would cost or does cost."""

    espn_player_id: int
    name: str
    position: str
    nfl_team: str
    base: int
    tax: int
    candidate_price: int
    """What he would cost in a keeper slot with no fee allocated — ``base + tax``."""
    kept_prior_year: bool
    source: str
    acquired_at: datetime
    slot: str
    """The claimed slot, or ``""`` for a player nobody has declared."""
    fee: int
    salary: int | None
    """The engine's price for this claim, or ``None`` when he is not claimed."""
    overridden: bool
    espn_base: int
    """ESPN's own figure, which differs from ``base`` only when an override is live."""

    @property
    def claimed(self) -> bool:
        return bool(self.slot)


@dataclass(frozen=True)
class TeamScreen:
    """One team's claim screen: priced rows, every issue, and what was not checked."""

    season: int
    prior_season: int
    """Passed in rather than computed in a template — years are arithmetic too."""
    manager_id: str
    name: str
    rows: tuple[PlayerRow, ...]
    issues: tuple[ValidationIssue, ...]
    notes: tuple[Note, ...]
    total_salary: int
    total_fees: int
    fee_expected: int | None
    """What the tier says this many keepers owes, or ``None`` above the legal maximum."""
    fee_shortfall: int | None
    keeper_count: int
    prospect_count: int
    blocked: bool
    fees_waived: bool
    waiver_recorded: bool
    submitted_at: datetime | None
    saved: bool = False

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def reviews(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.REVIEW)

    @property
    def unverified(self) -> tuple[Note, ...]:
        return tuple(note for note in self.notes if note.kind in ("review", "error"))


@dataclass(frozen=True)
class TeamSummary:
    manager_id: str
    name: str
    keeper_count: int
    prospect_count: int
    total_salary: int
    total_fees: int
    blocked: bool
    error_count: int
    review_count: int
    declared: bool
    fees_waived: bool


@dataclass(frozen=True)
class SeasonScreen:
    """Every team at once — the report the commissioner reads before releasing anything."""

    season: int
    prior_season: int
    teams: tuple[TeamSummary, ...]
    notes: tuple[Note, ...]
    league_salary: int
    league_fees: int
    declared_count: int
    blocked_count: int
    keeper_deadline: datetime | None
    deadline_passed: bool
    waiver_manager_id: str | None
    waiver_name: str | None
    waiver_recorded: bool


def _prospect_notes(claims: list[KeeperClaim], deadline: datetime | None, prior: int) -> list[Note]:
    """What a prospect claim cannot have checked, stated once per screen.

    Only emitted when a prospect is actually claimed — a note about prospect rules on a screen
    with no prospect on it is noise, and noise is how the real ones stop being read.
    """
    if not any(claim.slot is KeeperSlot.PROSPECT for claim in claims):
        return []
    notes = [
        Note(
            "review",
            "Prospect rule 1 (at most one NFL season) is NOT checked: nothing in data/ records "
            "how many NFL seasons a player has played, so the engine's check has no data to "
            "fail on. Confirm it by eye.",
        )
    ]
    if deadline is None:
        notes.append(
            Note(
                "review",
                f"Prospect rule 3 (rostered before the trade deadline) is NOT checked: "
                f"data/derived/{prior}.json is missing, so {prior}'s trade deadline is "
                f"unknown. Sync the prior season to check it.",
            )
        )
    else:
        notes.append(
            Note(
                "info",
                f"Prospect rule 3 is checked against {prior}'s trade deadline "
                f"({deadline:%Y-%m-%d}) — the deadline of the season he was rostered through, "
                f"not the coming season's.",
            )
        )
    return notes


def build_team_screen(
    season: int,
    manager_id: str,
    current: DerivedSeason,
    prior: DerivedSeason | None,
    store: ManualStore,
    *,
    claims: list[KeeperClaim] | None = None,
    saved: bool = False,
) -> TeamScreen:
    """Price and validate one team.

    ``claims`` overrides what is on file, which is what makes the live preview live: the form
    posts, the claims are built from it, and this runs against them without writing anything.

    ``prior`` is the **previous** season's derived file, and it is here for one reason: a
    prospect must have been rostered before the trade deadline of the season he was rostered
    through. Passing the coming season's own deadline would compare a 2025 acquisition against
    a deadline in December 2026 and pass every prospect that has ever existed.
    """
    stored = [claim for claim in store.claims(season) if claim.manager_id == manager_id]
    active = stored if claims is None else claims

    roster = current.roster_for(manager_id)
    overrides = store.overrides(season)
    waived_manager, waiver_recorded = store.fees_waived_for(season)
    fees_waived = waiver_recorded and waived_manager == manager_id
    prospect_ids, used_legacy = store.prior_prospect_ids(season)
    deadline = prior.trade_deadline if prior else None

    result = compute_team_keepers(
        active,
        roster,
        overrides,
        manager_id=manager_id,
        fees_waived=fees_waived,
        trade_deadline=deadline,
        prior_prospect_ids=prospect_ids,
    )

    priced = {keeper.espn_player_id: keeper for keeper in result.keepers}
    by_player = {claim.espn_player_id: claim for claim in active}
    players = current.player_by_id

    rows: list[PlayerRow] = []
    for entry in sorted(roster, key=lambda e: e.espn_player_id):
        player = players.get(entry.espn_player_id)
        if player is None:
            # validate.py reports this against the derived file. Skipping keeps a bare player
            # id off the screen rather than offering a nameless row to declare.
            continue
        base = effective_base_salary(entry, overrides)
        candidate = keeper_salary(base, 0, entry.kept_prior_year, KeeperSlot.K1)
        claim = by_player.get(entry.espn_player_id)
        keeper = priced.get(entry.espn_player_id)
        rows.append(
            PlayerRow(
                espn_player_id=entry.espn_player_id,
                name=player.name,
                position=str(player.position),
                nfl_team=player.nfl_team,
                base=base,
                tax=candidate - base,
                candidate_price=candidate,
                kept_prior_year=entry.kept_prior_year,
                source=str(entry.source),
                acquired_at=entry.acquired_at,
                slot=str(claim.slot) if claim else "",
                fee=claim.fee_allocated if claim else 0,
                salary=keeper.salary if keeper else None,
                overridden=base != entry.base_salary,
                espn_base=entry.base_salary,
            )
        )

    rows.sort(key=lambda row: (not row.claimed, row.slot, -row.candidate_price, row.name))

    keepers = [claim for claim in active if is_keeper_slot(claim.slot)]
    prospects = [claim for claim in active if claim.slot is KeeperSlot.PROSPECT]
    expected = fee_total_for(len(keepers), fees_waived) if len(keepers) <= MAX_KEEPERS else None
    shortfall = None if expected is None else expected - result.total_fees

    notes: list[Note] = []
    if not waiver_recorded:
        notes.append(
            Note(
                "review",
                f"Fees are priced IN FULL because {season - 1} has no settings row, so nobody "
                f"is recorded as {season - 1}'s consolation winner. If a team has its fees "
                f"waived this year, record it in season settings for {season - 1} — the waiver "
                f"belongs to the year AFTER the bracket was won.",
                team_specific=False,
            )
        )
    elif fees_waived:
        notes.append(
            Note(
                "info",
                f"Fees waived: this team won {season - 1}'s consolation bracket. Salaries are "
                f"still owed in full — only the fee on top is waived.",
            )
        )
    notes += _prospect_notes(active, deadline, season - 1)

    for claim in stored:
        keeper = priced.get(claim.espn_player_id)
        if claim.computed_salary is None or keeper is None:
            continue
        if claim.computed_salary != keeper.salary:
            player = players.get(claim.espn_player_id)
            notes.append(
                Note(
                    "review",
                    f"{player.name if player else claim.espn_player_id} was recorded at "
                    f"${claim.computed_salary} but now prices at ${keeper.salary}. The recorded "
                    f"figure is what the manager was told they owed and has NOT been "
                    f"overwritten — re-submit to accept the new number, or record an override "
                    f"if ESPN's base moved for a reason.",
                )
            )

    if used_legacy:
        notes.append(
            Note(
                "info",
                "Some prior prospect keeps come from data/manual/prospects.json rather than "
                "recorded claims, because no claims exist for completed seasons yet.",
                team_specific=False,
            )
        )
    for warning in current.warnings:
        notes.append(Note("review", f"{season} sync: {warning}", team_specific=False))
    mismatched = [
        row.name for row in rows if row.espn_player_id in current.waiver_base_mismatches
    ]
    if mismatched:
        notes.append(
            Note(
                "review",
                f"ESPN and the transaction log disagree about the waiver price of "
                f"{', '.join(sorted(mismatched))}, so the base above is unconfirmed.",
            )
        )

    submitted = [claim.submitted_at for claim in stored if claim.submitted_at]

    return TeamScreen(
        season=season,
        prior_season=season - 1,
        manager_id=manager_id,
        name=current.name_of(manager_id),
        rows=tuple(rows),
        issues=result.issues,
        notes=tuple(notes),
        total_salary=result.total_salary,
        total_fees=result.total_fees,
        fee_expected=expected,
        fee_shortfall=shortfall,
        keeper_count=len(keepers),
        prospect_count=len(prospects),
        blocked=result.blocked,
        fees_waived=fees_waived,
        waiver_recorded=waiver_recorded,
        submitted_at=max(submitted) if submitted else None,
        saved=saved,
    )


def build_season_screen(
    season: int,
    current: DerivedSeason,
    prior: DerivedSeason | None,
    store: ManualStore,
    *,
    now: datetime,
) -> SeasonScreen:
    """Every team's standing at a glance. Totals are added here, never in a template."""
    summaries: list[TeamSummary] = []
    waived_manager, waiver_recorded = store.fees_waived_for(season)

    for manager_id in current.manager_ids:
        screen = build_team_screen(season, manager_id, current, prior, store)
        summaries.append(
            TeamSummary(
                manager_id=manager_id,
                name=screen.name,
                keeper_count=screen.keeper_count,
                prospect_count=screen.prospect_count,
                total_salary=screen.total_salary,
                total_fees=screen.total_fees,
                blocked=screen.blocked,
                error_count=len(screen.errors),
                review_count=len(screen.reviews)
                + len([n for n in screen.unverified if n.team_specific]),
                declared=bool(screen.keeper_count or screen.prospect_count),
                fees_waived=screen.fees_waived,
            )
        )

    settings = store.season(season)
    deadline = settings.keeper_deadline if settings else None

    notes: list[Note] = []
    undeclared = [s for s in summaries if not s.declared]
    if undeclared:
        notes.append(
            Note(
                "info",
                f"{len(undeclared)} of {len(summaries)} teams have declared nothing yet: "
                f"{', '.join(s.name for s in undeclared)}.",
            )
        )
    if deadline is None:
        notes.append(
            Note(
                "review",
                f"No keeper deadline is recorded for {season}. ESPN keeps one in "
                f"draftSettings.keeperDeadlineDate — record it in season settings.",
            )
        )
    elif deadline < now:
        notes.append(
            Note(
                "review",
                f"The keeper deadline ({deadline:%Y-%m-%d %H:%M}) has passed. Claims are still "
                f"editable — the deadline is recorded and shown, never enforced — so anything "
                f"changed after it is a deliberate correction.",
            )
        )
    if not waiver_recorded:
        notes.append(
            Note(
                "review",
                f"Every team above is priced with fees in full: {season - 1} has no settings "
                f"row recording who won its consolation bracket.",
            )
        )

    return SeasonScreen(
        season=season,
        prior_season=season - 1,
        teams=tuple(summaries),
        notes=tuple(notes),
        league_salary=sum(s.total_salary for s in summaries),
        league_fees=sum(s.total_fees for s in summaries),
        declared_count=sum(1 for s in summaries if s.declared),
        blocked_count=sum(1 for s in summaries if s.blocked),
        keeper_deadline=deadline,
        deadline_passed=bool(deadline and deadline < now),
        waiver_manager_id=waived_manager,
        waiver_name=current.name_of(waived_manager) if waived_manager else None,
        waiver_recorded=waiver_recorded,
    )


def claims_from_form(
    season: int,
    manager_id: str,
    form: dict[str, str],
    *,
    now: datetime | None = None,
    price_with: tuple[DerivedSeason, ManualStore] | None = None,
) -> tuple[list[KeeperClaim], list[str]]:
    """Build claims out of a posted form, and report what could not be parsed.

    A fee of ``-5`` parses fine and comes back as a ``NEGATIVE_FEE`` issue from the engine.
    That is deliberate: the input carries no ``min`` attribute, because a browser refusing the
    value would hide a rule violation behind a form error and teach nobody anything. A fee of
    ``abc`` is a different thing — it is not a number at all — and is reported here.

    ``price_with`` freezes ``computed_salary`` at submission time. It is optional so the live
    preview can build claims without pretending to record anything.
    """
    claims: list[KeeperClaim] = []
    problems: list[str] = []

    for key, raw in sorted(form.items()):
        if not key.startswith("slot_"):
            continue
        slot_value = (raw or "").strip()
        if not slot_value:
            continue
        try:
            player_id = int(key.removeprefix("slot_"))
        except ValueError:
            problems.append(f"{key} is not a player id")
            continue
        if slot_value not in {str(slot) for slot in SLOT_CHOICES}:
            problems.append(f"{slot_value} is not a keeper slot")
            continue

        raw_fee = (form.get(f"fee_{player_id}") or "").strip()
        try:
            fee = int(raw_fee) if raw_fee else 0
        except ValueError:
            problems.append(f"fee for player {player_id} is {raw_fee!r}, which is not a whole number")
            continue

        claims.append(
            KeeperClaim(
                season=season,
                manager_id=manager_id,
                espn_player_id=player_id,
                slot=KeeperSlot(slot_value),
                fee_allocated=fee,
                submitted_at=now,
            )
        )

    if price_with is not None:
        current, store = price_with
        claims = _freeze_salaries(season, manager_id, claims, current, store)
    return claims, problems


def _freeze_salaries(
    season: int,
    manager_id: str,
    claims: list[KeeperClaim],
    current: DerivedSeason,
    store: ManualStore,
) -> list[KeeperClaim]:
    """Stamp each claim with the salary the engine prices it at right now.

    Frozen rather than recomputed on read: ``computed_salary`` is the record of what the
    manager was told they owed, and it is what ``check_base_continuity`` compares next season's
    ESPN base against a year later. Recomputing it on every read would make that audit compare
    a number against itself.
    """
    waived_manager, waiver_recorded = store.fees_waived_for(season)
    result = compute_team_keepers(
        claims,
        current.roster_for(manager_id),
        store.overrides(season),
        manager_id=manager_id,
        fees_waived=waiver_recorded and waived_manager == manager_id,
    )
    priced = {keeper.espn_player_id: keeper.salary for keeper in result.keepers}
    return [
        claim.model_copy(update={"computed_salary": priced.get(claim.espn_player_id)})
        for claim in claims
    ]


def override_form(
    form: dict[str, str], *, now: datetime
) -> tuple[SalaryOverride | None, list[str]]:
    """Build a ``SalaryOverride`` from the posted form.

    ``reason`` is required and free text. It is the injection path — typed here, stored in
    ``data/manual/``, committed to a public repo and rendered on a public site — so it is
    stored exactly as typed and escaped at render time. Never sanitised on the way in: a
    stripped-down ``reason`` would be a silently altered record.
    """
    problems: list[str] = []
    try:
        player_id = int((form.get("espn_player_id") or "").strip())
    except ValueError:
        problems.append("player id must be a number")
        player_id = 0
    try:
        season = int((form.get("season") or "").strip())
    except ValueError:
        problems.append("season must be a year")
        season = 0
    try:
        actual = int((form.get("actual_salary") or "").strip())
    except ValueError:
        problems.append("actual salary must be a whole number of dollars")
        actual = -1
    if actual < 0:
        problems.append("actual salary cannot be negative")
    reason = (form.get("reason") or "").strip()
    if not reason:
        problems.append("a reason is required — an override with no reason is unauditable")

    if problems:
        return None, problems
    return (
        SalaryOverride(
            espn_player_id=player_id,
            season=season,
            actual_salary=actual,
            reason=reason,
            created_at=now,
            reverted=False,
            unpaired_ok=bool(form.get("unpaired_ok")),
        ),
        [],
    )


FEE_HELP = (
    f"The tier is the team's total: 1 keeper owes $0, 2 owe $5, 3 owe $15. The split across "
    f"them is the manager's own choice, so nothing is filled in for them. A keeper kept last "
    f"season also owes the ${KEEPER_TAX} tax, which the engine adds — it is not part of the fee."
)

LIMITS_HELP = f"Max {MAX_KEEPERS} keepers plus {MAX_PROSPECTS} prospect. There is no salary cap."
