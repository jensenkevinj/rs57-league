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

from dataclasses import dataclass
from datetime import UTC, datetime

from rs57.admin.derived import DerivedSeason
from rs57.espn import EspnClient, EspnError
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
    """One player, as recorded here and as ESPN holds him."""

    manager_id: str
    name: str
    espn_player_id: int
    recorded_salary: int | None
    espn_bid: int | None
    slot: str

    @property
    def state(self) -> str:
        if self.recorded_salary is None:
            return "espn_only"
        if self.espn_bid is None:
            return "not_in_espn"
        if self.recorded_salary != self.espn_bid:
            return "mismatch"
        return "agrees"

    @property
    def delta(self) -> int | None:
        if self.recorded_salary is None or self.espn_bid is None:
            return None
        return self.espn_bid - self.recorded_salary


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
    def agrees(self) -> bool:
        return not (self.mismatches or self.missing_from_espn or self.espn_only)


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
    by_player: dict[tuple[str, int], ReconcileRow] = {}

    for claim in claims:
        player = names.get(claim.espn_player_id)
        by_player[(claim.manager_id, claim.espn_player_id)] = ReconcileRow(
            manager_id=claim.manager_id,
            name=player.name if player else f"player {claim.espn_player_id}",
            espn_player_id=claim.espn_player_id,
            recorded_salary=claim.computed_salary,
            espn_bid=None,
            slot=str(claim.slot),
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
                espn_bid=pick.bid,
                slot=existing.slot,
            )
            continue
        player = names.get(pick.espn_player_id)
        by_player[key] = ReconcileRow(
            manager_id=pick.manager_id,
            name=player.name if player else f"player {pick.espn_player_id}",
            espn_player_id=pick.espn_player_id,
            recorded_salary=None,
            espn_bid=pick.bid,
            slot="",
        )

    rows = sorted(
        by_player.values(),
        key=lambda row: (len(row.manager_id), row.manager_id, row.slot, row.name),
    )
    return Reconciliation(
        season=season, rows=tuple(rows), espn_pick_count=len(picks), error=error
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
