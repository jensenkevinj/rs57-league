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
* a card recorded *before* ESPN pruned the roster is never prefilled around, so it can drift
  out of agreement with ESPN in silence. A claim naming somebody ESPN dropped is already an
  ERROR from the engine; the mirror image — ESPN kept a player nobody declared — has no slot
  on the card to be missing from, so it is named here as REVIEW.

A prospect screen that looks clean had better have checked something. Everything unverified
renders as unverified.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from rs57.admin.derived import DerivedSeason
from rs57.admin.store import ManualStore
from rs57.keeper_rules import (
    KEEPER_TAX,
    MAX_KEEPERS,
    MAX_PROSPECTS,
    PROSPECT_RULES_TIGHTENED,
    IssueCode,
    Severity,
    ValidationIssue,
    check_cash_trades,
    trade_groups,
    compute_team_keepers,
    effective_base_salary,
    fee_total_for,
    is_keeper_slot,
    keeper_salary,
)
from rs57.models import (
    STALE_WAIVER_WARNING,
    CashTrade,
    KeeperClaim,
    KeeperSlot,
    RosterEntry,
    SalaryOverride,
    to_league_time,
)

SLOT_CHOICES = (KeeperSlot.K1, KeeperSlot.K2, KeeperSlot.K3, KeeperSlot.PROSPECT)

DEFAULT_OVERRIDE_REASON = "Draft-cash trade"
"""What an override is, every time. See ``override_form``."""

SLOT_LABELS = {"K1": "K1", "K2": "K2", "K3": "K3", "PROSPECT": "P"}
"""Short labels for the card, which is narrow. Display only — the stored slot is unchanged."""

FEE_ISSUE_CODES = frozenset({
    IssueCode.FEE_TOTAL_MISMATCH,
    IssueCode.NEGATIVE_FEE,
    IssueCode.FEE_ON_PROSPECT,
})
"""The engine findings that are about **money**, not about who was picked.

The card answers two questions separately because the offseason asks them a day apart: the
keeper deadline settles *who is kept*, and the fee breakdowns arrive afterwards (commissioner,
2026-09-01). A single verdict made a card with a perfectly legal set of keepers and no fees
typed yet look broken on deadline night, which is when the selection is the only thing anybody
can act on.

Listed as the fee side and **not** as the selection side, so ``TeamScreen.selection_issues`` can
be the complement: a code nobody classifies still surfaces rather than falling through both.
"""

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
    manager_id: str | None = None
    """Who holds the player, as an id. ``manager`` is the display name and changes yearly —
    one of them carries a double space — so anything deciding *which side of a trade this leg
    is on* keys on this and never on the name."""

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
        manager_id=entry.manager_id if entry else None,
    )


@dataclass(frozen=True)
class TradeLeg:
    """One override, joined to the trade it is a leg of and the side of it that it sits on.

    ``side`` is ``"to"``, ``"from"``, ``"stranger"`` (a player on neither party's roster) or
    ``"unknown"`` (ESPN has no base, so which way the money moved cannot be worked out). The
    last two exist so the screen can *show* the problem rather than dropping the row and
    presenting a total that silently rests on fewer legs than the trade has.
    """

    row: OverrideRow
    side: str

    @property
    def live(self) -> bool:
        return self.row.live


@dataclass(frozen=True)
class TradeRow:
    """One cash trade, its legs, and what those legs actually move.

    ``received`` and ``paid`` are display figures — the sums the templates print. Whether the
    trade is **acceptable** is not decided here: ``issues`` comes straight from
    ``keeper_rules.check_cash_trades``, so there is exactly one implementation of the rule and
    the screen cannot disagree with the validator. Both are ``None`` when any live leg's dollar
    movement is unknown, because a total formed from a subset of the legs is not a total.
    """

    trade: CashTrade
    legs: tuple[TradeLeg, ...]
    from_name: str
    to_name: str
    received: int | None
    paid: int | None
    issues: tuple[ValidationIssue, ...] = ()
    netted_with: tuple[str, ...] = ()
    """Other trades sharing a salary edit with this one.

    When this is non-empty the per-trade ``paid``/``received`` below are **not** meaningful and
    are ``None``: the edits were netted, so no dollar in them belongs to one trade rather than
    another. The verdict in ``issues`` is the group's, computed per franchise."""

    @property
    def netted(self) -> bool:
        return bool(self.netted_with)

    @property
    def live_legs(self) -> tuple[TradeLeg, ...]:
        return tuple(leg for leg in self.legs if leg.live)

    @property
    def settled(self) -> bool:
        """Both sides put back in ESPN. The intended end state, not a problem."""
        return bool(self.legs) and not self.live_legs

    @property
    def balanced(self) -> bool:
        """Nothing to report — which for a settled trade is true without any sums at all."""
        return not self.issues


def trade_rows(
    trades: list[CashTrade],
    overrides: list[SalaryOverride],
    seasons: Mapping[int, DerivedSeason | None],
    roster: Sequence[RosterEntry],
) -> list[TradeRow]:
    """Join trades to their legs for the ledger.

    ``roster`` is every shown season's roster at once, which lets the engine be called a single
    time for the whole page. A leg is matched on ``(player, season)``, so one combined roster
    cannot confuse two seasons of the same player.

    The **verdict** is the engine's — ``issues`` is grouped straight off
    ``ValidationIssue.trade_id``. What is computed here is only what the table prints, and it
    is computed the same way the engine computes it: per side, keyed on ``manager_id``.
    """
    issues = check_cash_trades(roster, overrides, trades)
    group_of = {
        trade.id: tuple(t.id for t in group)
        for group in trade_groups(trades, overrides=overrides)
        for trade in group
    }
    by_trade: dict[str, list[SalaryOverride]] = {}
    for override in overrides:
        for trade_id in override.trade_ids:
            by_trade.setdefault(trade_id, []).append(override)

    rows: list[TradeRow] = []
    for trade in sorted(trades, key=lambda t: (t.draft_year, t.id), reverse=True):
        season = seasons.get(trade.draft_year)
        sides = {trade.from_manager_id: 0, trade.to_manager_id: 0}
        legs: list[TradeLeg] = []
        unknown = False
        for override in sorted(by_trade.get(trade.id, []), key=lambda o: o.espn_player_id):
            row = override_row(override, season)
            if row.delta is None or row.manager_id is None:
                side = "unknown"
            elif row.manager_id == trade.to_manager_id:
                side = "to"
            elif row.manager_id == trade.from_manager_id:
                side = "from"
            else:
                side = "stranger"
            legs.append(TradeLeg(row=row, side=side))
            if not row.live:
                continue
            # Only live legs move money — a reverted one has already been put back in ESPN.
            if side in ("unknown", "stranger"):
                unknown = True
            else:
                sides[row.manager_id] += row.delta

        siblings = tuple(t for t in group_of.get(trade.id, ()) if t != trade.id)
        rows.append(
            TradeRow(
                netted_with=siblings,
                trade=trade,
                legs=tuple(legs),
                from_name=season.name_of(trade.from_manager_id)
                if season
                else trade.from_manager_id,
                to_name=season.name_of(trade.to_manager_id)
                if season
                else trade.to_manager_id,
                # Netted edits carry no per-trade split, so no per-trade figure is shown.
                received=None if unknown or siblings else sides[trade.to_manager_id],
                paid=None if unknown or siblings else sides[trade.from_manager_id],
                issues=tuple(
                    issue
                    for issue in issues
                    if issue.trade_id in {trade.id, *group_of.get(trade.id, ())}
                ),
            )
        )
    return rows


def unlinked_override_issues(
    overrides: list[SalaryOverride], trades: list[CashTrade], roster: Sequence[RosterEntry]
) -> tuple[ValidationIssue, ...]:
    """Live legs attached to no trade at all — the ones no ``TradeRow`` can show.

    Split out because they belong under the table rather than in it: they are exactly the rows
    the per-trade audit **cannot** reach, and leaving them off the screen would make a ledger
    of balanced trades look like a complete account of the season's cash.
    """
    return tuple(
        issue
        for issue in check_cash_trades(roster, overrides, trades)
        if issue.code is IssueCode.OVERRIDE_NOT_ON_A_TRADE
    )


@dataclass(frozen=True)
class CashScreen:
    """One season's draft cash, audited against the whole ledger.

    **The engine runs over every season; only the display is filtered.** That split is the
    whole reason this object exists. ``trade_groups`` is union-find over trades that share a
    salary edit and ``check_cash_trades`` audits *a franchise's net across a group*, so handing
    it one season's trades computes ``expected`` over half a group and reports an imbalance that
    is not there. ``CASH_TRADE_LEG_WRONG_DRAFT`` exists precisely because a leg's season can
    differ from its trade's draft year, so a group spanning two seasons is a real state, not a
    hypothetical. Filtering the inputs would also manufacture ``OVERRIDE_NOT_ON_A_TRADE`` for
    every leg pointing at a trade the filter removed.

    Consequence, stated on screen rather than hidden: a trade from another draft year appears
    here when it nets with one of this season's. Showing half a netted pair would rest the
    verdict on a row you cannot see.
    """

    season: int
    trades: tuple[TradeRow, ...]
    overrides: tuple[OverrideRow, ...]
    unlinked: tuple[ValidationIssue, ...]
    """Live legs on no trade at all — **league-wide**, and labelled that way on screen."""
    league_net: int | None
    """Every live override in the file, summed. The season-scoped ``SeasonScreen.override_net``
    cannot see a leg misfiled into another year, so on its own it reads clean for exactly the
    mistake ``CASH_TRADE_LEG_WRONG_DRAFT`` was written to catch. This is what replaces the
    cross-season ledger the Draft cash tab used to be."""
    hidden_years: tuple[int, ...]
    """Draft years holding rows this page is not showing. A ledger that looks complete and is
    not is the failure this repo keeps guarding against — silence reads exactly like success."""

    @property
    def borrowed(self) -> tuple[str, ...]:
        """Trade ids shown from another draft year, because they net with one of this season's."""
        return tuple(row.trade.id for row in self.trades if row.trade.draft_year != self.season)


def cash_screen(
    season: int,
    trades: list[CashTrade],
    overrides: list[SalaryOverride],
    seasons: Mapping[int, DerivedSeason | None],
    roster: Sequence[RosterEntry],
) -> CashScreen:
    """Build one season's slice of the ledger. See ``CashScreen`` for why it is a slice."""
    rows = trade_rows(trades, overrides, seasons, roster)

    seed = {trade.id for trade in trades if trade.draft_year == season}
    # ``netted_with`` already carries the whole group, so one pass closes the set.
    visible_ids = seed | {
        sibling for row in rows if row.trade.id in seed for sibling in row.netted_with
    }
    visible = tuple(row for row in rows if row.trade.id in visible_ids)

    # A leg misfiled into another year stays visible on the page its trade lives on. Without
    # that, CASH_TRADE_LEG_WRONG_DRAFT names a row nobody can reach to fix.
    override_view = tuple(
        override_row(override, seasons.get(override.season))
        for override in sorted(overrides, key=lambda o: (o.season, o.espn_player_id))
        if override.season == season or (set(override.trade_ids) & visible_ids)
    )

    # Every live leg in the file, not just this season's. A reverted one has been put back in
    # ESPN and moves nothing. ``None`` anywhere means no total: a sum resting on a guessed zero
    # is worse than saying it cannot be computed.
    live = [
        override_row(override, seasons.get(override.season)).delta
        for override in overrides
        if not override.reverted
    ]
    league_net = None if any(delta is None for delta in live) else sum(live)

    shown_trades = {row.trade.id for row in visible}
    shown_overrides = {(row.override.season, row.override.espn_player_id) for row in override_view}
    hidden = {t.draft_year for t in trades if t.id not in shown_trades}
    hidden |= {
        o.season for o in overrides if (o.season, o.espn_player_id) not in shown_overrides
    }

    return CashScreen(
        season=season,
        trades=visible,
        overrides=override_view,
        unlinked=unlinked_override_issues(overrides, trades, roster),
        league_net=league_net,
        hidden_years=tuple(sorted(hidden)),
    )



@dataclass(frozen=True)
class KeeperDeadline:
    """Where the season stands relative to the keeper deadline. **Display only.**

    It used to be a lock: before the deadline the console refused to save, on the reasoning that
    no salary is entered that early so a lock costs nothing (commissioner, 2026-08-04). That
    premise turned out to be wrong — ESPN publishes keeper selections to nobody but an
    authenticated league member, so **manual entry is the only way the selections get in**, and
    the commissioner needs to enter and check them *before* the deadline, not after
    (commissioner, 2026-09-01). A lock across the one window the work happens in was blocking
    the tool's only input path.

    What the lock was actually protecting — a claim recorded while a manager could still change
    his mind — is not dropped. ``build_team_screen`` reports it, per claim, as REVIEW: the risk
    is made *visible* rather than *prevented*. That is a weaker guarantee and a wider net. The
    lock never said a word about the four claims recorded 2026-07-29, six days before the lock
    itself existed; the note does.

    **Three states, not two.** ``deadline`` is ``None`` for two entirely different reasons —
    ESPN has not set one yet, or that season has not synced. Those are not the same as a
    deadline still in the future, and the three stay distinct here because the screen says
    something different about each. The deadline comes from ESPN's own
    ``draftSettings.keeperDeadlineDate``, read off the derived season the nightly sync wrote
    (commissioner, 2026-08-26); nothing in this tool can set it.

    ``state`` is ``"upcoming"``, ``"passed"`` or ``"unrecorded"``. **No state refuses a write.**
    """

    deadline: datetime | None
    """Naive **UTC**, exactly as ESPN gave it and as the derived file stores it. It is compared
    against the app's clock, which is UTC for the same reason. Print ``local_deadline``."""
    state: str

    @property
    def passed(self) -> bool:
        return self.state == "passed"

    @property
    def local_deadline(self) -> datetime | None:
        """The same instant on the league's own wall clock, for printing and nothing else.

        A deadline of ``2026-09-02 03:00`` UTC is 11pm ET on 9/1, which is what ESPN shows and
        what a manager was told. Printing the UTC form named the wrong day.
        """
        return to_league_time(self.deadline)

    @property
    def message(self) -> str:
        if self.state == "upcoming":
            return (
                f"The keeper deadline ({self.local_deadline:%Y-%m-%d %H:%M} ET) has not passed "
                f"yet. Managers can still change their minds, so anything recorded now is "
                f"provisional and every claim entered before it says so until you re-record it."
            )
        if self.state == "unrecorded":
            return (
                "ESPN has no keeper deadline set for this season yet "
                "(draftSettings.keeperDeadlineDate), so there is nothing to record claims "
                "against. It appears here once ESPN sets one and the season re-syncs."
            )
        return (
            f"The keeper deadline ({self.local_deadline:%Y-%m-%d %H:%M} ET) has passed. "
            f"Selections are final."
        )


def keeper_deadline_fact(current: DerivedSeason, *, now: datetime) -> KeeperDeadline:
    """Read the deadline's state off ESPN's own date. The one place the three are decided.

    ``now`` must be **naive UTC** — ``models.utc_now``, which is what the app injects.
    ``deadline`` is naive UTC off ESPN, so a local-time clock here compares two different
    timezones and misreports the state for the length of the offset. That used to keep the
    console shut for five hours after the deadline had actually passed; it is now only a
    mislabelled tag, but the comparison is still between two UTC values for the same reason.
    """
    deadline = current.keeper_deadline
    if deadline is None:
        return KeeperDeadline(deadline=None, state="unrecorded")
    return KeeperDeadline(deadline=deadline, state="passed" if deadline < now else "upcoming")


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
    prospect_eligible: bool | None
    """Whether the rookie rule would admit him as a prospect. ``None`` means unknown.

    Three states, not two, and the third is the point. ``True``/``False`` come from ESPN's
    draft class — ``season - first_nfl_season <= 1``, the same arithmetic ``_prospect_notes``
    and the engine use. ``None`` is a player ESPN carries no draft class or debut year for, or
    a season the rookie rule does not govern at all.

    An unknown must never collapse into ``False``: it would make a genuinely open question
    look settled, and the prospect fill below reads this to decide whether the slot has one
    answer. A D/ST is ``False`` rather than ``None`` — the question does not apply to a
    negative id, which is settled, not unknown."""

    @property
    def claimed(self) -> bool:
        return bool(self.slot)


UNCHECKED = "UNVERIFIED — nobody has checked this"
"""How a REVIEW is labelled wherever it is shown.

The long form, deliberately. It was the wording on the flags this badge replaced, and it is
the sentence that stops a reader skimming past an item as though somebody had looked at it."""


@dataclass(frozen=True)
class CardStatus:
    """The one badge a card carries, and everything behind it.

    Replaces two badges and two stacks of flags under the table. The card is scanned twelve at
    a time, and the question being asked of it at a glance is one question: is this franchise
    finished, broken, or waiting on me.

    ``kind`` is ``ok`` / ``error`` / ``review`` / ``none`` and drives the colour. ``detail``
    is every issue and every note, in full — nothing is dropped on the way into the tooltip,
    only moved.
    """

    kind: str
    label: str
    detail: tuple[str, ...]

    @property
    def has_detail(self) -> bool:
        return bool(self.detail)


@dataclass(frozen=True)
class SlotRow:
    """One line of the card: a slot, who is in it, and what he costs THERE.

    **Priced per slot, not per player**, which is the whole reason this type exists. The same
    man is worth different money in K2 and in PROSPECT — a prospect pays no tax and carries no
    fee — so neither figure can be read off ``PlayerRow``. ``PlayerRow.tax`` is deliberately
    "what he would cost as a keeper": the right number for the picker, and the wrong one for
    this table. Reading it here would print a $5 tax against every prospect on the board.

    ``total`` is the engine's own figure, never assembled here or in the template — the
    recorded claim's when there is one, the proposal's when the card is only pre-filled.
    ``None`` means the engine did not price this row, which is a real state: an illegal card
    has slots the engine refuses to cost.
    """

    slot: str
    label: str
    row: PlayerRow | None
    tax: int
    total: int | None


@dataclass(frozen=True)
class TeamScreen:
    """One team's claim screen: priced rows, every issue, and what was not checked."""

    season: int
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
    proposed_salaries: Mapping[int, int] = field(default_factory=dict)
    """Player id to the salary the engine gives him in the pre-filled arrangement.

    Empty once anything is recorded — the record is priced by ``rows`` then. Without this the
    total column reads ``—`` down a card that is showing a complete set of keepers, which is
    the one column the commissioner is checking."""
    proposed_issues: tuple[ValidationIssue, ...] | None = None
    """The engine's verdict on the slots ESPN filled in, for a card nobody has recorded.

    ``None`` means there is no proposal to speak for — nothing is pre-filled, or the card
    already carries a record and the record is what the badges report on.

    Deliberately NOT folded into ``issues``. ``issues`` is what the engine found in the
    **recorded** claims and is what ``errors``, ``blocked`` and the issue list under the card
    all rest on; a proposal nobody submitted must not block a Record, appear as a finding
    against this franchise, or count toward the unverified badge. It answers exactly one
    question — would this card be legal if you recorded it as it stands — and only the
    Selection badge asks it."""
    saved: bool = False
    keeper_deadline: KeeperDeadline | None = None
    """Where the season stands relative to ESPN's keeper deadline. **Display only** — no route
    reads it and none may: it stopped being a gate on 2026-09-01. See ``KeeperDeadline``."""

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
    def fee_issues(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.code in FEE_ISSUE_CODES)

    @property
    def selection_issues(self) -> tuple[ValidationIssue, ...]:
        """Everything that is not a fee issue — **the default side of the partition.**

        Defined as the complement rather than as its own list on purpose. A new ``IssueCode``
        nobody remembered to classify lands here and is visible, instead of falling out of both
        badges and leaving a card that reads legal on both counts while the engine is objecting.
        """
        return tuple(i for i in self.verdict_issues if i.code not in FEE_ISSUE_CODES)

    @property
    def selection_error_count(self) -> int:
        """ERRORs only. The badge used to print ``len(selection_issues)``, which counts REVIEWs
        too — so a card showed "2 error(s)" beside a status line reading "1 error(s)", from two
        different definitions of the same word."""
        return len([i for i in self.selection_issues if i.severity is Severity.ERROR])

    @property
    def fee_error_count(self) -> int:
        return len([i for i in self.fee_issues if i.severity is Severity.ERROR])

    @property
    def status(self) -> CardStatus:
        """The card's one-word verdict: ``Valid``, ``Invalid``, or nothing checked yet.

        **Errors only.** A REVIEW does not make a card invalid — it never blocked a claim and
        calling it invalid would be false — so unverified items are not in this badge at all.
        They are on the card's status line, in ``unverified_reasons``, which is where "Not
        recorded yet" already lives. Nothing is dropped; the badge is just no longer the place
        that carries it.

        **The tooltip is populated only when it is Invalid**, because that is the only state
        with anything to explain. A valid card's badge has no title attribute, so the cursor
        does not change and there is nothing to hover for.

        Whether the card has been *recorded* is a different question and is not in here either.
        The status line answers it, and a card that is valid but unsaved says both — "Valid"
        above, "Not recorded yet" below.

        **There is deliberately no branch for the unrun fee check.** Above the keeper maximum
        the tier is undefined and the engine raises no ``FEE_TOTAL_MISMATCH`` at all, which
        would be silence reading as success — but that state cannot exist on its own: more than
        ``MAX_KEEPERS`` keepers always raises ``TOO_MANY_KEEPERS``, so the card is already
        Invalid and already says why. A second check for it was written here, could not be made
        to fail under mutation, and was removed: an unreachable guard is worse than none,
        because it reads like protection.
        """
        names = {row.espn_player_id: row.name for row in self.rows}
        errors = [i for i in self.verdict_issues if i.severity is Severity.ERROR]
        detail = [
            f"{issue.code}: "
            f"{names[issue.espn_player_id] + ' — ' if names.get(issue.espn_player_id) else ''}"
            f"{issue.message}"
            for issue in errors
        ]
        detail += [f"{note.message}" for note in self.notes if note.kind == "error"]

        if detail:
            return CardStatus("error", "Invalid", tuple(detail))
        if not self.declared:
            return CardStatus("none", "Not checked", ())
        return CardStatus("ok", "Valid", ())

    @property
    def unverified_reasons(self) -> tuple[str, ...]:
        """Everything nobody has checked, for the status line's own tag.

        Kept off the Valid/Invalid badge because an unverified item is neither — but kept, and
        counted, because an unverified thing rendering as nothing is what this project guards
        against hardest.

        League-wide facts are included here and excluded from ``review_count``: the note
        belongs on every card whose numbers it affects, and the tally belongs to the franchise.
        Twelve identical counts is how a real flag stops being read.
        """
        names = {row.espn_player_id: row.name for row in self.rows}
        out = [
            f"{UNCHECKED} · {issue.code}: "
            f"{names[issue.espn_player_id] + ' — ' if names.get(issue.espn_player_id) else ''}"
            f"{issue.message}"
            for issue in self.verdict_issues
            if issue.severity is Severity.REVIEW
        ]
        out += [
            f"{UNCHECKED}: {note.message}"
            for note in self.notes
            if note.kind == "review"
        ]
        out += [
            f"FOR INFORMATION: {note.message}"
            for note in self.notes
            if note.kind == "info"
        ]
        return tuple(out)

    @property
    def display_total_salary(self) -> int:
        """The footer figure, and it must agree with the Total column above it.

        ``total_salary`` prices the **record**, so on a pre-filled card it is $0 while every row
        above shows real money — a card whose own total contradicts its own rows, in the one
        column the commissioner is reading. Once anything is recorded the two are the same
        number by construction.

        The fee figures deliberately do NOT follow. Nobody has typed a fee yet, and a footer
        reading "$5 short" against a card nobody has touched reports a shortfall in money the
        workflow does not collect until after the selection is settled.
        """
        if self.selection_proposed:
            return sum(self.proposed_salaries.values())
        return self.total_salary

    @property
    def selection_proposed(self) -> bool:
        """Is the Selection badge speaking for ESPN's prefill rather than for a record?

        The template must render this differently from a recorded verdict and never green.
        After the prune a pre-filled card *looks* finished, so a green badge would make it
        indistinguishable from a saved one — and the commissioner would work down twelve green
        cards, press Record, and find nothing had been written.
        """
        return not self.declared and self.proposed_issues is not None

    @property
    def verdict_issues(self) -> tuple[ValidationIssue, ...]:
        """The findings the badges speak for: the record's, or the proposal's when there is no
        record. One accessor so a badge cannot report on a different set than its tooltip."""
        return self.proposed_issues or () if self.selection_proposed else self.issues

    @property
    def selection_verdict(self) -> str:
        """``"none"``, ``"error"``, ``"review"`` or ``"ok"`` — is this a legal set of keepers?

        The first of the two questions the card answers, and on deadline night the only one that
        matters: who is kept is settled tonight, the fees are entered afterwards
        (commissioner, 2026-09-01).

        **"none" now means genuinely nothing to say**, not merely "nothing saved yet". A card
        ESPN has pre-filled is judged on that prefill and reports ok/error/review with
        ``selection_proposed`` set, because a grey "nothing declared" over four filled slots
        told the commissioner the opposite of what the card was showing him.
        """
        issues = self.verdict_issues
        if not self.declared and not self.selection_proposed:
            return "none"
        selection = tuple(i for i in issues if i.code not in FEE_ISSUE_CODES)
        if any(i.severity is Severity.ERROR for i in selection):
            return "error"
        if selection:
            return "review"
        return "ok"

    @property
    def fee_verdict(self) -> str:
        """``"none"``, ``"skipped"``, ``"error"`` or ``"ok"`` — do the fees follow the rules?

        ``"skipped"`` is the one that has to exist. Over the keeper maximum the tier is
        **undefined** — ``fee_total_for`` has no answer for four keepers — so ``keeper_rules``
        does not raise ``FEE_TOTAL_MISMATCH`` at all. An empty issue list there means the check
        never ran, and rendering that as a green "fees legal" is exactly the failure this
        repo is built around: silence reading as success.
        """
        if not self.declared:
            return "none"
        if self.fee_expected is None:
            return "skipped"
        if self.fee_issues:
            return "error"
        return "ok"

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

        K1/K2/K3 are interchangeable, so the fill runs dearest first. The keeper/prospect split
        is the load-bearing one (``is_keeper_slot``, ``prior_prospect_ids``), and **ESPN cannot
        say which keep is the prospect** — but the rookie rule often can, so ``obvious_prospect``
        fills that slot on the teams where only one kept player could legally hold it. Where
        more than one could, it stays empty and the note says which players are in the running.

        **Every slot stays a picker regardless** — see the template. The fill is a default on
        screen, not a record, and a rookie is *allowed* to be kept in a keeper slot, so an
        eligible player is a suggestion about the prospect rather than a determination.

        A slot claimed twice keeps the first. That is a rule violation the engine reports as an
        issue and the card shows as an error tag; the card is not the place to adjudicate it.
        """
        claimed: dict[str, PlayerRow] = {}
        for row in self.rows:
            if row.claimed and row.slot not in claimed:
                claimed[row.slot] = row

        fill = _prefilled_slots(self.rows) if not claimed else {}

        out: list[SlotRow] = []
        for slot in SLOT_CHOICES:
            name = str(slot)
            row = claimed.get(name) or fill.get(name)
            out.append(
                SlotRow(
                    slot=name,
                    label=SLOT_LABELS[name],
                    row=row,
                    tax=_slot_tax(slot, row),
                    total=self._slot_total(row),
                )
            )
        return tuple(out)

    def _slot_total(self, row: PlayerRow | None) -> int | None:
        """What the engine charged for this row: the record's price, or the proposal's.

        Never assembled from base, tax and fee here. ``keeper_rules`` is the only thing in this
        project that prices a keeper, and a second adder on the screen is how the card and the
        engine come to disagree about the same player.
        """
        if row is None:
            return None
        if row.salary is not None:
            return row.salary
        return self.proposed_salaries.get(row.espn_player_id)

    @property
    def obvious_prospect(self) -> PlayerRow | None:
        """The one kept player who could hold the prospect slot, or ``None`` when it is a choice.

        Only ever a *default*. A rookie may legally be kept in a normal keeper slot, so this
        answers "who is the only one who could be the prospect", never "who is the prospect".

        Two things make it stand down, and both mean the same thing — the answer is not
        forced:

        * **More than one eligible player**, which is the case the commissioner has to settle
          by hand.
        * **Any unknown on the roster.** A player ESPN carries no draft class for could be
          eligible too, so "exactly one eligible" is not established while one is unresolved.
          Filling anyway would present a guess as the settled answer, which is the failure this
          codebase keeps guarding against.

        Before the prune this returns ``None`` outright: the roster is still everybody, so
        "the only eligible player" would be a fact about the whole squad rather than about
        the keepers, and it would fill the slot with somebody nobody kept.
        """
        if not self.keepers_only:
            return None
        eligible, unresolved = _prospect_candidates(self.rows)
        if unresolved or len(eligible) != 1:
            return None
        return eligible[0]

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
    teams: tuple[TeamScreen, ...]
    """Full screens, not summaries. This page *is* the entry form for all twelve franchises, so
    it needs every priced row — and a summary object alongside them would be a second place for
    the same numbers to live."""
    notes: tuple[Note, ...]
    keeper_deadline: KeeperDeadline
    waiver_recorded: bool
    """Whether ``season - 1`` has a consolation winner on file. Drives the note below; the
    winner's id and name are deliberately not carried — a franchise's own "fees waived" tag is
    read on that franchise's card, where the number it affects is."""
    overrides: tuple[OverrideRow, ...]
    """This season's draft-cash trades, recorded in the same sitting as the fees."""
    override_net: int | None
    """Live overrides summed. Should be zero — a cash trade moves money between two teams, and
    ``check_override_balance`` reports it when it does not. ``None`` when any live row has no
    ESPN base to compare against, because a total resting on a guess is worse than no total."""


def _slot_tax(slot: KeeperSlot, row: PlayerRow | None) -> int:
    """The $5 tax as it applies IN THIS SLOT — the rule, not the player's attribute.

    ``keeper_salary`` waives it for a prospect regardless of what is passed in, so a taxed
    player moved into the prospect slot owes nothing. ``PlayerRow.tax`` cannot express that:
    it is computed once, as a K1, for the picker's candidate price.
    """
    if row is None or slot is KeeperSlot.PROSPECT or not row.kept_prior_year:
        return 0
    return KEEPER_TAX


def _prefilled_slots(rows: Sequence[PlayerRow]) -> dict[str, PlayerRow]:
    """ESPN's own answer arranged into slots, or ``{}`` when it has none to give.

    **The one place the arrangement is decided.** Both the card and the pre-validated Selection
    badge read it, so the badge cannot report on a different set of keepers than the one on
    screen — which is the way a green badge over a wrong card would happen.

    Empty unless ESPN has pruned to the kept players; before that the roster is everybody and
    there is nothing to arrange. The prospect goes in only when ``_prospect_candidates`` leaves
    one answer, and comes out of the keeper run first so a four-keep team does not claim him
    twice.
    """
    if not 0 < len(rows) <= ROSTER_IS_KEEPERS_ONLY:
        return {}
    eligible, unresolved = _prospect_candidates(rows)
    prospect = eligible[0] if not unresolved and len(eligible) == 1 else None
    keepers = [
        row
        for row in sorted(rows, key=lambda r: (-r.candidate_price, r.name))
        if prospect is None or row.espn_player_id != prospect.espn_player_id
    ][:MAX_KEEPERS]

    out: dict[str, PlayerRow] = {}
    for slot, row in zip([s for s in SLOT_CHOICES if is_keeper_slot(s)], keepers):
        out[str(slot)] = row
    if prospect is not None:
        out[str(KeeperSlot.PROSPECT)] = prospect
    return out


def _prospect_candidates(
    rows: Sequence[PlayerRow],
) -> tuple[list[PlayerRow], list[PlayerRow]]:
    """``(eligible, unresolved)`` among these rows, by the rookie rule.

    One computation behind both the prospect fill and the note that explains it. They were
    briefly separate and could disagree — a card can say the slot was left open for you to
    settle while the slot above it is filled in, and the reader believes the slot.
    """
    return (
        [row for row in rows if row.prospect_eligible],
        [row for row in rows if row.prospect_eligible is None],
    )


def _prospect_eligible(
    season: int, espn_player_id: int, first_nfl_season: Mapping[int, int] | None
) -> bool | None:
    """Would the rookie rule admit this player as a prospect? ``None`` when unknowable.

    The arithmetic is ``keeper_rules``' own: ``elapsed = season - first_nfl_season`` and ``> 1``
    is the violation, so a genuine rookie gives exactly 1 and anything at or under that is
    eligible. Stated here as ``<= 1`` rather than ``== 1`` so a player whose recorded first
    season is the keeper season itself does not read as ineligible on a technicality.

    **A D/ST is False, never None.** Negative id, 404 by construction — the question does not
    apply, which is a settled answer rather than a missing one. Folding it into the unknowns
    would make every roster carrying a defense look ambiguous.
    """
    if espn_player_id < 0:
        return False
    if first_nfl_season is None:
        return None
    began = first_nfl_season.get(espn_player_id)
    if began is None:
        return None
    return season - began <= 1


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
    keeper_deadline: KeeperDeadline | None = None,
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
                prospect_eligible=_prospect_eligible(season, entry.espn_player_id, origins),
            )
        )

    rows.sort(key=lambda row: (not row.claimed, row.slot, -row.candidate_price, row.name))

    # **Would this card be legal if you recorded it as it stands?** Only asked of a card with
    # nothing on file: once anything is recorded, the record is what the badges report on.
    #
    # Run through the same engine and the same arguments as the real claims, so a proposal that
    # reads legal here is legal when it is recorded. The fees are $0 because nobody has typed
    # them yet — which is why only the *selection* half of the verdict reads this. The fee
    # badge stays on the record, or every pre-filled card would open with a red fee shortfall
    # for money the workflow does not collect until afterwards.
    proposed_issues: tuple[ValidationIssue, ...] | None = None
    proposed_salaries: dict[int, int] = {}
    prefill = _prefilled_slots(tuple(rows)) if not active else {}
    if prefill:
        proposal = [
            KeeperClaim(
                season=season,
                manager_id=manager_id,
                espn_player_id=row.espn_player_id,
                slot=KeeperSlot(slot),
                fee_allocated=0,
                submitted_at=None,
            )
            for slot, row in prefill.items()
        ]
        proposal_result = compute_team_keepers(
            proposal,
            roster,
            overrides,
            manager_id=manager_id,
            fees_waived=fees_waived,
            first_nfl_season=origins,
            trade_deadline=deadline,
            prior_prospect_ids=prospect_ids,
        )
        proposed_issues = proposal_result.issues
        proposed_salaries = {
            keeper.espn_player_id: keeper.salary for keeper in proposal_result.keepers
        }

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
    # Who the prospect is, on a pruned roster with nothing recorded yet. ESPN never says, so
    # the rookie rule is the only thing that can narrow it, and what it can prove varies by
    # team. Each outcome gets its own note: a filled slot the commissioner should confirm, or
    # an open one naming exactly who is in the running.
    #
    # The cost of getting it wrong is the same in every branch and is stated in every branch —
    # the keeper/prospect split moves both the $5 tax and the fee tier, and it is the one thing
    # on this card ESPN cannot check afterwards.
    if 0 < len(rows) <= ROSTER_IS_KEEPERS_ONLY and not active:
        eligible, unresolved = _prospect_candidates(rows)
        forced = len(rows) > MAX_KEEPERS
        stakes = (
            f"a keeper charged as a prospect skips the ${KEEPER_TAX} tax, and a prospect "
            f"charged as a keeper pays it — and the keeper count sets the fee tier"
        )
        if unresolved:
            notes.append(
                Note(
                    "review",
                    f"The prospect slot is NOT filled: ESPN carries no draft class for "
                    f"{', '.join(row.name for row in unresolved)}, so it cannot be shown that "
                    f"only one kept player is rookie-eligible. Set the slot by hand — {stakes}.",
                )
            )
        elif len(eligible) == 1 and forced:
            notes.append(
                Note(
                    "info",
                    f"ESPN kept {len(rows)} players and {MAX_KEEPERS} is the keeper maximum, so "
                    f"one of them must be the prospect — and {eligible[0].name} is the only one "
                    f"the rookie rule admits. The slot is filled with him. Change it if the "
                    f"manager said otherwise.",
                )
            )
        elif len(eligible) == 1:
            notes.append(
                Note(
                    "review",
                    f"The prospect slot is pre-filled with {eligible[0].name}, the only kept "
                    f"player the rookie rule admits — but with {len(rows)} kept and "
                    f"{MAX_KEEPERS} keepers allowed, this team need not be using the slot at "
                    f"all. Confirm against what the manager sent: {stakes}.",
                )
            )
        elif eligible:
            notes.append(
                Note(
                    "review",
                    f"The prospect slot is NOT filled: "
                    f"{', '.join(row.name for row in eligible)} are all rookie-eligible, so the "
                    f"rookie rule cannot say which is the prospect. Set it by hand — {stakes}.",
                )
            )
        elif forced:
            notes.append(
                Note(
                    "review",
                    f"ESPN kept {len(rows)} players and {MAX_KEEPERS} is the keeper maximum, so "
                    f"one must be the prospect — but the rookie rule admits none of them. "
                    f"Either a keep is not what ESPN reports, or a prospect claim here will be "
                    f"flagged ineligible. Resolve before recording.",
                )
            )
    # The prefill in ``TeamScreen.slots`` runs only on a card with nothing recorded, so a
    # franchise entered *before* ESPN pruned keeps whatever was typed then and never learns
    # that ESPN went on to disagree. The engine catches half of that already — a claim naming
    # somebody ESPN dropped is PLAYER_NOT_ON_ROSTER, an ERROR. This is the other half, and it
    # is the silent one: ESPN kept a player nobody declared, and an undeclared keeper is a
    # slot that simply is not there to look at.
    #
    # REVIEW, not ERROR, and by the same split as the rookie rule: the claim is the league's
    # record and ESPN is downstream of it, so an outside source flags and never blocks.
    if 0 < len(rows) <= ROSTER_IS_KEEPERS_ONLY and active:
        undeclared = [row.name for row in rows if not row.claimed]
        if undeclared:
            notes.append(
                Note(
                    "review",
                    f"ESPN has pruned this roster to its kept players and "
                    f"{', '.join(undeclared)} "
                    f"{'is' if len(undeclared) == 1 else 'are'} not claimed on this card. "
                    f"Either the card was recorded before ESPN pruned and the manager has "
                    f"since changed their mind, or a slot is missing. The card shows what was "
                    f"recorded, not what ESPN says — reconcile the two before the auction.",
                )
            )

    notes += _prospect_notes(
        season, active, deadline, season - 1, origins, {p.espn_player_id: p.name for p in players.values()}
    )

    # **This is what replaces the deadline lock.** Until 2026-09-01 the console simply refused
    # to record before the deadline, so a claim entered while a manager could still change his
    # mind could not exist. It can now, because manual entry is the only way selections reach
    # this tool and the deadline is exactly when that entry happens — so the risk is reported
    # instead of prevented.
    #
    # Wider than the lock ever was, and that is the point: the lock said nothing about the four
    # claims stamped 2026-07-29, six days before the lock itself was written. This does.
    # It clears itself the moment the card is re-recorded after the deadline.
    # `deadline_at`, not `keeper_deadline`: this used to shadow the parameter of that name, so
    # the KeeperDeadline passed in was never read and the raw datetime went into TeamScreen's
    # `keeper_deadline` field, which is declared KeeperDeadline. Nothing read it, so nothing
    # broke — but the parameter is live below and needs its own name back.
    deadline_at = current.keeper_deadline
    if deadline_at is not None:
        provisional = sorted(
            {
                claim.submitted_at
                for claim in stored
                if claim.submitted_at is not None and claim.submitted_at < deadline_at
            }
        )
        if provisional:
            when = to_league_time(provisional[0])
            notes.append(
                Note(
                    "review",
                    f"Recorded {when:%Y-%m-%d %H:%M} ET, before this season's keeper deadline "
                    f"({to_league_time(deadline_at):%Y-%m-%d %H:%M} ET) — so managers could "
                    f"still change their minds after it was entered. Provisional until you "
                    f"re-record this card, which clears this note.",
                )
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
    # Not while ESPN holds the entered keeper prices. In that window its field is base + fee +
    # tax rather than the price the player was acquired for, so every waiver add carrying a fee
    # "disagrees" with its own FAAB bid by exactly that fee. `espn.py` stops recording these at
    # sync time; this is what keeps a file synced before that fix from reporting them.
    entered_prices = (
        not current.drafted and keeper_deadline is not None and keeper_deadline.passed
    )
    for warning in current.warnings:
        if entered_prices and STALE_WAIVER_WARNING in warning:
            continue
        notes.append(Note("review", f"{season} sync: {warning}", team_specific=False))
    mismatched = [
        row.name for row in rows if row.espn_player_id in current.waiver_base_mismatches
    ]
    if mismatched and not entered_prices:
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
        proposed_salaries=proposed_salaries,
        proposed_issues=proposed_issues,
        fees_waived=fees_waived,
        waiver_recorded=waiver_recorded,
        submitted_at=max(submitted) if submitted else None,
        saved=saved,
        keeper_deadline=keeper_deadline,
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
    deadline_fact = keeper_deadline_fact(current, now=now)

    summaries = [
        build_team_screen(
            season,
            manager_id,
            current,
            prior,
            store,
            first_nfl_season=first_nfl_season,
            keeper_deadline=deadline_fact,
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

    if deadline_fact.state == "unrecorded":
        # Still REVIEW: an unrecorded deadline is a real gap in the season's record, and it has
        # to be counted as unverified. It is just not a reason to lock the tool that records it.
        notes.append(Note("review", deadline_fact.message))
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
        teams=tuple(summaries),
        notes=tuple(notes),
        keeper_deadline=deadline_fact,
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
    form: dict[str, str], *, now: datetime, known_trades: set[str] | None = None
) -> tuple[SalaryOverride | None, list[str]]:
    """Build a ``SalaryOverride`` from the posted form.

    ``known_trades`` is the set of trade ids on file. When given, a ``trade_id`` naming a trade
    that does not exist is refused rather than stored — a dangling reference would leave the
    leg out of both audits at once, since the per-trade check has no trade to balance it
    against and the league-wide one skips anything carrying a ``trade_id``. Defaults to ``None``
    (no check) so the form stays usable without a store to hand.

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
    # Defaulted, not demanded. It is a draft-cash trade every single time, so requiring the
    # commissioner to retype that on every row bought nothing; the rows already on file carry
    # real provenance and keep it. The field stays stored and stays escaped at render.
    reason = (form.get("reason") or "").strip() or DEFAULT_OVERRIDE_REASON
    named = tuple(t for t in form.get("trade_ids", "").split(",") if t.strip())
    if known_trades is not None:
        for trade_id in named:
            if trade_id not in known_trades:
                problems.append(f"no trade {trade_id} on file — record the trade before its legs")

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
            trade_ids=tuple(t for t in form.get("trade_ids", "").split(",") if t.strip()),
        ),
        [],
    )


def new_trade_id(draft_year: int, from_manager_id: str, to_manager_id: str, taken: set[str]) -> str:
    """A readable, stable id for a new trade: ``2026-t3-to-t10``, suffixed if that is taken.

    Readable because it is what a leg carries in ``overrides.json`` and what an audit message
    names — a UUID there would make the file unreadable in a diff, and the diff is the only
    review step between this tool and a public repo. Uniqueness is checked league-wide rather
    than per season, because a leg names a trade by id alone.
    """
    stem = f"{draft_year}-{from_manager_id}-to-{to_manager_id}"
    if stem not in taken:
        return stem
    n = 2
    while f"{stem}-{n}" in taken:
        n += 1
    return f"{stem}-{n}"


def trade_form(
    form: dict[str, str],
    *,
    now: datetime,
    taken: set[str],
    known_managers: Sequence[str] = (),
    existing_id: str | None = None,
) -> tuple[CashTrade | None, list[str]]:
    """Build a ``CashTrade`` from the posted form.

    ``existing_id`` turns this into the edit form: the id is kept rather than minted, so a
    trade keeps the identity its legs already name even when its draft year or its two parties
    change. Every other rule below applies identically — an edit that made a trade illegal
    would otherwise slip past the checks the original had to satisfy.

    The direction is not a detail to be inferred later: ``from`` pays and ``to`` receives, and
    the audit reads the sign of every leg off that. So both are required, they must differ, and
    both must be franchises that actually exist in the draft being recorded — a typo in a
    manager id would otherwise produce a trade whose legs all read as belonging to strangers.

    ``amount`` must be positive. A $0 cash trade balances trivially and records nothing that
    happened, which would put a row on file that no audit could ever fail.

    ``agreed_at`` is **when the trade happened**, not when it was typed in. Those are routinely
    months apart — the 2025 legs are being reconstructed from a workbook in 2026 — so it is a
    field rather than a clock read, and it falls back to ``now`` only when left blank.

    ``note`` is free text and, like ``SalaryOverride.reason``, is stored exactly as typed and
    escaped at render time — never sanitised on the way in.
    """
    problems: list[str] = []
    try:
        draft_year = int((form.get("draft_year") or "").strip())
    except ValueError:
        problems.append("draft year must be a year")
        draft_year = 0
    try:
        amount = int((form.get("amount") or "").strip())
    except ValueError:
        problems.append("amount must be a whole number of dollars")
        amount = 0
    if amount <= 0:
        problems.append("amount must be more than $0 — a $0 cash trade records nothing")

    agreed = now
    raw_date = (form.get("agreed_at") or "").strip()
    if raw_date:
        try:
            agreed = datetime.fromisoformat(raw_date)
        except ValueError:
            problems.append(f"{raw_date} is not a date — use YYYY-MM-DD")

    payer = (form.get("from_manager_id") or "").strip()
    payee = (form.get("to_manager_id") or "").strip()
    if not payer or not payee:
        problems.append("a cash trade needs both a paying and a receiving franchise")
    elif payer == payee:
        problems.append("a cash trade moves money between two teams, not from a team to itself")
    if known_managers:
        for role, manager_id in (("paying", payer), ("receiving", payee)):
            if manager_id and manager_id not in known_managers:
                problems.append(
                    f"{manager_id} is not a franchise in {draft_year} ({role} side)"
                )

    if problems:
        return None, problems
    return (
        CashTrade(
            id=existing_id or new_trade_id(draft_year, payer, payee, taken),
            draft_year=draft_year,
            from_manager_id=payer,
            to_manager_id=payee,
            amount=amount,
            agreed_at=agreed,
            note=(form.get("note") or "").strip(),
        ),
        [],
    )


FEE_HELP = (
    f"The tier is the team's total: 1 keeper owes $0, 2 owe $5, 3 owe $15. The split across "
    f"them is the manager's own choice, so nothing is filled in for them. A keeper kept last "
    f"season also owes the ${KEEPER_TAX} tax, which the engine adds — it is not part of the fee."
)

LIMITS_HELP = f"Max {MAX_KEEPERS} keepers plus {MAX_PROSPECTS} prospect. There is no salary cap."
