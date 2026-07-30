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

from rs57.models import KeeperClaim, KeeperSlot, RosterEntry, SalaryOverride

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
pass ``prior_prospect_ids`` only for seasons from here on.

Set to 2026 because the 2025 declarations still include a second-year prospect. If the change
actually landed mid-2025, this is the one line to move.
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
    PROSPECT_ACQUIRED_AFTER_DEADLINE = "prospect_acquired_after_deadline"
    PROSPECT_REPEAT_CLAIM = "prospect_repeat_claim"
    BASE_DISCONTINUITY = "base_discontinuity"
    OVERRIDES_UNBALANCED = "overrides_unbalanced"


@dataclass(frozen=True)
class ValidationIssue:
    code: IssueCode
    severity: Severity
    message: str
    manager_id: str | None = None
    espn_player_id: int | None = None
    slot: KeeperSlot | None = None


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
    """
    bases = {(entry.espn_player_id, entry.season): entry.base_salary for entry in roster}
    live = [
        override
        for override in overrides
        if not override.reverted
        and not override.unpaired_ok
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


def validate_team_claims(
    claims: Sequence[KeeperClaim],
    roster: Sequence[RosterEntry],
    *,
    fees_waived: bool = False,
    seasons_played: Mapping[int, int] | None = None,
    trade_deadline: datetime | None = None,
    prior_prospect_ids: Collection[int] = (),
) -> list[ValidationIssue]:
    """Everything the spreadsheet's ``VALID`` column used to mean, and then some.

    ``seasons_played`` maps ``espn_player_id`` to NFL seasons played, for prospect rule 1 —
    **a prospect must be a rookie**. Nothing in ``data/`` populates it: ESPN's player payload
    carries no rookie year and no seasons-played field, so rule 1 cannot be checked directly.

    ``prior_prospect_ids`` is what makes it enforceable anyway. A player has exactly one rookie
    year, so **a repeat prospect claim is a rookie-rule violation you can actually detect**:
    if he was a prospect last season he is not a rookie this one. That indirection is the only
    reason the check exists, and it is why it stays even though ``seasons_played`` never
    arrives.

    Pass ``prior_prospect_ids`` only for seasons the *current* rule governs. The league once
    allowed second-year players to be prospected too, so the same player legitimately appears
    twice in the historical record — see ``PROSPECT_RULES_TIGHTENED``. The threshold is the
    caller's to know, which is why this takes the ids rather than deriving them.
    """
    seasons_played = seasons_played or {}
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

        played = seasons_played.get(claim.espn_player_id)
        if played is not None and played > 1:
            issues.append(
                ValidationIssue(
                    IssueCode.PROSPECT_TOO_MANY_SEASONS,
                    Severity.ERROR,
                    f"prospect has played {played} NFL seasons, max is 1",
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
    seasons_played: Mapping[int, int] | None = None,
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
        seasons_played=seasons_played,
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
