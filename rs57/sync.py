"""Write one season of ESPN data to ``data/derived/{year}.json``.

    python -m rs57.sync --year 2026

This is the nightly Action's writer and it owns ``data/derived/`` alone. It never *writes*
``data/manual/`` — that belongs to the local admin tool — and it never writes
``data/history/``, which is frozen once a season completes. NO FILE HAS TWO WRITERS.

It does **read** both, for one fact ESPN does not carry: which of a season's four keeper picks
filled the PROSPECT slot. See ``prospect_ids_for_season``. Reading is not writing, and
``site.py`` has always read the same file.

Salary overrides are deliberately **not** applied here. ``base_salary`` in the derived file is
what ESPN says, and ``keeper_rules.effective_base_salary`` swaps in an un-reverted
``SalaryOverride`` at pricing time. Baking an override into the derived base would hide a live
draft-cash distortion inside a file the Action rewrites nightly, which is the one way it could
ratchet into a player's price permanently.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from rs57.espn import (
    EspnClient,
    EspnError,
    SyncedSeason,
    base_season_for,
    build_season,
    keeper_pick_ids,
    winning_bids,
)
from rs57.models import dump_json

DATA = Path(__file__).resolve().parent.parent / "data"
DERIVED = DATA / "derived"
HISTORY = DATA / "history"
MANUAL = DATA / "manual"


def prospect_ids_for_season(
    season: int, history_dir: Path = HISTORY, manual_dir: Path = MANUAL
) -> set[int] | None:
    """Players kept in the PROSPECT slot in ``season``, from whichever record holds it.

    Read-only in both directories: the backfill importer owns ``data/history/`` and the admin
    tool owns ``data/manual/``. The Action reads them and writes neither.

    **Two records, because a season is only frozen after it ends.** A completed season's claims
    are in ``data/history/{season}.json``; the season being played has its claims in
    ``data/manual/claims.json``, hand-entered through the admin tool, which is the only input
    path ESPN leaves open before an auction. Asking history alone — which is what this did
    while it only ever wanted *last* season — answers ``None`` for the current season forever,
    and every prospect on it rides on a warning instead of being subtracted.

    This used to read a hand-maintained ``data/manual/prospects.json``, which existed only
    because no claim carried a slot — ESPN marks all four keeper picks the same way and
    ``draftSettings.keeperCount`` is 4, so it cannot tell a prospect from a keeper. Now that
    claims record real slots the answer is **derived** from ``slot == PROSPECT`` instead of
    transcribed, which is what that file's own header said to do once claims existed. Deleting
    it any earlier would have silently re-taxed every prospect $5.

    Returns ``None`` when no record names that season at all, which is not the same as an empty
    set — ``None`` means "unknown, warn about it" and ``set()`` means "checked, there were
    none". Collapsing the two would let an unrecorded prospect be taxed in silence.
    """
    frozen = _prospect_slots(history_dir / f"{season}.json", lambda doc: doc.get("claims"))
    if frozen is not None:
        return frozen
    return _prospect_slots(
        manual_dir / "claims.json",
        lambda doc: (doc.get("seasons") or {}).get(str(season)),
    )


def _prospect_slots(
    path: Path, claims_of: Callable[[dict[str, Any]], list[dict[str, Any]] | None]
) -> set[int] | None:
    """The PROSPECT ids in one claims file, or ``None`` when it does not record that season.

    A file with claims in it but no PROSPECT among them answers ``set()`` — checked, none —
    which is the distinction ``prospect_ids_for_season`` exists to preserve.

    **An empty claims list is "no record", not "no prospects".** 2019-2023 are frozen with
    ``"claims": []`` because those seasons predate the `RS57` sheet's Fee Allocations tabs
    entirely — nobody transcribed who was kept, let alone in which slot. Reading that as
    checked-and-none would subtract no prospects and say nothing about it, which is exactly the
    silent $5 the ``None`` case exists to prevent. A season where twelve managers genuinely kept
    nobody has never happened, and if it ever does, a spurious REVIEW note costs a sentence
    where a spurious ``set()`` costs money.
    """
    if not path.exists():
        return None
    claims = claims_of(json.loads(path.read_text(encoding="utf-8")))
    if not claims:
        return None
    return {
        claim["espn_player_id"] for claim in claims if claim.get("slot") == "PROSPECT"
    }


def season_document(season: SyncedSeason) -> dict[str, Any]:
    """The on-disk shape of a derived season.

    ``base_salary_field`` is recorded on purpose. It is the one number in this pipeline whose
    meaning depends on *when* it was read — ``keeperValue`` before a season's auction,
    ``keeperValueFuture`` after — so the file says which it was rather than leaving a reader
    to infer it. See ``docs/espn-field-semantics.md``.

    No ``generated_at``: the Action re-serialises this file nightly, and a timestamp would
    make every run a commit even when nothing about the league changed. Git already records
    when a commit happened.
    """
    return {
        "season": season.season,
        "source": {
            "drafted": season.drafted,
            "base_salary_field": season.base_field,
            # ISO string, not a datetime: dump_json's ``_jsonable`` walks models and
            # containers, and a bare datetime reaches the encoder unconverted. Models
            # serialise their own datetimes to ISO, so this matches them.
            "trade_deadline": (
                season.trade_deadline.isoformat() if season.trade_deadline else None
            ),
            "draft_date": season.draft_date.isoformat() if season.draft_date else None,
            "keeper_deadline": (
                season.keeper_deadline.isoformat() if season.keeper_deadline else None
            ),
        },
        "franchises": list(season.franchises),
        "players": list(season.players),
        "roster": list(season.roster),
        "review": {
            "waiver_bases_verified": season.waiver_bases_verified,
            "waiver_base_mismatches": list(season.waiver_base_mismatches),
            "warnings": list(season.warnings),
            # What state the season is in, kept apart from what needs looking at. A reader that
            # cannot tell them apart labels "this season has not drafted yet" as unverified.
            "phase": list(season.phase),
        },
    }


def hold_entered_bases(season: SyncedSeason, out_dir: Path) -> tuple[SyncedSeason, int]:
    """Keep the salaries already on disk while ESPN's field is not ESPN's to give.

    **The one field this sync will not overwrite, and only in one window.** For most of the
    year ESPN's keeper figure is the price a player carried in from last season, which is what
    every salary in this league is computed from. Between the keeper deadline and the auction
    it is not: the commissioner types the final keeper prices into ESPN in that window, base
    and allocated fee and $5 tax already added together, and the field hands those straight
    back. A sync that copies them in makes every kept player cost his own fee and tax twice.

    That is not hypothetical. On 2026-09-02 the 14:25 sync — after the deadline, after ESPN
    pruned the rosters to keepers — matched the 08-31 bases on all 38 players. The 17:16 sync,
    taken after the prices went in, moved 27 of them by exactly a fee or a fee plus tax.

    Matched on ``espn_player_id`` alone: a carried-in price follows the player across a trade,
    the same way the tax does, so it is his number rather than his franchise's.

    A player with no salary on disk takes ESPN's. There is nothing to preserve for somebody who
    has only just arrived, and refusing him a price would be worse than reading a stale one.

    Returns the season and how many salaries were held, because a merge nobody is told about is
    indistinguishable from no merge at all.
    """
    if not season.prices_entered:
        return season, 0
    path = out_dir / f"{season.season}.json"
    if not path.exists():
        return season, 0
    on_disk = {
        row.get("espn_player_id"): row.get("base_salary")
        for row in (json.loads(path.read_text(encoding="utf-8")).get("roster") or [])
    }
    held = 0
    roster = []
    for entry in season.roster:
        recorded = on_disk.get(entry.espn_player_id)
        if recorded is not None and recorded != entry.base_salary:
            held += 1
            entry = entry.model_copy(update={"base_salary": recorded})
        roster.append(entry)
    return replace(season, roster=tuple(roster)), held


def sync_season(year: int, *, out_dir: Path = DERIVED, write: bool = True) -> SyncedSeason:
    """Read ``year`` from ESPN and write the derived file.

    ``kept_prior_year`` comes from the keeper picks of **the season that established this
    season's bases** — its own once it has drafted, the previous one while it has not. That is
    ``base_season_for``, the same asymmetry as ``base_salary_field``, and pinning the tax to the
    same season as the base is the whole of it: charge a drafted season's ``keeperValueFuture``
    the previous season's tax and every keeper who went back into the auction pool and was
    bought again pays $5 he does not owe.

    Deriving it from the draft record at all retires the old script's hand-maintained list of
    *names*, which is what under-charged James Cook by $5 once ESPN started returning
    ``James Cook III``. Which of those picks was a PROSPECT is the one part ESPN cannot say; it
    comes from the league's own claims for that same season.

    Salaries are held rather than overwritten between the keeper deadline and the auction —
    see ``hold_entered_bases``. Everything else in the file syncs normally in that window: one
    field stops being ESPN's to give, not the season.
    """
    client = EspnClient.from_env(year)
    drafted = bool(client.fetch_draft_detail().get("drafted"))

    # One season answers for the base, the tax and the FAAB witness, because all three describe
    # the same money. Reuse this year's client when it is this year, rather than opening a
    # second one against the same season.
    base_season = base_season_for(year, drafted)
    base_client = client if base_season == year else EspnClient.from_env(base_season)

    try:
        keepers = keeper_pick_ids(base_client.fetch_draft_detail())
    except EspnError:
        # A missing season is survivable, and build_season warns that nobody is taxed. Failing
        # outright here would make the first synced season unsyncable.
        keepers = frozenset()
    try:
        faab = winning_bids(base_client.fetch_transactions())
    except EspnError:
        faab = None

    season = build_season(
        client,
        keeper_ids=keepers,
        prospect_ids=prospect_ids_for_season(base_season),
        faab_bids=faab,
    )

    # Before the write, and before the report: a dry run that skipped the merge would print a
    # count of nothing and call it a preview.
    season, held = hold_entered_bases(season, out_dir)
    season = replace(season, bases_held=held)

    if write:
        out_dir.mkdir(parents=True, exist_ok=True)
        dump_json(season_document(season), out_dir / f"{year}.json")
    return season


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync one season from ESPN to data/derived/.")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = parser.parse_args(argv)

    try:
        season = sync_season(args.year, write=not args.dry_run)
    except EspnError as exc:
        # EspnError messages never carry credentials; see EspnClient._get.
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1

    taxed = sum(entry.kept_prior_year for entry in season.roster)
    print(
        f"{season.season}: {len(season.roster)} roster entries across "
        f"{len(season.franchises)} franchises"
    )
    print(f"  base_salary read from {season.base_field} (drafted={season.drafted})")
    print(f"  kept_prior_year (taxed $5): {taxed}")
    print(
        f"  waiver bases verified against FAAB: {season.waiver_bases_verified}"
        f" ({len(season.waiver_base_mismatches)} mismatched)"
    )
    if season.bases_held:
        print(
            f"  HELD:   {season.bases_held} salaries left as they were on disk — ESPN currently "
            f"holds the entered keeper prices, not the values carried in from last season"
        )
    for note in season.phase:
        print(f"  phase:  {note}")
    for warning in season.warnings:
        print(f"  REVIEW: {warning}")
    if args.dry_run:
        print("  dry run, nothing written")
    else:
        print(f"  wrote {DERIVED / f'{season.season}.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
