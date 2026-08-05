"""Compare what is recorded here against what ESPN actually holds. **Read only, both ways.**

Why this screen exists. A declared keeper has to be entered into ESPN's auction at his keeper
price, by hand, and ESPN then reports that number back as the player's base next season —
verified against the live league: every 2025 keeper pick's ``bidAmount`` equals that player's
2026 ``keeperValue`` to the dollar. So a mistyped ESPN entry does not stay a typo. It becomes
the player's base, and the ratchet carries it forward every year after.

``check_base_continuity`` catches that a year later. This catches it the same week.

What ESPN can and cannot tell us
--------------------------------

* **After keepers are entered**, ``mDraftDetail`` holds them: 2025 returns 33 picks with
  ``keeper: true`` and a ``bidAmount`` that is the price charged.
* **Before then, there is nothing to read.** Probed on 2026 with a keeper freshly selected in
  the ESPN UI: 180 pick slots all at ``playerId: -1``, no ``keeper`` or ``reservedForKeeper``
  flag anywhere, and no keeper field on any of the 12 rosters beyond ``keeperValue`` /
  ``keeperValueFuture``. Pre-deadline selections are not in the public API — most likely
  because ESPN hides them from other managers until the deadline. This module therefore reports
  "ESPN has no keeper picks yet" as a normal state, not an error.
* **ESPN never says which claim is the prospect.** ``draftSettings.keeperCount`` is 4 and all
  four picks come back ``keeper: true`` with nothing recording the slot. That gap is what taxed
  a prospect $5 in the old spreadsheet, and it is why the slot is entered by a human here and
  cannot ever be imported.

So ESPN is downstream of this tool, never upstream of it. Nothing in this module writes a
claim; it produces rows for a human to read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from rs57.admin.derived import DerivedSeason
from rs57.espn import EspnClient, EspnError, check_roster_sizes
from rs57.models import KeeperClaim


@dataclass(frozen=True)
class EspnKeeperPick:
    """One keeper as ESPN holds it. ``bid`` is what ESPN charged for him."""

    espn_team_id: int
    espn_player_id: int
    bid: int

    @property
    def manager_id(self) -> str:
        return f"t{self.espn_team_id}"


@dataclass(frozen=True)
class ReconcileRow:
    """One player, as recorded here and as ESPN holds him.

    ``claimed`` is what separates "nobody recorded him" from "somebody recorded him at no
    price". Both arrive here with ``recorded_salary is None`` and they are not the same fact.
    """

    manager_id: str
    name: str
    espn_player_id: int
    recorded_salary: int | None
    espn_value: int | None
    """What ESPN holds for this player — the auction ``bidAmount`` once a season has drafted,
    and the roster's keeper value before it. The same number either way: what the player will
    be charged. Named for the fact rather than for whichever endpoint supplied it."""
    slot: str
    claimed: bool = False
    """True when a claim on file names this player, whether or not it carries a price."""
    base_at_sync: int | None = None
    """What ESPN held for him when the season was last synced — the price he carried IN.

    Present so the screen can tell "ESPN disagrees" from "ESPN has not been told yet", which
    look identical if you only compare against the recorded salary.
    """

    @property
    def state(self) -> str:
        """What this row is, in the order the cases have to be distinguished.

        ``unpriced`` comes before ``espn_only``, and that ordering is the whole fix.
        ``KeeperClaim.computed_salary`` is optional on purpose — ``compute_team_keepers``
        skips a claim naming a player who is no longer rostered, which is exactly the
        situation somebody reconciles in. Testing ``recorded_salary is None`` first read that
        claim as absent and told the commissioner "ESPN has a keeper you never recorded"
        about a keeper sitting in ``claims.json``. Where a number cannot be known, record no
        number — and say that is what happened, rather than inventing a different finding.
        """
        if self.recorded_salary is None:
            return "unpriced" if self.claimed else "espn_only"
        if self.espn_value is None:
            return "not_in_espn"
        if self.recorded_salary == self.espn_value:
            return "agrees"
        if self.base_at_sync is not None and self.espn_value == self.base_at_sync:
            # ESPN still holds the price he carried in, unchanged since the last sync. That is
            # not a disagreement about his salary — it is the salary not having been entered
            # yet, which is the normal state until step 6 of the offseason.
            #
            # Worth separating, because the difference is exactly the player's fee plus tax on
            # every keeper at once. A screen reporting the whole league red before anybody has
            # typed anything into ESPN is a screen that gets scrolled past, and the mismatch it
            # exists to catch goes with it.
            return "not_yet_entered"
        return "mismatch"

    @property
    def delta(self) -> int | None:
        if self.recorded_salary is None or self.espn_value is None:
            return None
        return self.espn_value - self.recorded_salary


@dataclass(frozen=True)
class Reconciliation:
    season: int
    rows: tuple[ReconcileRow, ...]
    espn_pick_count: int
    error: str | None = None

    @property
    def mismatches(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "mismatch")

    @property
    def missing_from_espn(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "not_in_espn")

    @property
    def espn_only(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "espn_only")

    @property
    def unpriced(self) -> tuple[ReconcileRow, ...]:
        """Claims carrying no ``computed_salary``. Nothing to compare, so nothing was compared."""
        return tuple(row for row in self.rows if row.state == "unpriced")

    @property
    def agrees(self) -> bool:
        """True only when every row was actually compared and every comparison passed.

        An unpriced row counts against it. A check that could not run is not a check that
        passed — silence reads exactly like success, which is the rule the validator is built
        around, and it applies just as much to a screen.
        """
        return not (
            self.mismatches or self.missing_from_espn or self.espn_only or self.unpriced
        )


def keeper_picks(payload: dict) -> list[EspnKeeperPick]:
    """Pull the keeper picks out of an ``mDraftDetail`` payload.

    Accepts both markers. ``keeper`` is what a completed draft sets; ``reservedForKeeper`` is
    the pre-draft field, which was False on every 2025 pick and has never been observed set —
    it is read anyway so that the day ESPN does populate it, this screen already works.
    """
    picks = []
    for pick in payload.get("picks") or []:
        if not (pick.get("keeper") or pick.get("reservedForKeeper")):
            continue
        player_id = pick.get("playerId", -1)
        team_id = pick.get("teamId", -1)
        if player_id == -1 or team_id == -1:
            continue
        picks.append(
            EspnKeeperPick(
                espn_team_id=int(team_id),
                espn_player_id=int(player_id),
                bid=int(pick.get("bidAmount") or 0),
            )
        )
    return picks


def reconcile(
    season: int,
    claims: list[KeeperClaim],
    picks: list[EspnKeeperPick],
    current: DerivedSeason,
    *,
    error: str | None = None,
) -> Reconciliation:
    """Diff recorded claims against ESPN's keeper picks, player by player.

    Matched on ``espn_player_id`` and never on name — the spreadsheet this replaces
    under-charges James Cook by $5 because it matched ``James Cook`` against a feed that now
    says ``James Cook III``.
    """
    names = current.player_by_id
    base_at_sync = {
        (entry.manager_id, entry.espn_player_id): entry.base_salary for entry in current.roster
    }
    by_player: dict[tuple[str, int], ReconcileRow] = {}

    for claim in claims:
        player = names.get(claim.espn_player_id)
        key = (claim.manager_id, claim.espn_player_id)
        by_player[key] = ReconcileRow(
            manager_id=claim.manager_id,
            name=player.name if player else f"player {claim.espn_player_id}",
            espn_player_id=claim.espn_player_id,
            recorded_salary=claim.computed_salary,
            espn_value=None,
            slot=str(claim.slot),
            claimed=True,
            base_at_sync=base_at_sync.get(key),
        )

    for pick in picks:
        key = (pick.manager_id, pick.espn_player_id)
        existing = by_player.get(key)
        if existing is not None:
            by_player[key] = ReconcileRow(
                manager_id=existing.manager_id,
                name=existing.name,
                espn_player_id=existing.espn_player_id,
                recorded_salary=existing.recorded_salary,
                espn_value=pick.bid,
                slot=existing.slot,
                claimed=existing.claimed,
                base_at_sync=existing.base_at_sync,
            )
            continue
        player = names.get(pick.espn_player_id)
        by_player[key] = ReconcileRow(
            manager_id=pick.manager_id,
            name=player.name if player else f"player {pick.espn_player_id}",
            espn_player_id=pick.espn_player_id,
            recorded_salary=None,
            espn_value=pick.bid,
            slot="",
            base_at_sync=base_at_sync.get(key),
        )

    rows = sorted(
        by_player.values(),
        key=lambda row: (len(row.manager_id), row.manager_id, row.slot, row.name),
    )
    return Reconciliation(
        season=season, rows=tuple(rows), espn_pick_count=len(picks), error=error
    )


@dataclass(frozen=True)
class Verification:
    """One run of "does ESPN hold what we recorded".

    Two questions, and only one of them can always be answered.

    * **Does every recorded claim carry the salary we computed?** A roster read answers this
      whenever ESPN is reachable. It is the check that matters: a mistyped keeper price becomes
      the player's base and the ratchet carries it forward every year after.
    * **Did somebody keep a player we never recorded?** Answerable only when ESPN has pruned
      the rosters to the kept players. On a full roster every player carries a keeper value
      whether he is a keeper or not, so there is nothing in the payload that distinguishes one.

    ``unrecorded_checked`` is False for the second case and the screen says so. Reporting it as
    clean would be a check that never ran wearing the face of one that passed.
    """

    season: int
    rows: tuple[ReconcileRow, ...]
    regime: str | None
    """``"full"``, ``"keepers"``, or ``None`` when ESPN could not be read."""
    unrecorded_checked: bool
    draft_picks_seen: int
    error: str | None = None

    @property
    def mismatches(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "mismatch")

    @property
    def missing_from_espn(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "not_in_espn")

    @property
    def espn_only(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "espn_only")

    @property
    def unpriced(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "unpriced")

    @property
    def not_yet_entered(self) -> tuple[ReconcileRow, ...]:
        """Recorded here, but ESPN still holds the price the player carried in.

        The ordinary state until the salaries are typed into ESPN. Not a disagreement, and not
        a pass either — ESPN does not hold the number yet, so nothing has been confirmed.
        """
        return tuple(row for row in self.rows if row.state == "not_yet_entered")

    @property
    def checked(self) -> tuple[ReconcileRow, ...]:
        return tuple(row for row in self.rows if row.state == "agrees")

    @property
    def clean(self) -> bool:
        """Every row compared, every comparison passed, and ESPN was actually reachable.

        Deliberately not "no mismatches". An error, an unpriced claim or a claim ESPN has no
        value for all mean something went unchecked, and silence reads exactly like success.
        """
        return not (
            self.error
            or self.mismatches
            or self.missing_from_espn
            or self.espn_only
            or self.unpriced
            or self.not_yet_entered
        )


def roster_salaries(payload: dict, espn_team_id: int, field: str) -> tuple[list[EspnKeeperPick], int]:
    """Every player on one team's roster with the salary ESPN currently holds for him.

    Returns rows plus the roster's own depth, because depth is what says whether ESPN has
    pruned the league to its keepers — see ``espn.check_roster_sizes``.

    ``field`` is ``keeperValue`` or ``keeperValueFuture`` and is **not** decided here. It comes
    from the derived season's recorded ``base_salary_field``, so the verify compares against the
    same field the rest of the pipeline read. Choosing it in a second place is how the two drift
    apart, and getting it wrong does not fail loudly — it reports the whole league as mismatched,
    or reports it clean having compared each number against itself.
    """
    entries = ((payload.get("teams") or [{}])[0].get("roster") or {}).get("entries") or []
    rows = []
    for entry in entries:
        pool = entry.get("playerPoolEntry") or {}
        player_id = (pool.get("player") or {}).get("id")
        value = pool.get(field)
        if player_id is None or value is None:
            continue
        rows.append(
            EspnKeeperPick(
                espn_team_id=espn_team_id, espn_player_id=int(player_id), bid=int(value)
            )
        )
    return rows, len(entries)


def fetch_roster_salaries(
    season: int, field: str, espn_team_ids: Sequence[int]
) -> tuple[list[EspnKeeperPick], dict[int, int], str | None]:
    """Read every team's live roster. The same call ``rs57.sync`` makes, and it writes nothing.

    The error comes back as a value rather than an exception because an unreachable ESPN is a
    normal thing for a localhost tool to meet, and it must not take the screen down.
    """
    try:
        client = EspnClient.from_env(season)
        rows: list[EspnKeeperPick] = []
        sizes: dict[int, int] = {}
        for team_id in espn_team_ids:
            team_rows, depth = roster_salaries(client.fetch_roster(team_id), team_id, field)
            rows.extend(team_rows)
            sizes[team_id] = depth
    except EspnError as exc:
        return [], {}, str(exc)
    return rows, sizes, None


def verify(
    season: int,
    claims: list[KeeperClaim],
    salaries: list[EspnKeeperPick],
    sizes: Mapping[int, int],
    current: DerivedSeason,
    *,
    draft_picks: int = 0,
    error: str | None = None,
) -> Verification:
    """Compare recorded claims against the salaries ESPN holds on its rosters.

    Compared against each claim's stored ``computed_salary`` — the frozen record of what the
    manager was told they owed — and never against a fresh recomputation, which would compare a
    number against itself and report a clean result having checked nothing.
    """
    if error is not None:
        return Verification(
            season=season,
            rows=(),
            regime=None,
            unrecorded_checked=False,
            draft_picks_seen=draft_picks,
            error=error,
        )

    try:
        regime = check_roster_sizes(sizes) if sizes else None
    except EspnError as exc:
        return Verification(
            season=season,
            rows=(),
            regime=None,
            unrecorded_checked=False,
            draft_picks_seen=draft_picks,
            error=str(exc),
        )

    # On a full roster every player has a keeper value, so an unclaimed one says nothing. Only
    # a pruned league lets a rostered-but-unrecorded player mean "kept and never entered here".
    pruned = regime == "keepers"
    claimed_ids = {(claim.manager_id, claim.espn_player_id) for claim in claims}
    relevant = [
        row for row in salaries if pruned or (row.manager_id, row.espn_player_id) in claimed_ids
    ]

    reconciled = reconcile(season, claims, relevant, current)
    return Verification(
        season=season,
        rows=reconciled.rows,
        regime=regime,
        unrecorded_checked=pruned,
        draft_picks_seen=draft_picks,
        error=None,
    )


def fetch_keeper_picks(season: int) -> tuple[list[EspnKeeperPick], str | None]:
    """Read ESPN's draft record for ``season``. Returns the picks and any error to display.

    The error comes back as a value rather than an exception because an unreachable ESPN is a
    normal thing for a localhost tool to encounter, and it must not take the screen down.
    """
    try:
        payload = EspnClient.from_env(season).fetch_draft_detail()
    except EspnError as exc:
        return [], str(exc)
    return keeper_picks(payload), None


def _epoch_ms(value: int | None) -> datetime | None:
    """ESPN epoch milliseconds to a **naive UTC** datetime.

    The same conversion ``espn._epoch_ms`` does, and naive UTC for the same reason: the models,
    the fixtures and the derived files are naive throughout, and ``keeper_rules`` compares a
    prospect's ``acquired_at`` against a ``trade_deadline`` directly.

    Local time here would be a real bug rather than a cosmetic one. ``data/derived/2026.json``
    records the trade deadline as ``17:00`` (naive UTC); rendering the same instant as ``12:00``
    Eastern and saving that into ``seasons.json`` would leave two records of one deadline five
    hours apart, and the prospect check reading whichever it happened to be handed.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None)


def fetch_deadlines(season: int) -> tuple[dict[str, datetime | None], str | None]:
    """ESPN's own keeper and trade deadlines for ``season``, as naive UTC datetimes.

    Offered to the settings screen so the deadline is read off the league rather than retyped
    from memory. ``rs57.sync`` does not record ``keeperDeadlineDate`` — widening the derived
    file's shape would be a change to what the nightly Action writes, which is not this phase's
    to make — so the admin tool reads it directly and stores it in ``data/manual/seasons.json``.
    """
    try:
        settings = (EspnClient.from_env(season).fetch_league().get("settings") or {})
    except EspnError as exc:
        return {}, str(exc)
    draft = settings.get("draftSettings") or {}
    trade = settings.get("tradeSettings") or {}
    return (
        {
            "keeper_deadline": _epoch_ms(draft.get("keeperDeadlineDate")),
            "trade_deadline": _epoch_ms(trade.get("deadlineDate")),
        },
        None,
    )
