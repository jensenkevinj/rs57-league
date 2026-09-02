"""The keeper rules engine.

**Pure.** No I/O, no Flask, no ESPN, no reads from ``data/``. Everything it needs arrives as
arguments. This is the tested core and it stays that way.

The salary formula::

    effective_base = un-reverted SalaryOverride.actual_salary, else ESPN base
    salary         = effective_base + allocated_fee + ($5 if kept_prior_year)
    prospect       = effective_base                      # no fee, no tax, ever

``effective_base`` is what the player cost his manager *this* season, not his original
acquisition value — see ``RosterEntry.base_salary``. The keeper ratchet (base + fee + tax all
carry into next year's base) happens in the data pipeline, not in this formula.

There is **no salary cap**. Keeper totals are unbounded. Don't add a cap check, and don't
leave a disabled one lying around for someone to switch on later.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from rs57.models import CashTrade, KeeperClaim, KeeperSlot, RosterEntry, SalaryOverride

KEEPER_TAX = 5
"""Charged when the player was kept the previous season. Waived on a drop, NOT on a trade."""

FEE_TIERS: Mapping[int, int] = {0: 0, 1: 0, 2: 5, 3: 15}
"""Total keeper fee owed, by number of keepers. The manager splits it however they like."""

MAX_KEEPERS = 3
MAX_PROSPECTS = 1

PROSPECT_RULES_TIGHTENED = 2026
"""First season a prospect must be a **rookie**.

The league previously allowed second-year players to be prospected as well, and separately
required that a prospect had never been *started* by any league team. Both were relaxed to the
single rookie-only rule (commissioner, 2026-07-30); the never-started rule is gone entirely and
prospects may now be started.

The date matters because the record contains claims made under the old rule that the new one
would reject — Tyjae Spears was kept as a prospect in both 2024 and 2025, legally. Applying
today's rule backwards would turn a correct historical record into a wall of errors, so callers
pass ``prior_prospect_ids`` **and** ``first_nfl_season`` only for seasons from here on. This
gate now governs two checks rather than one; both express the same rule and both must be gated,
or the pre-2026 record starts failing under half of it.

Set to 2026 because the 2025 declarations still include a second-year prospect. If the change
actually landed mid-2025, this is the one line to move.

Independently corroborated. Checking every recorded prospect claim against ESPN's draft class
puts nine of ten at ``first_nfl_season == keeper_season - 1``; the single exception is Spears'
2025 claim. An outside source reproducing the league's record *and* its one documented
exception is about as good as evidence for a threshold gets — see ``fixtures/prospect_cases.json``.
"""

KEEPER_SLOTS = (KeeperSlot.K1, KeeperSlot.K2, KeeperSlot.K3)


class Severity(StrEnum):
    ERROR = "error"
    """Blocks the claim. The manager has to fix it."""

    REVIEW = "review"
    """Needs the commissioner's eyes. Renders on screen as unverified rather than passing
    silently — nobody should assume a REVIEW item was checked."""


class IssueCode(StrEnum):
    TOO_MANY_KEEPERS = "too_many_keepers"
    TOO_MANY_PROSPECTS = "too_many_prospects"
    FEE_TOTAL_MISMATCH = "fee_total_mismatch"
    NEGATIVE_FEE = "negative_fee"
    FEE_ON_PROSPECT = "fee_on_prospect"
    DUPLICATE_PLAYER = "duplicate_player"
    DUPLICATE_SLOT = "duplicate_slot"
    PLAYER_NOT_ON_ROSTER = "player_not_on_roster"
    PROSPECT_TOO_MANY_SEASONS = "prospect_too_many_seasons"
    PROSPECT_ROOKIE_UNVERIFIED = "prospect_rookie_unverified"
    PROSPECT_ACQUIRED_AFTER_DEADLINE = "prospect_acquired_after_deadline"
    PROSPECT_REPEAT_CLAIM = "prospect_repeat_claim"
    BASE_DISCONTINUITY = "base_discontinuity"
    OVERRIDES_UNBALANCED = "overrides_unbalanced"
    CASH_TRADE_UNBALANCED = "cash_trade_unbalanced"
    CASH_TRADE_NO_LEGS = "cash_trade_no_legs"
    CASH_TRADE_UNCHECKABLE = "cash_trade_uncheckable"
    CASH_TRADE_STRANGER_LEG = "cash_trade_stranger_leg"
    CASH_TRADE_LEG_WRONG_DRAFT = "cash_trade_leg_wrong_draft"
    OVERRIDE_NOT_ON_A_TRADE = "override_not_on_a_trade"


@dataclass(frozen=True)
class ValidationIssue:
    code: IssueCode
    severity: Severity
    message: str
    manager_id: str | None = None
    espn_player_id: int | None = None
    slot: KeeperSlot | None = None
    trade_id: str | None = None
    """Which cash trade this is about, for the screens that group issues by trade.

    Carried as a field rather than parsed back out of ``message``: the message is prose meant
    for a human and it changes whenever the wording is improved, which would silently break
    any grouping that depended on reading it.
    """


@dataclass(frozen=True)
class ComputedKeeper:
    espn_player_id: int
    slot: KeeperSlot
    base_salary: int
    fee_allocated: int
    kept_prior_year: bool
    salary: int


@dataclass(frozen=True)
class TeamKeeperResult:
    manager_id: str
    keepers: tuple[ComputedKeeper, ...] = ()
    issues: tuple[ValidationIssue, ...] = field(default=())

    @property
    def total_salary(self) -> int:
        return sum(keeper.salary for keeper in self.keepers)

    @property
    def total_fees(self) -> int:
        return sum(keeper.fee_allocated for keeper in self.keepers)

    @property
    def blocked(self) -> bool:
        """True if any issue is an ERROR. REVIEW issues do not block."""
        return any(issue.severity is Severity.ERROR for issue in self.issues)


def is_keeper_slot(slot: KeeperSlot) -> bool:
    return slot in KEEPER_SLOTS


def effective_base_salary(
    entry: RosterEntry, overrides: Iterable[SalaryOverride] = ()
) -> int:
    """The player's true salary for his season, with any live draft-cash distortion undone.

    An un-reverted override wins over ESPN. If several apply to the same player and season —
    which should not happen — the most recently created one wins, deterministically.
    """
    applicable = [
        override
        for override in overrides
        if override.espn_player_id == entry.espn_player_id
        and override.season == entry.season
        and not override.reverted
    ]
    if not applicable:
        return entry.base_salary
    return max(applicable, key=lambda override: override.created_at).actual_salary


def charges_in_base(drafted: bool, keeper_deadline: datetime | None, now: datetime) -> bool:
    """Whether ESPN's keeper field currently holds the ENTERED price, not the carried-in one.

    ``keeperValue`` means "what this player carried in from last season" for most of the year.
    Between the keeper deadline and the auction it does not: the commissioner has typed this
    season's keeper prices into ESPN by then, so the field holds base + allocated fee + $5 tax.
    Anything that adds a charge on top of it in that window charges the same money twice, and
    anything that compares it against an acquisition price compares two different facts.

    **Three states, not two.** A season ESPN has set no deadline for cannot place itself in this
    window and reads the way it does the rest of the year — a missing fact is not a past one.

    This reads the deadline; it does not enforce it. Nothing gates on the result except which of
    two meanings the field currently carries. ``now`` must be naive UTC, like the stored
    deadline, so both sides of the comparison are the same clock.
    """
    return not drafted and keeper_deadline is not None and keeper_deadline < now


def keeper_salary(
    base_salary: int,
    fee_allocated: int,
    kept_prior_year: bool,
    slot: KeeperSlot = KeeperSlot.K1,
) -> int:
    """Salary for one keeper.

    Prospects are kept at their acquisition value: no fee, no tax, regardless of what is
    passed in. That is a rule, not a convenience — a fee on a prospect is separately reported
    as ``FEE_ON_PROSPECT`` rather than silently priced in here.
    """
    if slot is KeeperSlot.PROSPECT:
        return base_salary
    return base_salary + fee_allocated + (KEEPER_TAX if kept_prior_year else 0)


def fee_total_for(n_keepers: int, fees_waived: bool = False) -> int:
    """Total fee owed for keeping ``n_keepers`` players. Prospects do not count.

    The consolation bracket winner has fees waived for one year. That does not mean free
    keepers — salaries are still owed in full, only the fee on top is waived.

    Raises ``ValueError`` above ``MAX_KEEPERS``; callers check the count first and report
    ``TOO_MANY_KEEPERS`` rather than pricing an illegal roster.
    """
    if fees_waived:
        return 0
    if n_keepers not in FEE_TIERS:
        raise ValueError(f"no fee tier for {n_keepers} keepers (max {MAX_KEEPERS})")
    return FEE_TIERS[n_keepers]


def derive_kept_prior_year(
    prior_claim: KeeperClaim | None, dropped_since: bool = False
) -> bool:
    """Whether the $5 tax applies this season.

    Three rules live here, and all three are easy to get wrong:

    * A **prospect** keep does not count. A player kept in the PROSPECT slot starts next
      season untaxed.
    * A **trade** does not clear it. Tax is a property of the player's history, not of who
      holds him now, so this function deliberately takes no "was traded" argument.
    * A **drop** clears it completely, and the manager who re-adds him starts from the new
      acquisition value.
    """
    if prior_claim is None or dropped_since:
        return False
    return is_keeper_slot(prior_claim.slot)


def check_base_continuity(
    roster: Sequence[RosterEntry],
    prior_claims: Sequence[KeeperClaim],
    overrides: Iterable[SalaryOverride] = (),
) -> list[ValidationIssue]:
    """Audit the ratchet: this season's base must equal last season's computed salary.

    ESPN carries a keeper's salary forward because keepers enter the auction at their keeper
    price. That means we can *verify* it. A mismatch is a keeper mis-entered at auction, or a
    draft-cash distortion that never got reverted — either one silently corrupts the player's
    price for every season after, not just this one.

    REVIEW severity, never blocking: the usual fix is recording an override, which is a
    commissioner judgement call.

    Returns no issues when ``prior_claims`` is empty — before the history backfill there is
    nothing to compare against, and a wall of false alarms would teach everyone to ignore it.
    """
    if not prior_claims:
        return []

    priced = {
        claim.espn_player_id: claim
        for claim in prior_claims
        if claim.computed_salary is not None
    }
    issues: list[ValidationIssue] = []
    for entry in roster:
        if not entry.kept_prior_year:
            continue
        prior = priced.get(entry.espn_player_id)
        if prior is None:
            continue
        actual = effective_base_salary(entry, overrides)
        if actual != prior.computed_salary:
            issues.append(
                ValidationIssue(
                    code=IssueCode.BASE_DISCONTINUITY,
                    severity=Severity.REVIEW,
                    message=(
                        f"base is ${actual} but last season's computed salary was "
                        f"${prior.computed_salary} — auction entry error, or an "
                        f"un-reverted draft-cash override needs recording"
                    ),
                    manager_id=entry.manager_id,
                    espn_player_id=entry.espn_player_id,
                )
            )
    return issues


def check_override_balance(
    roster: Sequence[RosterEntry], overrides: Sequence[SalaryOverride]
) -> list[ValidationIssue]:
    """Audit draft-cash trades: live overrides should net to zero across the league.

    A cash trade moves money between two teams, so the commissioner nudges one player up on
    one roster and another down on the other. If the deltas don't cancel, a leg was never
    entered — which is exactly what happened to the counterparty of Saquon Barkley's +$3.

    Rows flagged ``unpaired_ok`` are excluded, so a known-unrecoverable orphan doesn't cry
    wolf forever. REVIEW severity, never blocking.

    **This is the weak check, and it only covers what the strong one cannot.** Netting the
    whole league cannot tell one balanced trade from two unrelated mistakes that cancel, and
    when it does fail it names every live player at once. A row carrying a ``trade_id`` is
    audited by ``check_cash_trades`` against its own trade's declared amount and parties
    instead, and is excluded here — reporting it twice would make this message a duplicate
    that moves whenever an unrelated trade changes.
    """
    bases = {(entry.espn_player_id, entry.season): entry.base_salary for entry in roster}
    live = [
        override
        for override in overrides
        if not override.reverted
        and not override.unpaired_ok
        and not override.trade_ids
        and (override.espn_player_id, override.season) in bases
    ]
    if not live:
        return []

    net = sum(
        override.actual_salary - bases[(override.espn_player_id, override.season)]
        for override in live
    )
    if net == 0:
        return []

    players = ", ".join(str(override.espn_player_id) for override in live)
    return [
        ValidationIssue(
            code=IssueCode.OVERRIDES_UNBALANCED,
            severity=Severity.REVIEW,
            message=(
                f"live salary overrides net to ${net:+d}, expected $0 — a draft-cash trade "
                f"is missing a leg (players: {players})"
            ),
        )
    ]


def trade_groups(
    trades: Sequence[CashTrade],
    legs_of: Mapping[str, list[SalaryOverride]] | None = None,
    overrides: Sequence[SalaryOverride] = (),
) -> list[list[CashTrade]]:
    """Trades that share a salary edit, grouped together.

    Two trades belong together when one override is a leg of both — which is what netting
    produces. Union-find over the trade ids; a trade nothing is netted with comes back as a
    group of one, which is why the ordinary case needs no special handling anywhere below.
    """
    if legs_of is None:
        legs_of = {}
        for override in overrides:
            for trade_id in override.trade_ids:
                legs_of.setdefault(trade_id, []).append(override)
    parent = {trade.id: trade.id for trade in trades}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for legs in legs_of.values():
        shared = [tid for leg in legs for tid in leg.trade_ids if tid in parent]
        for other in shared[1:]:
            parent[find(shared[0])] = find(other)

    grouped: dict[str, list[CashTrade]] = {}
    for trade in sorted(trades, key=lambda t: (t.draft_year, t.id)):
        grouped.setdefault(find(trade.id), []).append(trade)
    return list(grouped.values())


def check_cash_trades(
    roster: Sequence[RosterEntry],
    overrides: Sequence[SalaryOverride],
    trades: Sequence[CashTrade],
) -> list[ValidationIssue]:
    """Audit draft-cash trades against the salary edits that express them.

    **The unit of audit is a franchise's net, over a group of trades that share edits.** One
    salary edit routinely covers several trades — a franchise owing $1 from one deal and $2
    from another gets a single $3 edit, and a franchise that both pays and receives $5 gets no
    edit at all. Once that netting happens, per-trade balancing is impossible in principle:
    nothing records which dollar belonged to which deal, and inventing a split would be
    recording a number nobody decided.

    What is exactly checkable survives::

        expected[manager] = sum over the group's trades of
                            (-amount to from_manager_id, +amount to to_manager_id)
        actual[manager]   = sum of that manager's live legs' (actual_salary - espn_base)

    A trade netted with nothing is a group of one, and then ``expected`` is ``{payer: -amount,
    payee: +amount}`` — exactly the two-sided check this replaced. Nothing got weaker for the
    simple case; the netted case went from unrepresentable to checked.

    The sign convention is unchanged and is still the thing to get right: ESPN **under**-charges
    the receiving side, freeing that much auction budget, so a receiving leg's delta is positive
    and a paying leg's is negative.

    Everything reported is REVIEW and nothing blocks — consistent with ``check_override_balance``
    and deliberate, because rows predating the trades file are legitimately unlinked and a fresh
    ERROR would turn the existing record red.

    * a trade nothing points at — declared, but never expressed in ESPN
    * a leg whose ESPN base is unknown, so no sum can be formed. **Reported, not skipped**: the
      remaining legs would otherwise total to something that looks like an answer
    * a leg on a franchise that is party to none of the group's trades
    * a leg filed under a different draft than the trades it names. A trade is expressed at the
      auction it spends its money at, so the two are the same year or one of them is wrong
    * a franchise whose edits do not net to what its trades say it owes, including the
      half-reverted case — one leg put back and one left live is a distortion the ratchet then
      carries forward every year
    * a live override attached to no trade at all, or naming one that is not on file

    A group whose legs are **all** reverted is finished, not broken: ESPN has been put back and
    that is the intended end state.
    """
    entries = {(entry.espn_player_id, entry.season): entry for entry in roster}
    legs_of: dict[str, list[SalaryOverride]] = {}
    for override in overrides:
        for trade_id in override.trade_ids:
            legs_of.setdefault(trade_id, []).append(override)

    issues: list[ValidationIssue] = []
    for group in trade_groups(trades, legs_of):
        ids = [trade.id for trade in group]
        label = ids[0] if len(ids) == 1 else " + ".join(ids)
        # Deduplicated: one override netting two of the group's trades is still one edit.
        legs = {
            (leg.espn_player_id, leg.season, leg.created_at): leg
            for tid in ids
            for leg in legs_of.get(tid, [])
        }
        if not legs:
            for trade in group:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.CASH_TRADE_NO_LEGS,
                        severity=Severity.REVIEW,
                        message=(
                            f"trade {trade.id} declares ${trade.amount} from "
                            f"{trade.from_manager_id} to {trade.to_manager_id}, but no salary "
                            f"override points at it — nothing expresses it in ESPN"
                        ),
                        trade_id=trade.id,
                    )
                )
            continue

        live = [leg for leg in legs.values() if not leg.reverted]
        if not live:
            continue  # Every side put back. Finished, not broken.

        parties: set[str] = set()
        expected: dict[str, int] = {}
        for trade in group:
            parties |= {trade.from_manager_id, trade.to_manager_id}
            expected[trade.from_manager_id] = (
                expected.get(trade.from_manager_id, 0) - trade.amount
            )
            expected[trade.to_manager_id] = expected.get(trade.to_manager_id, 0) + trade.amount

        drafts = {trade.draft_year for trade in group}
        actual: dict[str, int] = {manager: 0 for manager in parties}
        unknown = False
        for leg in sorted(live, key=lambda o: (o.season, o.espn_player_id)):
            entry = entries.get((leg.espn_player_id, leg.season))
            if entry is None:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.CASH_TRADE_UNCHECKABLE,
                        severity=Severity.REVIEW,
                        message=(
                            f"{label} cannot be balanced: ESPN has no {leg.season} base for "
                            f"player {leg.espn_player_id}, so that leg's dollar movement is "
                            f"unknown and the rest would not add up to an answer"
                        ),
                        espn_player_id=leg.espn_player_id,
                        trade_id=ids[0],
                    )
                )
                unknown = True
                continue
            if leg.season not in drafts:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.CASH_TRADE_LEG_WRONG_DRAFT,
                        severity=Severity.REVIEW,
                        message=(
                            f"{label} is for the {sorted(drafts)} draft, but its leg on player "
                            f"{leg.espn_player_id} distorts a {leg.season} price — a trade is "
                            f"expressed at the auction it spends its money at, so the two are "
                            f"the same year or one of them is wrong"
                        ),
                        espn_player_id=leg.espn_player_id,
                        trade_id=ids[0],
                    )
                )
                unknown = True
                continue
            if entry.manager_id not in parties:
                issues.append(
                    ValidationIssue(
                        code=IssueCode.CASH_TRADE_STRANGER_LEG,
                        severity=Severity.REVIEW,
                        message=(
                            f"{label} is between {', '.join(sorted(parties))}, but player "
                            f"{leg.espn_player_id} is on {entry.manager_id} — a cash trade is "
                            f"only ever expressed on the teams that agreed it"
                        ),
                        manager_id=entry.manager_id,
                        espn_player_id=leg.espn_player_id,
                        trade_id=ids[0],
                    )
                )
                unknown = True
                continue
            actual[entry.manager_id] += leg.actual_salary - entry.base_salary

        if unknown:
            continue

        off = {
            manager: actual.get(manager, 0) - owed
            for manager, owed in expected.items()
            if actual.get(manager, 0) != owed
        }
        if not off:
            continue

        half_reverted = len(live) != len(legs)
        because = (
            " — some legs are marked reverted and some are not, which leaves the distortion "
            "live and carries it into next season's base"
            if half_reverted
            else ""
        )
        detail = ", ".join(
            f"{manager} is ${amount:+d} against an expected ${expected[manager]:+d}"
            for manager, amount in sorted(off.items())
        )
        issues.append(
            ValidationIssue(
                code=IssueCode.CASH_TRADE_UNBALANCED,
                severity=Severity.REVIEW,
                message=(
                    f"{label}: the salary edits do not net to what the trades say is owed — "
                    f"{detail}{because}"
                ),
                trade_id=ids[0],
            )
        )

    known = {trade.id for trade in trades}
    for override in sorted(overrides, key=lambda o: (o.season, o.espn_player_id)):
        if override.reverted or override.unpaired_ok:
            continue
        if not override.trade_ids:
            issues.append(
                ValidationIssue(
                    code=IssueCode.OVERRIDE_NOT_ON_A_TRADE,
                    severity=Severity.REVIEW,
                    message=(
                        f"the live {override.season} override for player "
                        f"{override.espn_player_id} is not attached to a cash trade, so it can "
                        f"only be checked by netting the whole league — record the trade it is "
                        f"a leg of, or flag it unpaired_ok if the counterparty is unrecoverable"
                    ),
                    espn_player_id=override.espn_player_id,
                )
            )
        for dangling in sorted(set(override.trade_ids) - known):
            issues.append(
                ValidationIssue(
                    code=IssueCode.OVERRIDE_NOT_ON_A_TRADE,
                    severity=Severity.REVIEW,
                    message=(
                        f"the live {override.season} override for player "
                        f"{override.espn_player_id} names trade {dangling}, which is not on file"
                    ),
                    espn_player_id=override.espn_player_id,
                    trade_id=dangling,
                )
            )
    return issues


def _ordinal(n: int) -> str:
    """``2 -> 'second'``. For the prospect message, which a manager reads and acts on.

    Falls back to digits past the range that ever comes up, because a message is not worth
    a lookup table and "his 14th-year" reads fine.
    """
    words = {2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth"}
    return words.get(n, f"{n}th")


def validate_team_claims(
    claims: Sequence[KeeperClaim],
    roster: Sequence[RosterEntry],
    *,
    fees_waived: bool = False,
    first_nfl_season: Mapping[int, int] | None = None,
    trade_deadline: datetime | None = None,
    prior_prospect_ids: Collection[int] = (),
) -> list[ValidationIssue]:
    """Everything the spreadsheet's ``VALID`` column used to mean, and then some.

    ``first_nfl_season`` maps ``espn_player_id`` to the season that player's NFL career began,
    for prospect rule 1 — **a prospect must be a rookie**. It comes from
    ``data/derived/player-origins.json``, which ``rs57.origins_sync`` fills from ESPN's core
    API; ``draft.year`` is the draft class and is immutable.

    **The arithmetic, because it is easy to get wrong and it fails quietly.** A prospect is
    kept *from the season just completed*, so the count that matters runs to ``claim.season - 1``
    inclusive::

        elapsed = claim.season - first_nfl_season      # NOT ... + 1

    Cam Skattebo began in 2025 and was kept as a 2026 prospect: ``2026 - 2025 == 1``, legal.
    Add one and that becomes 2, and the check rejects the single claim we have the best
    evidence for. This must agree with ``trade_deadline``, which the caller passes for the same
    ``claim.season - 1`` — if the two ever judge different seasons they will disagree about the
    same player.

    **The mapping's presence is itself the switch**, following the convention
    ``sync.prior_prospect_ids`` set (``None`` means unknown, empty means checked-and-none):

    * ``None``  — rule 1 is not being applied at all. No issue either way.
    * a mapping, **even an empty one** — rule 1 *is* being applied, and a prospect who is not
      in it yields ``PROSPECT_ROOKIE_UNVERIFIED`` at REVIEW. ESPN could not answer for him, so
      the rule could not run, and a check that cannot run is reported rather than passed.

    Rule 1 is REVIEW rather than ERROR **by decision** (commissioner, 2026-08-01): the draft
    class comes from outside the league, so the final call stays with a human who can overrule
    ESPN or record a voted exception. Contrast ``PROSPECT_REPEAT_CLAIM`` below, which stays
    ERROR — *the league's own record blocks; an outside data source flags.*

    ``prior_prospect_ids`` is the older, indirect form of the same rule, and it stays. A player
    has exactly one rookie year, so a repeat prospect claim is a rookie-rule violation
    detectable from the league's own claims with no external source at all. It is now a
    cross-check on rule 1 rather than a substitute for it.

    Pass ``prior_prospect_ids`` only for seasons the *current* rule governs. The league once
    allowed second-year players to be prospected too, so the same player legitimately appears
    twice in the historical record — see ``PROSPECT_RULES_TIGHTENED``. The threshold is the
    caller's to know, which is why this takes the ids rather than deriving them.
    """
    # Deliberately not ``or {}``: an empty mapping is not the same as no mapping, and
    # collapsing them would turn "we checked and know nothing" into "we are not checking".
    rule_one_applies = first_nfl_season is not None
    first_nfl_season = first_nfl_season if first_nfl_season is not None else {}
    manager_id = claims[0].manager_id if claims else None
    issues: list[ValidationIssue] = []

    keepers = [claim for claim in claims if is_keeper_slot(claim.slot)]
    prospects = [claim for claim in claims if claim.slot is KeeperSlot.PROSPECT]
    rostered = {entry.espn_player_id: entry for entry in roster}

    if len(keepers) > MAX_KEEPERS:
        issues.append(
            ValidationIssue(
                IssueCode.TOO_MANY_KEEPERS,
                Severity.ERROR,
                f"{len(keepers)} keepers claimed, max is {MAX_KEEPERS}",
                manager_id=manager_id,
            )
        )
    if len(prospects) > MAX_PROSPECTS:
        issues.append(
            ValidationIssue(
                IssueCode.TOO_MANY_PROSPECTS,
                Severity.ERROR,
                f"{len(prospects)} prospects claimed, max is {MAX_PROSPECTS}",
                manager_id=manager_id,
            )
        )

    seen_slots: set[KeeperSlot] = set()
    seen_players: set[int] = set()
    for claim in claims:
        if claim.slot in seen_slots:
            issues.append(
                ValidationIssue(
                    IssueCode.DUPLICATE_SLOT,
                    Severity.ERROR,
                    f"slot {claim.slot} claimed more than once",
                    manager_id=claim.manager_id,
                    espn_player_id=claim.espn_player_id,
                    slot=claim.slot,
                )
            )
        seen_slots.add(claim.slot)

        if claim.espn_player_id in seen_players:
            issues.append(
                ValidationIssue(
                    IssueCode.DUPLICATE_PLAYER,
                    Severity.ERROR,
                    f"player {claim.espn_player_id} claimed in more than one slot",
                    manager_id=claim.manager_id,
                    espn_player_id=claim.espn_player_id,
                    slot=claim.slot,
                )
            )
        seen_players.add(claim.espn_player_id)

        if claim.fee_allocated < 0:
            issues.append(
                ValidationIssue(
                    IssueCode.NEGATIVE_FEE,
                    Severity.ERROR,
                    f"fee of ${claim.fee_allocated} is negative",
                    manager_id=claim.manager_id,
                    espn_player_id=claim.espn_player_id,
                    slot=claim.slot,
                )
            )

        entry = rostered.get(claim.espn_player_id)
        if entry is None:
            issues.append(
                ValidationIssue(
                    IssueCode.PLAYER_NOT_ON_ROSTER,
                    Severity.ERROR,
                    f"player {claim.espn_player_id} was not on this roster at season end",
                    manager_id=claim.manager_id,
                    espn_player_id=claim.espn_player_id,
                    slot=claim.slot,
                )
            )

    for claim in prospects:
        if claim.fee_allocated != 0:
            issues.append(
                ValidationIssue(
                    IssueCode.FEE_ON_PROSPECT,
                    Severity.ERROR,
                    f"prospect allocated ${claim.fee_allocated}; prospects take no fees",
                    manager_id=claim.manager_id,
                    espn_player_id=claim.espn_player_id,
                    slot=claim.slot,
                )
            )

        if rule_one_applies:
            began = first_nfl_season.get(claim.espn_player_id)
            if began is None:
                issues.append(
                    ValidationIssue(
                        IssueCode.PROSPECT_ROOKIE_UNVERIFIED,
                        Severity.REVIEW,
                        "ESPN has no draft class or debut year on record for this player, so "
                        "prospect rule 1 (must be a rookie) could not be checked",
                        manager_id=claim.manager_id,
                        espn_player_id=claim.espn_player_id,
                        slot=claim.slot,
                    )
                )
            else:
                # Kept FROM the season just completed, so the count runs to claim.season - 1
                # inclusive: (claim.season - 1) - began + 1. Not claim.season - began + 1,
                # which rejects a genuine rookie.
                elapsed = claim.season - began
                if elapsed > 1:
                    issues.append(
                        ValidationIssue(
                            IssueCode.PROSPECT_TOO_MANY_SEASONS,
                            Severity.REVIEW,
                            f"first NFL season was {began}, so he was not a rookie in "
                            f"{claim.season - 1} — {_ordinal(elapsed)}-year player",
                            manager_id=claim.manager_id,
                            espn_player_id=claim.espn_player_id,
                            slot=claim.slot,
                        )
                    )
                elif elapsed < 1:
                    issues.append(
                        ValidationIssue(
                            IssueCode.PROSPECT_ROOKIE_UNVERIFIED,
                            Severity.REVIEW,
                            f"first NFL season is recorded as {began}, which is not before "
                            f"{claim.season} — he could not have been on a {claim.season - 1} "
                            f"roster, so this is bad data rather than a bad claim",
                            manager_id=claim.manager_id,
                            espn_player_id=claim.espn_player_id,
                            slot=claim.slot,
                        )
                    )

        entry = rostered.get(claim.espn_player_id)
        if entry is not None and trade_deadline is not None:
            if entry.acquired_at > trade_deadline:
                issues.append(
                    ValidationIssue(
                        IssueCode.PROSPECT_ACQUIRED_AFTER_DEADLINE,
                        Severity.ERROR,
                        f"prospect acquired {entry.acquired_at:%Y-%m-%d}, after the "
                        f"{trade_deadline:%Y-%m-%d} trade deadline",
                        manager_id=claim.manager_id,
                        espn_player_id=claim.espn_player_id,
                        slot=claim.slot,
                    )
                )

        if claim.espn_player_id in prior_prospect_ids:
            issues.append(
                ValidationIssue(
                    IssueCode.PROSPECT_REPEAT_CLAIM,
                    Severity.ERROR,
                    "player was kept as a prospect in an earlier season, so he is not a "
                    "rookie now",
                    manager_id=claim.manager_id,
                    espn_player_id=claim.espn_player_id,
                    slot=claim.slot,
                )
            )

    # Only price a legal number of keepers. Above the cap the tier is undefined, and
    # TOO_MANY_KEEPERS above already says what's wrong.
    if len(keepers) <= MAX_KEEPERS:
        expected = fee_total_for(len(keepers), fees_waived)
        allocated = sum(claim.fee_allocated for claim in keepers)
        if allocated != expected:
            waived = " (fees waived: consolation winner)" if fees_waived else ""
            issues.append(
                ValidationIssue(
                    IssueCode.FEE_TOTAL_MISMATCH,
                    Severity.ERROR,
                    f"fees total ${allocated}, expected ${expected} for "
                    f"{len(keepers)} keepers{waived}",
                    manager_id=manager_id,
                )
            )

    return issues


def compute_team_keepers(
    claims: Sequence[KeeperClaim],
    roster: Sequence[RosterEntry],
    overrides: Sequence[SalaryOverride] = (),
    *,
    manager_id: str | None = None,
    fees_waived: bool = False,
    first_nfl_season: Mapping[int, int] | None = None,
    trade_deadline: datetime | None = None,
    prior_prospect_ids: Collection[int] = (),
) -> TeamKeeperResult:
    """Price one team's keeper claims and validate them in a single pass.

    Salaries are computed for every claim that names a rostered player, even when the team has
    blocking errors — an admin screen showing "you owe $5 more in fees" is far more useful
    alongside the salaries than instead of them.
    """
    issues = validate_team_claims(
        claims,
        roster,
        fees_waived=fees_waived,
        first_nfl_season=first_nfl_season,
        trade_deadline=trade_deadline,
        prior_prospect_ids=prior_prospect_ids,
    )

    rostered = {entry.espn_player_id: entry for entry in roster}
    computed: list[ComputedKeeper] = []
    for claim in claims:
        entry = rostered.get(claim.espn_player_id)
        if entry is None:
            continue
        base = effective_base_salary(entry, overrides)
        computed.append(
            ComputedKeeper(
                espn_player_id=claim.espn_player_id,
                slot=claim.slot,
                base_salary=base,
                fee_allocated=claim.fee_allocated,
                kept_prior_year=entry.kept_prior_year,
                salary=keeper_salary(
                    base, claim.fee_allocated, entry.kept_prior_year, claim.slot
                ),
            )
        )

    resolved_manager = manager_id or (claims[0].manager_id if claims else "")
    return TeamKeeperResult(
        manager_id=resolved_manager,
        keepers=tuple(computed),
        issues=tuple(issues),
    )
