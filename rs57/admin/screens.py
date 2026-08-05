"""Turn engine output into screen context. No arithmetic on money lives past this module.

``compute_team_keepers`` prices a team **and** validates it in one pass, so it *is* the claim
screen: post the form, call the function, re-render the fragment. Nothing here recomputes a
salary, a fee tier or a tax — those come from ``keeper_rules`` and are handed to the templates
already computed, the same arrangement ``rs57.site`` uses. The templates loop and format.

The other half of this module's job is saying what has **not** been checked.

* prospect rule 1 — **a prospect must be a rookie** — *is* checked now, against ESPN's draft
  class from ``data/derived/player-origins.json``. It is REVIEW rather than ERROR by decision
  (commissioner, 2026-08-01): the draft class comes from outside the league, so this screen
  flags an ineligible prospect and **still saves him**, leaving the final call to a human who
  can overrule ESPN or record a voted exception. ``PROSPECT_REPEAT_CLAIM`` remains ERROR and
  blocks — the league's own record blocks, an outside data source flags.
* what cannot be checked is a player ESPN has **no** draft class or debut year for. That is a
  per-claim note naming him, not a blanket one: a blanket "rule 1 is unchecked" would go on
  shouting over prospects that had in fact been verified, and that is how a real warning stops
  being read.
* the fee waiver needs a recorded consolation winner. Until one exists, fees are priced in
  full and the screen says the waiver is unconfirmed.

A prospect screen that looks clean had better have checked something. Everything unverified
renders as unverified.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from rs57.admin.derived import DerivedSeason
from rs57.admin.store import ManualStore
from rs57.keeper_rules import (
    KEEPER_TAX,
    MAX_KEEPERS,
    MAX_PROSPECTS,
    PROSPECT_RULES_TIGHTENED,
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

SLOT_LABELS = {"K1": "K1", "K2": "K2", "K3": "K3", "PROSPECT": "P"}
"""Short labels for the card, which is narrow. Display only — the stored slot is unchanged."""

ROSTER_IS_KEEPERS_ONLY = MAX_KEEPERS + MAX_PROSPECTS
"""At or under this many rostered players, ESPN has pruned the team to its keepers.

The same threshold ``espn.check_roster_sizes`` reads, for the same reason: it is a fact the
payload announces rather than a mode anybody switches on. Once ESPN prunes, who was kept stops
being a question — so the card stops asking it and shows names instead of pickers.
"""


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
class OverrideRow:
    """One salary override, joined to the player and franchise it moved money between."""

    override: SalaryOverride
    name: str
    espn_base: int | None
    manager: str | None

    @property
    def live(self) -> bool:
        return not self.override.reverted

    @property
    def delta(self) -> int | None:
        """True salary minus what ESPN holds — the leg of the cash trade, in dollars.

        ``None`` when ESPN has no base for the player, because an unknowable figure is left
        out rather than guessed at zero. A guessed zero would sum into a league total that
        looked balanced while resting on a number nobody had.
        """
        if self.espn_base is None:
            return None
        return self.override.actual_salary - self.espn_base


def override_row(override: SalaryOverride, season: DerivedSeason | None) -> OverrideRow:
    """Join one override to its season's derived roster. Matched on id, never on name."""
    player = season.player_by_id.get(override.espn_player_id) if season else None
    entry = next(
        (
            e
            for e in (season.roster if season else ())
            if e.espn_player_id == override.espn_player_id
        ),
        None,
    )
    return OverrideRow(
        override=override,
        name=player.name if player else f"player {override.espn_player_id}",
        espn_base=entry.base_salary if entry else None,
        manager=season.name_of(entry.manager_id) if season and entry else None,
    )


@dataclass(frozen=True)
class KeeperGate:
    """Whether claims may be edited right now, and why.

    **Three states, not two.** ``deadline`` is ``None`` for two entirely different reasons —
    nobody has recorded one yet, or... no, only one reason, and that is the point. A future
    deadline and an unrecorded deadline both used to collapse into ``deadline_passed = False``,
    so gating on that single flag would freeze a freshly synced season solid, with nothing on
    screen saying why and the way out — the settings page — not obviously connected. The
    deadline is recorded in this same tool; a missing one cannot be the thing that locks it.

    ``state`` is ``"locked"``, ``"open"`` or ``"unrecorded"``. Only ``"locked"`` refuses a write.

    This object is presentation *and* an answer, but it is never the guard. The ``save`` route
    computes its own gate from the store and the clock rather than trusting whatever a screen
    was handed — a ``disabled`` attribute is a courtesy to the browser, and a tab left open
    from before the deadline still posts.
    """

    deadline: datetime | None
    state: str

    @property
    def editable(self) -> bool:
        return self.state != "locked"

    @property
    def message(self) -> str:
        if self.state == "locked":
            return (
                f"Claims are locked until the keeper deadline "
                f"({self.deadline:%Y-%m-%d %H:%M}). Salaries are entered after it passes, so "
                f"nothing is recorded while managers can still change their minds. Prices "
                f"below are live — you can look, you just cannot record."
            )
        if self.state == "unrecorded":
            return (
                "No keeper deadline is recorded for this season, so nothing is locked. "
                "ESPN keeps one in draftSettings.keeperDeadlineDate — record it in season "
                "settings and the console will hold claims until it passes."
            )
        return (
            f"The keeper deadline ({self.deadline:%Y-%m-%d %H:%M}) has passed. The console is "
            f"open and stays open — nothing re-locks."
        )


def keeper_gate(season: int, store: ManualStore, *, now: datetime) -> KeeperGate:
    """Read the gate off the recorded deadline. The one place the three states are decided."""
    settings = store.season(season)
    deadline = settings.keeper_deadline if settings else None
    if deadline is None:
        return KeeperGate(deadline=None, state="unrecorded")
    return KeeperGate(deadline=deadline, state="open" if deadline < now else "locked")


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
    gate: KeeperGate | None = None
    """``None`` renders as editable. Safe as a default because the ``save`` route computes its
    own gate and never reads this one — a screen cannot talk a route into a write."""

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.ERROR)

    @property
    def reviews(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity is Severity.REVIEW)

    @property
    def unverified(self) -> tuple[Note, ...]:
        return tuple(note for note in self.notes if note.kind in ("review", "error"))

    @property
    def fee_gap(self) -> int:
        """How far the allocated fees are from the tier, unsigned. ``fee_state`` says which way.

        The card used to print the tier as a second total next to the allocated one and leave
        the reader to subtract. This is the number that subtraction was for.
        """
        return abs(self.fee_shortfall or 0)

    @property
    def fee_state(self) -> str:
        """``"met"``, ``"short"``, ``"over"``, ``"none"`` or ``"unknown"``.

        Named in Python rather than inferred in a template. ``fee_shortfall`` is signed —
        positive owes more, negative has over-allocated — and a template testing it for
        truthiness calls both "short", which tells a manager who paid $5 too much to pay more.
        """
        if self.fee_expected is None:
            return "unknown"
        if not self.declared:
            return "none"
        if self.fee_shortfall == 0:
            return "met"
        return "short" if (self.fee_shortfall or 0) > 0 else "over"

    @property
    def keepers_only(self) -> bool:
        """True once ESPN has pruned this roster to the players who were kept.

        Read off the roster's depth, not off a setting. Between the keeper deadline and the
        auction ESPN holds only the kept players, and at that point *who* is kept is not a
        question anybody needs a picker to answer.
        """
        return 0 < len(self.rows) <= ROSTER_IS_KEEPERS_ONLY

    @property
    def slots(self) -> tuple[tuple[str, str, PlayerRow | None], ...]:
        """The team's four keeper slots in order: ``(slot, short label, claim or None)``.

        Always four rows, even when nothing is declared — an empty slot is a fact worth
        rendering rather than a row that isn't there, and a card whose height depends on how
        many keepers a team has makes a grid of twelve impossible to scan.

        **Once the roster is pruned, unclaimed slots are pre-filled from it.** ESPN has already
        said these are the keepers; making somebody re-pick them from a list of exactly
        themselves is asking a question that has one answer. The fill is a *default on screen*,
        not a record — nothing reaches ``claims.json`` until the card is submitted.

        K1/K2/K3 are interchangeable, so the fill runs dearest first. Only the keeper/prospect
        split is load-bearing anywhere (``is_keeper_slot``, ``prior_prospect_ids``), and **ESPN
        cannot say which keep is the prospect** — so the prospect slot is never filled by
        guessing. It stays empty and ``prospect_unknown`` says so.

        A slot claimed twice keeps the first. That is a rule violation the engine reports as an
        issue and the card shows as an error tag; the card is not the place to adjudicate it.
        """
        claimed: dict[str, PlayerRow] = {}
        for row in self.rows:
            if row.claimed and row.slot not in claimed:
                claimed[row.slot] = row

        fill: list[PlayerRow] = []
        if self.keepers_only and not claimed:
            fill = [row for row in self.pickable][:MAX_KEEPERS]

        filled = iter(fill)
        out: list[tuple[str, str, PlayerRow | None]] = []
        for slot in SLOT_CHOICES:
            name = str(slot)
            row = claimed.get(name)
            if row is None and is_keeper_slot(slot):
                row = next(filled, None)
            out.append((name, SLOT_LABELS[name], row))
        return tuple(out)

    @property
    def prospect_unknown(self) -> bool:
        """A pruned roster deeper than the keeper limit must hold a prospect, and ESPN won't say.

        Three keepers is the maximum, so a team ESPN pruned to four has exactly one prospect
        among them — a fact forced by the rules. *Which* one is not derivable from anything
        ESPN publishes, so it is left for the commissioner rather than guessed.
        """
        return self.keepers_only and len(self.rows) > MAX_KEEPERS and not self.prospect_count

    @property
    def pickable(self) -> tuple[PlayerRow, ...]:
        """Everyone on the roster, dearest first — the options behind each slot picker.

        Ordered by what he would cost rather than alphabetically: a manager keeps his expensive
        players, so the ones being picked cluster at the top of the list.
        """
        return tuple(sorted(self.rows, key=lambda row: (-row.candidate_price, row.name)))

    @property
    def declared(self) -> bool:
        return bool(self.keeper_count or self.prospect_count)

    @property
    def error_count(self) -> int:
        return len(self.errors)

    @property
    def review_count(self) -> int:
        """Everything on this team needing the commissioner's eyes.

        League-wide notes are excluded — an unrecorded consolation winner is one fact about the
        season, and counting it against all twelve teams is how a real flag stops being read.
        The season report shows those once, at the bottom.

        A property rather than a number copied into a summary object: the report used to carry
        its own count, and a count computed twice is a count that can disagree with itself.
        """
        return len(self.reviews) + len([n for n in self.unverified if n.team_specific])


@dataclass(frozen=True)
class SeasonScreen:
    """Every team at once — the report the commissioner reads before releasing anything."""

    season: int
    prior_season: int
    teams: tuple[TeamScreen, ...]
    """Full screens, not summaries. This page *is* the entry form for all twelve franchises, so
    it needs every priced row — and a summary object alongside them would be a second place for
    the same numbers to live."""
    notes: tuple[Note, ...]
    league_salary: int
    league_fees: int
    declared_count: int
    blocked_count: int
    gate: KeeperGate
    waiver_manager_id: str | None
    waiver_name: str | None
    waiver_recorded: bool
    overrides: tuple[OverrideRow, ...]
    """This season's draft-cash trades, recorded in the same sitting as the fees."""
    override_net: int | None
    """Live overrides summed. Should be zero — a cash trade moves money between two teams, and
    ``check_override_balance`` reports it when it does not. ``None`` when any live row has no
    ESPN base to compare against, because a total resting on a guess is worse than no total."""


def _prospect_notes(
    season: int,
    claims: list[KeeperClaim],
    deadline: datetime | None,
    prior: int,
    first_nfl_season: Mapping[int, int] | None,
    player_names: Mapping[int, str] | None = None,
) -> list[Note]:
    """What a prospect claim was and was not checked against, stated per claim.

    Only emitted when a prospect is actually claimed — a note about prospect rules on a screen
    with no prospect on it is noise, and noise is how the real ones stop being read.

    Rule 1 used to be a single blanket REVIEW saying it could not be checked at all. It can
    now, from ESPN's draft class, so the note is **per claim** instead: a screen with a
    verified prospect says so, and one with an unresolved player says which player and why.
    A blanket note would go on saying "unchecked" over a prospect that had in fact been
    checked, and that is how a real warning stops being read.
    """
    prospects = [claim for claim in claims if claim.slot is KeeperSlot.PROSPECT]
    if not prospects:
        return []
    names = player_names or {}
    notes: list[Note] = []

    for claim in prospects:
        who = names.get(claim.espn_player_id, f"player {claim.espn_player_id}")
        began = (first_nfl_season or {}).get(claim.espn_player_id)
        if first_nfl_season is None:
            notes.append(
                Note(
                    "review",
                    f"Prospect rule 1 (must be a rookie) is NOT checked for {who}: no draft "
                    f"classes are loaded. Run `python -m rs57.origins_sync` and reload.",
                )
            )
        elif began is None:
            notes.append(
                Note(
                    "review",
                    f"Prospect rule 1 (must be a rookie) is NOT checked for {who}: ESPN "
                    f"carries neither a draft class nor a debut year for him. Confirm by eye.",
                )
            )
        elif season - began == 1:
            notes.append(
                Note(
                    "info",
                    f"Prospect rule 1 is checked: {who}'s first NFL season was {began} "
                    f"(ESPN draft class), so he was a rookie in {prior}.",
                )
            )
        # The ineligible case is an engine issue, not a note — it renders with the claim.

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
    first_nfl_season: Mapping[int, int] | None = None,
    gate: KeeperGate | None = None,
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

    # Both forms of the rookie rule are gated on the season, EXPLICITLY. The repeat check used
    # to be passed unconditionally and was correct only because nobody opens a pre-2026 season
    # in this tool — an implicit gate, and the first time someone did open 2025 it would have
    # flagged Tyjae Spears' legal claim as an error.
    governed = season >= PROSPECT_RULES_TIGHTENED
    origins = (first_nfl_season or {}) if governed else None
    if not governed:
        prospect_ids = set()

    result = compute_team_keepers(
        active,
        roster,
        overrides,
        manager_id=manager_id,
        fees_waived=fees_waived,
        first_nfl_season=origins,
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
    if len(rows) > MAX_KEEPERS and 0 < len(rows) <= ROSTER_IS_KEEPERS_ONLY and not prospects:
        notes.append(
            Note(
                "review",
                f"ESPN has pruned this roster to {len(rows)} kept players, and {MAX_KEEPERS} is "
                f"the keeper maximum — so exactly one of them is the prospect. ESPN does not "
                f"record which, so the slots above are filled from the roster and the prospect "
                f"is left empty. Set it before recording: a keeper charged as a prospect skips "
                f"the ${KEEPER_TAX} tax, and a prospect charged as a keeper pays it.",
            )
        )
    notes += _prospect_notes(
        season, active, deadline, season - 1, origins, {p.espn_player_id: p.name for p in players.values()}
    )

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
                "Some prior prospect keeps come from the frozen claims in data/history/ "
                "rather than from claims recorded here.",
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
        gate=gate,
    )


def build_season_screen(
    season: int,
    current: DerivedSeason,
    prior: DerivedSeason | None,
    store: ManualStore,
    *,
    now: datetime,
    first_nfl_season: Mapping[int, int] | None = None,
) -> SeasonScreen:
    """Every team's standing at a glance. Totals are added here, never in a template.

    ``first_nfl_season`` is passed straight through to each team, and leaving it out is not a
    harmless omission. ``keeper_rules`` reads ``None`` as "prospect rule 1 is not being applied"
    and an empty mapping as "it *is* being applied and this player is unknown" — so a season
    screen built without it reports every prospect in the league as unverified while the team
    screen, handed the same claim and the real draft classes, reports him checked. Two screens
    disagreeing about whether a rule ran is worse than either answer.
    """
    waived_manager, waiver_recorded = store.fees_waived_for(season)
    gate = keeper_gate(season, store, now=now)

    summaries = [
        build_team_screen(
            season,
            manager_id,
            current,
            prior,
            store,
            first_nfl_season=first_nfl_season,
            gate=gate,
        )
        for manager_id in current.manager_ids
    ]

    # No "N teams have declared nothing yet" note. Every card already says "Not recorded yet"
    # where that team is, and a list naming eleven franchises directly above eleven cards each
    # saying the same thing is one fact told twice.
    notes: list[Note] = []
    override_view = tuple(
        override_row(o, current)
        for o in sorted(store.overrides(season), key=lambda o: o.espn_player_id)
    )
    live_deltas = [row.delta for row in override_view if row.live]
    override_net = None if any(d is None for d in live_deltas) else sum(live_deltas)
    if override_net:
        notes.append(
            Note(
                "review",
                f"This season's live draft-cash overrides net to ${override_net}, not $0. A "
                f"cash trade moves money between two teams, so the legs should cancel — either "
                f"a counterparty is unrecorded or one of the figures is wrong.",
            )
        )

    if gate.state == "unrecorded":
        # Still REVIEW: an unrecorded deadline is a real gap in the season's record, and it has
        # to be counted as unverified. It is just not a reason to lock the tool that records it.
        notes.append(Note("review", gate.message))
    # Locked and open get no note. Locked is the page's operating state and the template says so
    # once, at the top, where it is acted on; open is what the deadline line already reads. The
    # old note here warned that the deadline had passed, which is now simply what a season does —
    # and a note repeating a banner is the same fact twice on one page.
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
        gate=gate,
        waiver_manager_id=waived_manager,
        waiver_name=current.name_of(waived_manager) if waived_manager else None,
        waiver_recorded=waiver_recorded,
        overrides=override_view,
        override_net=override_net,
    )


FIELD_SEP = "__"
"""Separates the manager id from the field name: ``t1__player_K1``.

The board is one form carrying all twelve franchises, so every field has to say which team it
belongs to — twelve copies of ``player_K1`` in one POST are indistinguishable.
"""


def split_league_form(form: Mapping[str, str]) -> dict[str, dict[str, str]]:
    """Split a board-wide form into one plain per-team form each.

    Exists so there is still exactly **one** parser. ``claims_from_form`` never learns that a
    league-wide POST is possible; it keeps taking a single team's fields, and this hands it
    those fields whether they arrived alone from one card or alongside eleven others.

    Fields with no manager prefix are dropped rather than guessed at. A field that cannot say
    which team it belongs to cannot be recorded against one.
    """
    split: dict[str, dict[str, str]] = {}
    for key, value in form.items():
        manager_id, sep, field = key.partition(FIELD_SEP)
        if not sep or not manager_id or not field:
            continue
        split.setdefault(manager_id, {})[field] = value
    return split


def claims_from_form(
    season: int,
    manager_id: str,
    form: dict[str, str],
    *,
    now: datetime | None = None,
    price_with: tuple[DerivedSeason, ManualStore] | None = None,
) -> tuple[list[KeeperClaim], list[str]]:
    """Build claims out of a posted form, and report what could not be parsed.

    **Keyed by slot, not by player.** The form is a team's four keeper slots — ``player_K1``,
    ``fee_K1``, and so on — because that is the shape of what a manager sends in: "K1 Nacua $5,
    K2 Cook $10, prospect Skattebo". The screen it posts from is four rows, not sixteen, and the
    commissioner is transcribing rather than shopping.

    It used to be the other way round: a slot dropdown on every rostered player,
    ``slot_{player_id}``. That reads the roster instead of the message, and it puts fifteen rows
    on screen to record four.

    A fee of ``-5`` parses fine and comes back as a ``NEGATIVE_FEE`` issue from the engine.
    That is deliberate: the input carries no ``min`` attribute, because a browser refusing the
    value would hide a rule violation behind a form error and teach nobody anything. A fee of
    ``abc`` is a different thing — it is not a number at all — and is reported here.

    So is **the same player picked into two slots**. That is not a league rule the engine can
    speak to — it is an impossible input, the way ``abc`` is — and it arrived with the slot-keyed
    form, which is the only shape that can express it. Left unreported the engine would price him
    twice and the team would silently owe double.

    ``price_with`` freezes ``computed_salary`` at submission time. It is optional so the live
    preview can build claims without pretending to record anything.
    """
    claims: list[KeeperClaim] = []
    problems: list[str] = []
    seen: dict[int, str] = {}

    for slot in SLOT_CHOICES:
        name = str(slot)
        raw_player = (form.get(f"player_{name}") or "").strip()
        if not raw_player:
            continue
        try:
            player_id = int(raw_player)
        except ValueError:
            problems.append(f"{name} holds {raw_player!r}, which is not a player id")
            continue

        if player_id in seen:
            problems.append(
                f"the same player is in both {seen[player_id]} and {name} — pick him once"
            )
            continue
        seen[player_id] = name

        raw_fee = (form.get(f"fee_{name}") or "").strip()
        try:
            fee = int(raw_fee) if raw_fee else 0
        except ValueError:
            problems.append(f"fee for {name} is {raw_fee!r}, which is not a whole number")
            continue

        claims.append(
            KeeperClaim(
                season=season,
                manager_id=manager_id,
                espn_player_id=player_id,
                slot=slot,
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
