"""Write ``data/derived/player-origins.json`` — when each player's NFL career began.

    python -m rs57.origins_sync --year 2026 --dry-run

The nightly Action owns ``data/derived/``. Locally use ``--dry-run``, which fetches and reports
without writing, so it cannot leave an untracked file for someone to commit by accident.

**The only writer of that file, and the only module that talks to ESPN's core API.** The keeper
sync does not, deliberately: a core-API outage must never be able to touch a season of
salaries, and the way to guarantee that is for the two never to share a run.

What makes this safe to run every night
---------------------------------------

*It only adds.* ``origins.merge`` folds a run's findings into what is on disk and never removes
or overwrites, so a run that fetches half the league leaves the other half exactly as it was.
A partial failure is safe by construction rather than by careful ordering.

*A 404 is an answer; anything else is an outage.* ESPN having no athlete record is a fact worth
recording. Not being able to reach ESPN is not, and it stops the run before it writes.

*Zero resolutions is treated as breakage, not as news.* If a run asks about a whole league and
resolves nobody, that is a changed API or a bad parser, not a league of players without draft
classes — and writing it would mark every prospect in the league unverified in one commit. The
same reasoning as ``espn.MIN_ROSTER_SIZE``.

*Nothing is fetched twice.* A draft class is immutable, so a player already on record is never
asked about again. Steady state is a handful of requests for the week's waiver adds; only the
first run pays for the whole league.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from pathlib import Path

from rs57.espn import EspnCoreClient, EspnError, first_nfl_season
from rs57.models import OriginSource, dump_json
from rs57.origins import (
    load_document,
    merge,
    origins_path,
    unresolved_ids,
)

DATA = Path(__file__).resolve().parent.parent / "data"
DERIVED = DATA / "derived"

MIN_ATTEMPTS_FOR_DEGRADED_CHECK = 10
"""Below this a run is too small to tell "resolved nothing" from "had nothing to ask about".

Counted over players never asked about before. Retries of known-unresolved ids are excluded
deliberately: ESPN has already said it has nothing for them, so they resolve nothing on every
subsequent run by definition, and counting them would make the guard fire every night.
"""


def roster_player_ids(derived_dir: Path, years: Iterable[int] | None = None) -> set[int]:
    """Every player id on a roster in ``data/derived/{year}.json``, DEF excluded.

    D/ST ids are negative and ESPN's athlete endpoint 404s on every one of them. Filtering
    here rather than collecting a dozen meaningless misses each run keeps ``unresolved``
    meaning "ESPN knows him but not when he started", which is the only reading that is worth
    putting in front of the commissioner.
    """
    found: set[int] = set()
    for path in sorted(derived_dir.glob("*.json")):
        stem = path.stem
        if not stem.isdigit():
            continue
        if years is not None and int(stem) not in set(years):
            continue
        document = json.loads(path.read_text(encoding="utf-8"))
        for row in document.get("roster") or []:
            player_id = int(row["espn_player_id"])
            if player_id > 0:
                found.add(player_id)
    return found


def claimed_player_ids(data_dir: Path = DATA) -> set[int]:
    """Player ids on any recorded keeper claim, frozen or live.

    A prospect claimed in a frozen season may no longer be on anybody's roster, and the audit
    still has to be able to say when he started. Read-only across both directories.
    """
    found: set[int] = set()
    for path in sorted((data_dir / "history").glob("*.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        for claim in document.get("claims") or []:
            found.add(int(claim["espn_player_id"]))
    live = data_dir / "manual" / "claims.json"
    if live.exists():
        document = json.loads(live.read_text(encoding="utf-8"))
        for claims in (document.get("seasons") or {}).values():
            for claim in claims:
                found.add(int(claim["espn_player_id"]))
    return found


def sync_origins(
    season: int,
    *,
    write: bool = True,
    every_season: bool = False,
    derived_dir: Path = DERIVED,
    data_dir: Path = DATA,
    client: EspnCoreClient | None = None,
) -> tuple[dict, list[int], list[int], int]:
    """Fetch the players not yet on record and merge them in.

    Returns ``(document, newly_resolved, still_unresolved, attempted)``.
    """
    client = client or EspnCoreClient()
    document = load_document(derived_dir)

    known = {int(row["espn_player_id"]) for row in document.get("players") or []}
    wanted = roster_player_ids(derived_dir, None if every_season else [season])
    if every_season:
        wanted |= claimed_player_ids(data_dir)

    # Already-resolved players are never re-fetched; a draft class does not change. Previously
    # unresolved ones are, because ESPN backfills debutYear.
    retries = unresolved_ids(derived_dir) & wanted
    fresh = wanted - known - retries
    to_ask = sorted(fresh | retries)

    resolved: dict[int, tuple[int, str]] = {}
    unresolved: list[int] = []
    for player_id in to_ask:
        record = client.fetch_athlete(player_id, season)
        if record is None:
            unresolved.append(player_id)
            continue
        answer = first_nfl_season(record)
        if answer is not None:
            resolved[player_id] = answer
            continue
        # No draft class and no debut year — an undrafted player ESPN states nothing about.
        # The earliest season he has statistics for is still worth having: it cannot prove he
        # IS a rookie, because a player who recorded nothing shows up late, but it proves he is
        # not one whenever it lands before the season in question. Recorded under its own
        # source so no reader can mistake the bound for the fact.
        earliest = client.fetch_first_stats_season(player_id)
        if earliest is None:
            unresolved.append(player_id)
            continue
        resolved[player_id] = (earliest, OriginSource.FIRST_STATS_SEASON.value)

    # Counted over players nobody has asked about before, NOT over everyone asked. The retry
    # set is players ESPN has already said it has nothing for, and it resolving nothing is the
    # steady state, not a symptom — every run after the first is mostly retries, so counting
    # them here would fail the nightly every single night. Found by running it twice.
    if len(fresh) >= MIN_ATTEMPTS_FOR_DEGRADED_CHECK and not any(
        player_id in resolved for player_id in fresh
    ):
        raise EspnError(
            f"asked ESPN about {len(fresh)} players never seen before and resolved none — "
            f"treating that as a degraded response rather than a league with no draft classes, "
            f"and writing nothing. Check the core API and the parser before re-running."
        )

    merged = merge(document, resolved, unresolved)
    if write:
        dump_json(merged, origins_path(derived_dir))
    return merged, sorted(resolved), sorted(unresolved), len(to_ask)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Record when each player's NFL career began, to data/derived/."
    )
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--all",
        action="store_true",
        help="sweep every synced season and every recorded claim, not just --year",
    )
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = parser.parse_args(argv)

    try:
        document, resolved, unresolved, attempted = sync_origins(
            args.year, write=not args.dry_run, every_season=args.all
        )
    except EspnError as exc:
        # EspnError messages never carry credentials, and the core client never sends any.
        print(f"origins sync failed: {exc}", file=sys.stderr)
        return 1

    print(f"asked ESPN about {attempted} players")
    print(f"  resolved {len(resolved)} newly")
    print(f"  {len(document['players'])} players on record, {len(document['unresolved'])} unresolved")
    for player_id in unresolved:
        print(f"  UNRESOLVED: player {player_id} — ESPN has no draft.year and no debutYear")
    for warning in (document.get("review") or {}).get("warnings") or []:
        print(f"  REVIEW: {warning}")
    if args.dry_run:
        print("dry run — nothing written")
    else:
        print(f"wrote {origins_path(DERIVED)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
