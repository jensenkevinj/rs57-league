"""Write one season of ESPN data to ``data/derived/{year}.json``.

    python -m rs57.sync --year 2026

This is the nightly Action's writer and it owns ``data/derived/`` alone. It never touches
``data/manual/`` — that belongs to the local admin tool — and it never writes
``data/history/``, which is frozen once a season completes. NO FILE HAS TWO WRITERS.

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
from pathlib import Path
from typing import Any

from rs57.espn import (
    EspnClient,
    EspnError,
    SyncedSeason,
    bid_season_for,
    build_season,
    keeper_pick_ids,
    winning_bids,
)
from rs57.models import dump_json

DATA = Path(__file__).resolve().parent.parent / "data"
DERIVED = DATA / "derived"
HISTORY = DATA / "history"


def prior_prospect_ids(season: int, history_dir: Path = HISTORY) -> set[int] | None:
    """Players kept in the PROSPECT slot in ``season``, read from ``data/history/``.

    Read-only: the backfill importer owns ``data/history/`` and the Action must never write it.

    This used to read a hand-maintained ``data/manual/prospects.json``, which existed only
    because no claim carried a slot — ESPN marks all four keeper picks the same way and
    ``draftSettings.keeperCount`` is 4, so it cannot tell a prospect from a keeper. Now that
    completed seasons record real ``KeeperClaim`` rows the answer is **derived** from
    ``slot == PROSPECT`` instead of transcribed, which is what that file's own header said to
    do once claims existed. Deleting it any earlier would have silently re-taxed every
    prospect $5.

    Returns ``None`` when that season is not frozen, which is not the same as an empty set —
    ``None`` means "unknown, warn about it" and ``set()`` means "checked, there were none".
    Collapsing the two would let an unrecorded prospect be taxed in silence.
    """
    path = history_dir / f"{season}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    claims = doc.get("claims")
    if claims is None:
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


def sync_season(year: int, *, out_dir: Path = DERIVED, write: bool = True) -> SyncedSeason:
    """Read ``year`` from ESPN and write the derived file.

    Last season's draft supplies ``kept_prior_year``: a player who entered the previous
    auction as a keeper is taxed this season unless he has been dropped since. Deriving it
    this way retires the old script's hand-maintained list of *names*, which is what
    under-charged James Cook by $5 once ESPN started returning ``James Cook III``.
    """
    client = EspnClient.from_env(year)
    try:
        prior_keepers = keeper_pick_ids(EspnClient.from_env(year - 1).fetch_draft_detail())
    except EspnError:
        # A missing prior season is survivable, and build_season warns that nobody is taxed.
        # Failing outright here would make the first synced season unsyncable.
        prior_keepers = frozenset()

    # Waiver bases are checked against the FAAB actually bid, read from whichever season
    # established them — this one if it has drafted, otherwise the one before.
    drafted = bool(client.fetch_draft_detail().get("drafted"))
    bid_client = EspnClient.from_env(bid_season_for(year, drafted))
    try:
        faab = winning_bids(bid_client.fetch_transactions())
    except EspnError:
        faab = None

    season = build_season(
        client,
        prior_keeper_ids=prior_keepers,
        prior_prospect_ids=prior_prospect_ids(year - 1),
        faab_bids=faab,
    )

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
