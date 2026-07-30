"""Import completed seasons into ``data/history/`` and freeze them.

    python -m rs57.backfill --year 2025 --dry-run
    python -m rs57.backfill --all

Writes ``data/history/`` through :class:`rs57.history.HistoryStore` and nothing else. It never
touches ``data/derived/`` (the nightly Action's) or ``data/manual/`` (the admin tool's), and it
**does not commit** — a committing importer is how the intent-to-add defect turned into an
empty frozen season, and there is no reason for an import to hold the button as well.

What a frozen season holds, and why each part is there
-----------------------------------------------------

``claims``
    What each manager declared and what they owed for it, for the seasons where that is
    *recorded* rather than inferable. See "Where the claims come from" below.

``roster``
    The season's roster priced the ordinary way — what each player cost **in** that season.
    Identical in meaning to ``data/derived/{year}.json``'s roster, and it is what the *next*
    season's keeper claims were chosen from, so it is the membership those claims are checked
    against.

``roster_carried_in``
    The same roster priced at what each player **carried into** the season, which is the
    previous season's ``keeperValueFuture``. This is the half of the ratchet audit that no
    other file holds for a completed season, and it has to be built rather than read:

        **Once a season has drafted, ESPN overwrites ``keeperValue`` to equal
        ``keeperValueFuture``.**

    ``keeperValue`` holds the carried-in price only while a season is *undrafted* — which is
    the case ``docs/espn-field-semantics.md`` settled, against 2026, and it does not
    generalise backwards. Reading a completed season's ``keeperValue`` and calling it the
    carried-in price yields that season's own auction result instead, so every keeper appears
    to have carried in exactly what he was charged and the ratchet audit reports each one off
    by precisely his own fee and tax. It does not fail loudly. It was caught by running the
    audit and reading it.

``started_player_ids``
    Every player any league team started that season. Collected for a prospect rule requiring
    that a prospect had never been started, which was **retired** while this was being built
    (commissioner, 2026-07-30) — prospects may be started now, and the only remaining
    stipulation is that they are rookies. Kept as league history; nothing validates against it.
    ESPN serves box scores back to 2019 and no further.

Where the claims come from, and why not from ESPN
-------------------------------------------------

ESPN records which players were kept and the price charged, but **not the slot and not the fee
split** — ``draftSettings.keeperCount`` is 4 and all four picks are marked ``keeper: True``
with nothing saying which was the prospect. So a claim cannot be read off ESPN, and inventing
one would be worse than having none.

It matters more than that. A claim's ``computed_salary`` reconstructed from ESPN's own
``bidAmount`` would make ``check_base_continuity`` **tautological**: next season's
``keeperValue`` equals that bid by construction, so the audit would report a clean ratchet
having verified nothing — the same failure ``audit_ratchet``'s own comment warns about, from
the other direction. The audit only has teeth when ``computed_salary`` is built independently:
last season's price, plus the fee the manager actually allocated, plus the tax.

That fee split exists in exactly one place for the seasons before the admin tool, the
``Keepers`` workbook's ``Fee Allocations`` tabs, and there are exactly two of them. So **2024
and 2025 get claims and 2019-2023 do not.** The earlier seasons are still worth freezing for
their carried-in prices and their box-score history; they simply carry no claim rows, and each
file says why in as many words.

**From 2026 on the admin tool is the record**, so a season with no tab is not necessarily a
season with no claims: this reads ``data/manual/claims.json`` for it. Those rows are *better*
than a reconstruction — their ``computed_salary`` was frozen at submission, which is literally
what the manager was told they owed — so they are copied across verbatim and never recomputed.

Graduating a season is therefore: let it finish, run this, and clear that season out of the
admin tool. This module cannot do the clearing itself — ``data/manual/`` has one writer and it
is not this one — so it says so instead, and ``validate`` keeps reporting until it is done.

The workbook's ``K*_sal`` columns are **not** used and must never be: they are live VLOOKUPs
that recompute against today's roster, so they render current-year numbers against old declared
fees. Only the typed columns — franchise, slot, fee, prospect, and the consolation ``*`` — are
transcribed below.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rs57.espn import (
    BENCH_SLOT_IDS,
    POSITION_BY_ID,
    EspnClient,
    EspnError,
    _epoch_ms,
    acquisition_source,
    bid_season_for,
    build_season,
    keeper_pick_ids,
    winning_bids,
)
from rs57.admin.store import ManualStore
from rs57.history import HistoryStore
from rs57.models import KeeperClaim, KeeperSlot, RosterEntry

FIRST_ESPN_SEASON = 2019
"""The oldest season ESPN serves this league. 2018 answers 401 and 2017 and earlier 404, so
the record starts here — not by choice, and the files say so."""

SLOT_ORDER = (KeeperSlot.K1, KeeperSlot.K2, KeeperSlot.K3)


@dataclass(frozen=True)
class TeamAllocation:
    """One team's declaration for one season, as typed into the workbook.

    ``keepers`` is ``(player name, fee)`` in K1/K2/K3 order. The name is here only to be
    *resolved* against that season's ESPN keeper picks and is never stored — matching on a
    name is what under-charged James Cook by $5 once ESPN started returning ``James Cook III``,
    and the same workbook holds both spellings a year apart.
    """

    espn_team_id: int
    keepers: tuple[tuple[str, int], ...]
    prospect: str | None = None
    fees_waived: bool = False


# The two `Fee Allocations` tabs, transcribed. Typed cells only — no K*_sal, no P_Sal.
# The `*` in the CW column is the consolation-bracket winner of the PRIOR season, whose fees
# are waived here; read it off by one year and every waiver lands on the wrong team.
FEE_ALLOCATIONS: dict[int, tuple[TeamAllocation, ...]] = {
    2024: (
        TeamAllocation(1, (("Garrett Wilson", 0), ("Christian Kirk", 0)),
                       prospect="Tank Dell", fees_waived=True),
        TeamAllocation(2, (("Kenneth Walker III", 15), ("Nico Collins", 0), ("Puka Nacua", 0)),
                       prospect="Zach Charbonnet"),
        TeamAllocation(3, (("Kyren Williams", 7), ("Jonathan Taylor", 8),
                           ("Justin Jefferson", 0))),
        TeamAllocation(4, (("James Cook", 0), ("Zay Flowers", 5), ("Rashee Rice", 10)),
                       prospect="Anthony Richardson"),
        TeamAllocation(5, (("Ja'Marr Chase", 0), ("Kyle Pitts", 15), ("Chris Olave", 0))),
        TeamAllocation(6, (("CeeDee Lamb", 0), ("Drake London", 8),
                           ("Michael Pittman Jr.", 7)), prospect="Roschon Johnson"),
        TeamAllocation(7, (("Christian McCaffrey", 5), ("David Montgomery", 5),
                           ("C.J. Stroud", 5))),
        TeamAllocation(8, (("Jahmyr Gibbs", 5), ("DJ Moore", 5), ("Isiah Pacheco", 5))),
        TeamAllocation(9, (("Josh Allen", 5), ("Jayden Reed", 0))),
        TeamAllocation(10, (("Bijan Robinson", 5), ("Breece Hall", 0)),
                       prospect="Tyjae Spears"),
        TeamAllocation(11, (("Tyreek Hill", 15), ("Amon-Ra St. Brown", 0), ("Sam LaPorta", 0)),
                       prospect="Quentin Johnston"),
        TeamAllocation(12, (("De'Von Achane", 15), ("A.J. Brown", 0),
                            ("Travis Etienne Jr.", 0)), prospect="Tank Bigsby"),
    ),
    2025: (
        TeamAllocation(1, (("Bucky Irving", 0), ("James Conner", 5))),
        TeamAllocation(2, (("Puka Nacua", 0), ("Nico Collins", 0), ("Saquon Barkley", 15))),
        TeamAllocation(3, (("Justin Jefferson", 0), ("Jaxon Smith-Njigba", 5))),
        TeamAllocation(4, (("Jayden Daniels", 0), ("James Cook III", 15),
                           ("Rashee Rice", 0))),
        TeamAllocation(5, (("Ja'Marr Chase", 0), ("Malik Nabers", 0), ("Xavier Worthy", 15))),
        TeamAllocation(6, (("Drake London", 0), ("Michael Pittman Jr.", 15),
                           ("CeeDee Lamb", 0))),
        TeamAllocation(7, (("Brian Thomas Jr.", 5), ("Tee Higgins", 5), ("Brock Bowers", 5))),
        TeamAllocation(8, (("Jahmyr Gibbs", 0), ("Chuba Hubbard", 15), ("Mike Evans", 0))),
        TeamAllocation(9, (("Chase Brown", 0), ("Trey McBride", 5)), prospect="Tyjae Spears"),
        TeamAllocation(10, (("Jonathan Taylor", 0), ("Ladd McConkey", 0)), fees_waived=True),
        TeamAllocation(11, (("Amon-Ra St. Brown", 0), ("Terry McLaurin", 15),
                            ("Sam LaPorta", 0)), prospect="Braelon Allen"),
        TeamAllocation(12, (("De'Von Achane", 0), ("A.J. Brown", 5))),
    ),
}

CLAIMS_ABOUT = [
    "Claims are transcribed from the Keepers workbook's Fee Allocations tab for this season --",
    "the typed columns only. slot and fee_allocated cannot come from ESPN: keeperCount is 4 and",
    "all four picks are marked keeper=True with nothing recording which slot each filled.",
    "",
    "computed_salary is RECONSTRUCTED, not read from ESPN: last season's price, plus the fee",
    "the manager allocated, plus the $5 tax where it applies. That is the point. Taking it from",
    "ESPN's own bidAmount would make check_base_continuity tautological, because next season's",
    "keeperValue equals that bid by construction -- a clean ratchet having checked nothing.",
    "Where this figure and ESPN disagree, the audit reports it. Several do.",
    "",
    "The workbook's K*_sal cells are live VLOOKUPs that recompute against today's roster. They",
    "are not a record of what anyone paid and are deliberately not used here.",
]

RECORDED_CLAIMS_ABOUT = [
    "Claims recorded by the admin tool during the season, copied here verbatim when the season",
    "was frozen. Not reconstructed and not recomputed: computed_salary was frozen at submission,",
    "so it is exactly what the manager was told they owed -- a better witness than any later",
    "derivation, which is why it is carried across untouched.",
    "",
    "check_base_continuity audits next season's carried-in prices against these figures. A",
    "disagreement means the keeper was entered into ESPN's auction at a different number, or a",
    "draft-cash trade was never recorded as an override.",
    "",
    "The same rows may still be sitting in data/manual/claims.json. That file has one writer and",
    "it is not the importer, so clearing the graduated season out of it is a job for the admin",
    "tool; validate reports the duplication until it is done.",
]

NO_CLAIMS_ABOUT = [
    "No claims: this season predates the Fee Allocations tabs, of which the workbook holds",
    "exactly two (2024 and 2025). ESPN records WHICH players were kept and the price charged,",
    "but not the slot and not the fee split, so a claim for this season cannot be recorded --",
    "only invented. An invented claim would be worse than none: check_base_continuity would",
    "compare a number against itself and report a clean ratchet.",
    "",
    "The season is still frozen here for its carried-in prices and its started-lineup history,",
    "which ESPN does serve this far back. That history is retained as a league record; the",
    "prospect rule that required a prospect to have never been started is retired.",
]

PROSPECT_RULE_NOTE = [
    "The prospect rules have CHANGED. This league once allowed both rookies and second-year",
    "players to be prospected; it is rookies only now. So the same player legitimately appears",
    "as a prospect in two different seasons in the record below, and that is not an error to be",
    "tidied up. Today's repeat-claim rule is not applied retroactively to these seasons.",
]


class BackfillError(RuntimeError):
    """The import could not be made to reconcile, so nothing was written."""


@dataclass
class SeasonImport:
    """One season's import, assembled but not yet written."""

    season: int
    document: dict[str, Any]
    about: list[str] = field(default_factory=list)
    claims: list[KeeperClaim] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


_ROSTER_CACHE: dict[tuple[int, int], list[dict[str, Any]]] = {}


def _roster_entries(client: EspnClient, team_id: int) -> list[dict[str, Any]]:
    """One team's roster entries, memoised per (season, team).

    An import reads each season's rosters from several angles — the carried-in prices, the
    prior season's prices, the keeper name pool — and ESPN is a public endpoint being asked
    nicely. Twelve teams times seven seasons is enough round trips without fetching each one
    four times.
    """
    key = (client.year, team_id)
    if key not in _ROSTER_CACHE:
        payload = client.fetch_roster(team_id)
        _ROSTER_CACHE[key] = (
            (payload.get("teams") or [{}])[0].get("roster") or {}
        ).get("entries") or []
    return _ROSTER_CACHE[key]


def _resolve_names(
    names: list[str], candidates: dict[int, str], *, season: int, team_id: int
) -> dict[str, int]:
    """Map the workbook's typed names onto ``espn_player_id`` within one team's keeper picks.

    The candidate pool is a single team's two-to-four keepers, so there is nothing to be clever
    about — but a name that does not resolve is raised rather than skipped. The whole point of
    this pass is that it is the one place names are allowed to be load-bearing, and a silent
    miss here would drop a claim and take its fee with it.
    """
    by_name = {name: pid for pid, name in candidates.items()}
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for name in names:
        if name in by_name:
            resolved[name] = by_name[name]
        else:
            missing.append(name)
    if missing or len(names) != len(candidates):
        raise BackfillError(
            f"{season} t{team_id}: the workbook and ESPN do not agree on who was kept. "
            f"workbook={sorted(names)} espn={sorted(candidates.values())}"
            + (f" unresolved={missing}" if missing else "")
            + ". Match players on espn_player_id, never on name — and do not paper over this "
            "by dropping the row."
        )
    return resolved


def build_claims(
    season: int,
    *,
    keeper_names: dict[int, dict[int, str]],
    prior_base: dict[int, int],
    prior_keeper_ids: frozenset[int],
    prior_prospect_ids: frozenset[int],
    prior_prospects_known: bool = True,
) -> tuple[list[KeeperClaim], list[str]]:
    """Reconstruct one season's claims from the workbook's fee splits and ESPN's prices.

    ``computed_salary`` is the engine's formula applied to last season's price, deliberately
    *not* what ESPN charged — see this module's header. ``submitted_at`` stays ``None``: these
    were declared by text message years ago and inventing a timestamp would make a guess look
    like a record.

    ``prior_prospects_known`` is False when the previous season has no Fee Allocations tab, and
    it is the difference between a finding and a fabrication. ESPN marks all four keeper picks
    identically, so without that tab **there is no way to tell which of last season's keeps was
    the prospect** — and a prospect keep sets no $5 tax. Assuming taxed over-prices every one of
    them by exactly $5, which then reads as a discontinuity the commissioner would go and chase.
    Three of them did, before this was caught.

    So where the tax cannot be determined, ``computed_salary`` is left ``None``.
    ``check_base_continuity`` skips unpriced claims, so those players are *not audited* rather
    than audited against a guess — and the slot and fee, which the tab does record, are kept.
    """
    allocations = FEE_ALLOCATIONS.get(season)
    if allocations is None:
        return [], []

    claims: list[KeeperClaim] = []
    notes: list[str] = []
    unpriced: list[str] = []
    for allocation in allocations:
        team_id = allocation.espn_team_id
        manager_id = f"t{team_id}"
        candidates = keeper_names.get(team_id, {})
        names = [name for name, _ in allocation.keepers]
        if allocation.prospect:
            names.append(allocation.prospect)
        resolved = _resolve_names(names, candidates, season=season, team_id=team_id)

        for slot, (name, fee) in zip(SLOT_ORDER, allocation.keepers, strict=False):
            player_id = resolved[name]
            base = prior_base.get(player_id)
            if base is None:
                raise BackfillError(
                    f"{season} t{team_id}: {name} ({player_id}) was kept but is not on any "
                    f"{season - 1} roster, so his carried-in price is unknown"
                )
            was_kept = player_id in prior_keeper_ids
            taxed = was_kept and player_id not in prior_prospect_ids
            # Ambiguous only when he WAS kept and we cannot tell whether that keep was a
            # prospect. A player absent from last season's keeper set is untaxed for certain.
            tax_unknown = was_kept and not prior_prospects_known
            if tax_unknown:
                unpriced.append(name)
            claims.append(
                KeeperClaim(
                    season=season,
                    manager_id=manager_id,
                    espn_player_id=player_id,
                    slot=slot,
                    fee_allocated=fee,
                    computed_salary=None if tax_unknown else base + fee + (5 if taxed else 0),
                )
            )

        if allocation.prospect:
            player_id = resolved[allocation.prospect]
            base = prior_base.get(player_id)
            if base is None:
                raise BackfillError(
                    f"{season} t{team_id}: prospect {allocation.prospect} ({player_id}) is not "
                    f"on any {season - 1} roster"
                )
            # A prospect is kept at acquisition value: no fee, no tax, ever.
            claims.append(
                KeeperClaim(
                    season=season,
                    manager_id=manager_id,
                    espn_player_id=player_id,
                    slot=KeeperSlot.PROSPECT,
                    fee_allocated=0,
                    computed_salary=base,
                )
            )

        if allocation.fees_waived:
            notes.append(
                f"{manager_id} had fees waived as the {season - 1} consolation winner, so the "
                f"tier total of $0 here is correct and not a missing allocation"
            )

    if unpriced:
        notes.append(
            f"{len(unpriced)} claim(s) carry no computed_salary because {season - 1} has no "
            f"Fee Allocations tab, so it is unknown which of its keeps was the PROSPECT — and "
            f"a prospect keep sets no $5 tax. They are NOT audited by check_base_continuity "
            f"rather than audited against a guessed tax: {', '.join(sorted(unpriced))}"
        )
    return claims, notes


def started_player_ids(client: EspnClient, weeks: range = range(1, 18)) -> set[int]:
    """Every player any team in the league *started* in one season.

    Recorded as league history. No rule reads it: the prospect rule requiring that a prospect
    had never been started is retired. ``lineupSlotId`` outside the bench and IR is what
    "started" means, the same deny-list ``stats`` awards the positional studs on.
    """
    found: set[int] = set()
    for week in weeks:
        try:
            schedule = client.fetch_boxscore(week)
        except EspnError:
            # A week that was never played is not a failure; an unplayed week has no starters.
            continue
        for raw in schedule:
            if raw.get("matchupPeriodId") != week:
                continue
            for side in ("home", "away"):
                team_side = raw.get(side)
                if not team_side:
                    continue
                roster = team_side.get("rosterForCurrentScoringPeriod") or {}
                for entry in roster.get("entries") or []:
                    player = (entry.get("playerPoolEntry") or {}).get("player") or {}
                    if player.get("id") is None:
                        continue
                    if player.get("defaultPositionId") not in POSITION_BY_ID:
                        continue
                    if entry.get("lineupSlotId") not in BENCH_SLOT_IDS:
                        found.add(player["id"])
    return found


def prospect_ids_for(season: int) -> frozenset[int] | None:
    """Players kept in the PROSPECT slot in ``season``, or ``None`` when it is unrecorded.

    ``None`` and ``frozenset()`` are not the same answer and must not be collapsed: ``None``
    means "no Fee Allocations tab for that season, so nobody knows", and an empty set means
    "checked, there were none". Passed on to ``build_season``, the first carries a warning and
    the second does not — because a prospect keep sets no $5 tax, and quietly taxing one is the
    error this whole distinction exists to prevent.
    """
    allocations = FEE_ALLOCATIONS.get(season)
    if allocations is None:
        return None
    keeper_names = _keeper_names(EspnClient.from_env(season))
    found: set[int] = set()
    for allocation in allocations:
        if not allocation.prospect:
            continue
        candidates = keeper_names.get(allocation.espn_team_id, {})
        names = [name for name, _ in allocation.keepers] + [allocation.prospect]
        resolved = _resolve_names(
            names, candidates, season=season, team_id=allocation.espn_team_id
        )
        found.add(resolved[allocation.prospect])
    return frozenset(found)


def check_completed(client: EspnClient) -> None:
    """Refuse a season that is not over. Freezing is permanent, so this comes first.

    Two signals, both needed. A season that has not drafted has no keeper prices at all. A
    season that has drafted but has no ``rankCalculatedFinal`` on any team is in progress —
    its rosters are still moving, its box scores are partial, and freezing it would record a
    September snapshot as the settled result of the year.
    """
    if not client.fetch_draft_detail().get("drafted"):
        raise BackfillError(
            f"{client.year} has not drafted, so it has no keeper prices to freeze. "
            f"data/history/ is written once per COMPLETED season."
        )
    teams = client.fetch_league().get("teams") or []
    if not any(team.get("rankCalculatedFinal") for team in teams):
        raise BackfillError(
            f"{client.year} has drafted but no team has a final rank, so the season is still "
            f"in progress. Freezing it now would record a mid-season snapshot permanently."
        )


def graduated_claims(year: int, manual: ManualStore) -> tuple[list[KeeperClaim], list[str]]:
    """Claims the admin tool recorded for ``year``, for a season graduating into history.

    A season with no Fee Allocations tab is not necessarily a season with no claims — from
    2026 on the admin tool is the record, and this is how a completed season's declarations
    reach ``data/history/`` instead of sitting in ``data/manual/`` forever.

    Copied **verbatim and never recomputed**. ``computed_salary`` was frozen at submission, so
    it is literally what the manager was told they owed; re-deriving it here would replace the
    strongest witness this pipeline has with a later guess, and would hand
    ``check_base_continuity`` a figure derived from the same data it is auditing.
    """
    claims = manual.claims(year)
    if not claims:
        return [], []
    return claims, [
        f"{len(claims)} claim(s) came from data/manual/claims.json, recorded by the admin tool "
        f"during the season and copied here unchanged. Clear {year} out of that file through "
        f"the tool now that it is frozen — until then there are two records of one fact, and "
        f"validate reports it every run."
    ]


def prepare_season(year: int, *, manual: ManualStore | None = None) -> SeasonImport:
    """Read one completed season from ESPN and assemble its frozen document. Writes nothing."""
    client = EspnClient.from_env(year)
    prior = EspnClient.from_env(year - 1)
    check_completed(client)

    try:
        prior_keepers = keeper_pick_ids(prior.fetch_draft_detail())
    except EspnError:
        prior_keepers = frozenset()
    prior_prospects = prospect_ids_for(year - 1)

    drafted = bool(client.fetch_draft_detail().get("drafted"))
    try:
        faab = winning_bids(EspnClient.from_env(bid_season_for(year, drafted)).fetch_transactions())
    except EspnError:
        faab = None

    season = build_season(
        client,
        prior_keeper_ids=prior_keepers,
        prior_prospect_ids=prior_prospects,
        faab_bids=faab,
    )

    # What each player carried INTO this season is the previous season's keeperValueFuture.
    # It cannot be read off this season's payload: a drafted season's keeperValue has been
    # overwritten with what its own auction charged. See this module's header.
    # 2018 answers 401, so the oldest season ESPN serves has no carried-in prices at all —
    # reported as absent rather than filled in with this season's numbers.
    try:
        prior_snapshot = _prior_snapshot(year)
    except EspnError:
        prior_snapshot = {}
    prior_base = {pid: row["base"] for pid, row in prior_snapshot.items()}
    carried_in = [
        entry.model_copy(update={"base_salary": prior_base[entry.espn_player_id]})
        for entry in season.roster
        if entry.espn_player_id in prior_base
    ]

    # The roster each keeper was DECLARED from, reconstructed from ESPN's draft record.
    #
    # Neither roster snapshot can serve as this. The previous season's ends before the
    # offseason trades — Garrett Wilson finished 2023 on t12 and was kept by t1 — and this
    # season's has already lost every keeper dropped during it, Christian Kirk among them.
    # ESPN preserves no roster as it stood on the keeper deadline.
    #
    # The draft record does, though, for exactly the players that matter: a keeper pick names
    # the team that kept him. That is the authoritative attribution, it is the same record the
    # claims themselves are built from, and it is why a claim's membership needs no second
    # opinion from a roster that was taken on a different day.
    # A keeper dropped during the season he was kept for is gone from this season's roster —
    # Christian Kirk, Kyle Pitts and James Conner all are — so the previous season's snapshot
    # is the fallback. He was on somebody's roster there by definition; that is how he came to
    # be keepable.
    own_entries = {entry.espn_player_id: entry for entry in season.roster}
    declared: list[RosterEntry] = []
    for pick in client.fetch_draft_detail().get("picks") or []:
        if not pick.get("keeper"):
            continue
        player_id, team_id = pick["playerId"], pick["teamId"]
        manager_id = f"t{team_id}"
        taxed = player_id in prior_keepers and player_id not in (prior_prospects or ())
        snapshot = prior_snapshot.get(player_id)
        own = own_entries.get(player_id)
        if own is not None:
            declared.append(
                own.model_copy(
                    update={
                        "manager_id": manager_id,
                        "base_salary": prior_base.get(player_id, own.base_salary),
                    }
                )
            )
        elif snapshot is not None and snapshot["acquired_at"] is not None:
            declared.append(
                RosterEntry(
                    season=year,
                    manager_id=manager_id,
                    espn_player_id=player_id,
                    acquired_at=snapshot["acquired_at"],
                    base_salary=snapshot["base"],
                    kept_prior_year=taxed,
                    source=acquisition_source(snapshot["source"]),
                )
            )

    claims, notes = build_claims(
        year,
        keeper_names=_keeper_names(client) if FEE_ALLOCATIONS.get(year) else {},
        prior_base=prior_base,
        prior_keeper_ids=prior_keepers,
        prior_prospect_ids=prior_prospects or frozenset(),
        prior_prospects_known=prior_prospects is not None,
    )

    recorded = False
    if not claims and FEE_ALLOCATIONS.get(year) is None:
        claims, graduation_notes = graduated_claims(year, manual or ManualStore())
        notes += graduation_notes
        recorded = bool(claims)
    waived = next(
        (
            f"t{allocation.espn_team_id}"
            for allocation in FEE_ALLOCATIONS.get(year, ())
            if allocation.fees_waived
        ),
        None,
    )
    started = started_player_ids(client)

    if not claims:
        about_lines = NO_CLAIMS_ABOUT
    elif recorded:
        about_lines = RECORDED_CLAIMS_ABOUT
    else:
        about_lines = CLAIMS_ABOUT
    about = list(about_lines) + ["", *PROSPECT_RULE_NOTE]
    document: dict[str, Any] = {
        "source": {
            "base_salary_field": season.base_field,
            "carried_in_from": f"{year - 1} keeperValueFuture" if carried_in else None,
            "claims_source": (
                None
                if not claims
                else "data/manual/claims.json, recorded by the admin tool"
                if recorded
                else f"Keepers workbook, {year} Fee Allocations tab (typed columns only)"
            ),
            "started_history_from": "ESPN mBoxscore",
            "trade_deadline": (
                season.trade_deadline.isoformat() if season.trade_deadline else None
            ),
        },
        "fees_waived_manager_id": waived,
        "franchises": list(season.franchises),
        "roster": list(season.roster),
        "roster_carried_in": carried_in,
        "roster_at_declaration": declared,
        "claims": claims,
        # Overrides stay OUT of history. The three on the workbook's `Manually Changed
        # Salaries` tab are all un-reverted, which makes them live state the commissioner
        # still has to flip once ESPN is put back — and a frozen file cannot be flipped. They
        # belong in data/manual/overrides.json, which is the admin tool's to write, and until
        # they are there the ratchet audit reports each one. That is the correct loud answer.
        "overrides": [],
        "started_player_ids": sorted(started),
        "review": {"notes": notes},
    }
    return SeasonImport(
        season=year, document=document, about=about, claims=claims, notes=notes
    )


def _keeper_names(client: EspnClient) -> dict[int, dict[int, str]]:
    """``espn_team_id -> {player id: name}`` for that season's declared keepers.

    The resolution pool for the workbook's typed names, and nothing wider: two-to-four players
    per team, so a name either resolves inside its own team's keepers or the transcription is
    wrong and the import stops.

    Names are gathered from **both** this season's rosters and the previous season's, because
    ESPN's roster is the state at the end of a season and a keeper can be dropped during the
    one he was kept for. Christian Kirk is the real case: kept into 2024, gone from the 2024
    roster by the end of it, so this season alone yields a bare player id and the transcription
    cannot be checked against anything. A keeper was by definition rostered the season before,
    which makes that the pool that always has him.
    """
    names: dict[int, str] = {}
    for year in (client.year - 1, client.year):
        source = client if year == client.year else EspnClient.from_env(year)
        for team_id in range(1, 13):
            try:
                entries = _roster_entries(source, team_id)
            except EspnError:
                continue
            for entry in entries:
                player = (entry.get("playerPoolEntry") or {}).get("player") or {}
                if player.get("id") is not None:
                    names[player["id"]] = player.get("fullName") or ""

    picks: dict[int, dict[int, str]] = {}
    for pick in client.fetch_draft_detail().get("picks") or []:
        if pick.get("keeper"):
            picks.setdefault(pick["teamId"], {})[pick["playerId"]] = names.get(
                pick["playerId"], str(pick["playerId"])
            )
    return picks


def _prior_snapshot(year: int) -> dict[int, dict[str, Any]]:
    """Each player's ``year - 1`` price and acquisition, keyed by ``espn_player_id``.

    The price is the prior season's ``keeperValueFuture`` — what was established *in* that
    season, which is exactly what a keeper carries forward. Not its ``keeperValue``, which
    would reach back one season too far and reprice the whole league; and not this season's
    ``keeperValue``, which a completed season has already overwritten.
    """
    client = EspnClient.from_env(year - 1)
    out: dict[int, dict[str, Any]] = {}
    for team_id in range(1, 13):
        for entry in _roster_entries(client, team_id):
            pool = entry.get("playerPoolEntry") or {}
            player_id = (pool.get("player") or {}).get("id")
            if player_id is None or pool.get("keeperValueFuture") is None:
                continue
            out[player_id] = {
                "base": pool["keeperValueFuture"],
                "acquired_at": _epoch_ms(entry.get("acquisitionDate")),
                "source": entry.get("acquisitionType"),
            }
    return out




def import_season(
    year: int,
    *,
    store: HistoryStore | None = None,
    manual: ManualStore | None = None,
    write: bool = True,
) -> SeasonImport:
    store = store or HistoryStore()
    if write and store.exists(year):
        raise BackfillError(
            f"data/history/{year}.json is already frozen. Nothing re-writes a frozen season."
        )
    prepared = prepare_season(year, manual=manual)
    if write:
        store.write_season(year, prepared.document, about=prepared.about)
    return prepared


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze completed seasons into data/history/."
    )
    parser.add_argument("--year", type=int, action="append", dest="years")
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"every completed season ESPN serves, {FIRST_ESPN_SEASON} onward",
    )
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args(argv)

    store = HistoryStore()
    if args.all:
        years = [y for y in range(FIRST_ESPN_SEASON, 2026)]
    elif args.years:
        years = args.years
    else:
        parser.error("pass --year or --all")

    failures = 0
    for year in years:
        if store.exists(year) and not args.dry_run:
            print(f"{year}: already frozen, skipping (data/history/ is written once)")
            continue
        try:
            prepared = import_season(year, store=store, write=not args.dry_run)
        except (BackfillError, EspnError) as exc:
            print(f"{year}: FAILED — {exc}", file=sys.stderr)
            failures += 1
            continue
        doc = prepared.document
        print(f"{year}: {len(doc['roster'])} roster entries "
              f"({len(doc['roster_carried_in'])} with a carried-in price), "
              f"{len(prepared.claims)} claims, "
              f"{len(doc['started_player_ids'])} players started")
        for note in prepared.notes:
            print(f"  note: {note}")
        if not prepared.claims:
            print("  no claims — no Fee Allocations tab for this season; slots and fee "
                  "splits are not recoverable from ESPN")
        if args.dry_run:
            print("  dry run, nothing written")
        else:
            print(f"  froze {Path('data/history') / f'{year}.json'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
