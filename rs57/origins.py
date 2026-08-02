"""Reading ``data/derived/player-origins.json`` — when each player's NFL career began.

**Pure of network, deliberately.** This module reads and merges; ``rs57.origins_sync`` is the
only thing that fetches, and the only writer. It must never import ``rs57.espn`` — that
separation is what keeps a core-API outage out of every reader's failure domain.

Why a file of its own
---------------------

The alternative was a ``first_nfl_season`` field on ``Player`` inside every
``data/derived/{year}.json``. It is the wrong shape for two reasons.

``sync.season_document`` regenerates a season file **wholesale**, with no merge path. A
degraded core API on the night of the keeper deadline would rewrite that field as null for the
entire league in one quiet commit, and every prospect would flip from eligible to unknown.
That is the same failure the split between ``{year}.json`` and ``{year}-stats.json`` exists to
prevent: a broken feed must not blank a good one.

And a draft class never changes. One fact belongs in one record, fetched once ever, rather
than copied into eight season files that are then free to drift apart.

The three states
----------------

``first_nfl_season`` is required on :class:`~rs57.models.PlayerOrigin`, so this file has no way
to say "unknown" with a value. It says it with absence:

* **in** ``players``      — ESPN answered, and this is the season
* **in** ``unresolved``   — ESPN was asked and had neither a draft class nor a debut year
* **in neither**          — nobody has asked yet

``unresolved`` is not a cache of failures; it is the record that a question was put and came
back empty, which is what lets a screen say "ESPN has no draft class for him" rather than the
much weaker "not fetched". Those ids are retried on every run, because ESPN does backfill
``debutYear``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from rs57.models import OriginSource, PlayerOrigin

ORIGINS_FILENAME = "player-origins.json"

ABOUT = [
    "When each player's NFL career began, from ESPN's core athlete API.",
    "",
    "Written by `python -m rs57.origins_sync` and by nothing else. Merge-only: a value here",
    "is never overwritten, because a draft class is immutable. If one ever needs correcting,",
    "delete this file in a commit that says why and let the nightly rebuild it.",
    "",
    "players    - ESPN answered. first_nfl_season is the draft class (source=draft_year), or",
    "             the debut year for undrafted players (source=debut_year).",
    "unresolved - ESPN was ASKED and had neither field. Not a cache of failures: it is the",
    "             difference between 'ESPN has no draft class for him' and 'nobody asked'.",
    "             Retried every run, since ESPN does backfill debutYear.",
    "absent     - nobody has asked yet.",
    "",
    "experience.years is NOT read and must never be. It counts accrued seasons rather than a",
    "draft class -- Jawhar Jordan was drafted in 2024 and reports 1 -- and it ignores the",
    "season in the request URL entirely. Reading it makes third-year players prospect-eligible.",
]

ENDPOINT = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
    "/seasons/{season}/athletes/{espn_player_id}?lang=en&region=us"
)


class OriginsError(RuntimeError):
    """The origins file is unreadable, or a merge would have destroyed a recorded fact."""


def origins_path(derived_dir: Path) -> Path:
    return derived_dir / ORIGINS_FILENAME


def empty_document() -> dict[str, Any]:
    """A fresh document. Used by the first run and by tests; never written over a live file."""
    return {
        "_about": list(ABOUT),
        "source": {"endpoint": ENDPOINT, "fields": ["draft.year", "debutYear"]},
        "players": [],
        "unresolved": [],
        "review": {"warnings": []},
    }


def load_document(derived_dir: Path) -> dict[str, Any]:
    """The raw document, or an empty one when the file does not exist yet.

    A missing file is a legitimate state — the feature is new and the nightly has not run —
    and it is distinguishable from an empty one by ``players`` being empty either way, which
    is why callers that care use :func:`load_player_origins` and treat ``{}`` as "the rule
    applies and nothing is known", never as "the rule does not apply".
    """
    path = origins_path(derived_dir)
    if not path.exists():
        return empty_document()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OriginsError(f"{path} is not valid JSON: {exc}") from None


def load_origins(derived_dir: Path) -> list[PlayerOrigin]:
    """Every resolved player, as models. Raises rather than skipping a malformed row.

    A row that will not load is schema drift in a file this repo generates itself, and
    dropping it silently would turn a known player into an unknown one — which reads on the
    page as "we could not check", the exact thing this feature exists to stop saying.
    """
    document = load_document(derived_dir)
    return [PlayerOrigin.model_validate(row) for row in document.get("players") or []]


def load_player_origins(derived_dir: Path) -> dict[int, int]:
    """``espn_player_id -> first_nfl_season``, **exact sources only**.

    This is the mapping ``keeper_rules.validate_team_claims`` takes, and the only one allowed
    to decide that somebody **is** a rookie. Rows sourced from the statistics log are excluded
    on purpose: that value is an upper bound, and a bound that came out a season late would
    make a second-year player prospect-eligible. See ``models.EXACT_SOURCES``.

    Note what it does **not** contain: bounded, unresolved and never-fetched players are all
    absent, and the engine reports a prospect it cannot find as unverified rather than passing
    him. Use :func:`load_first_season_bounds` to rule such a player out.
    """
    return {
        origin.espn_player_id: origin.first_nfl_season
        for origin in load_origins(derived_dir)
        if origin.exact
    }


def load_first_season_bounds(derived_dir: Path) -> dict[int, int]:
    """``espn_player_id -> the earliest season his career can have begun``, every source.

    Exact rows are included too — an exact first season is trivially its own bound — so this
    is a superset of :func:`load_player_origins` and a caller needs only this one to answer
    "was he definitely already in the league?".

    **What it licenses.** ``bound < qualifying_season`` proves the player is not a rookie: his
    true first season is at or before the bound, so it is earlier still. ``bound ==
    qualifying_season`` proves nothing on its own, because a bounded value can be a season
    late — he might be a genuine rookie, or he might have spent the year before on a roster
    recording nothing. That case stays unknown, which is the honest answer.
    """
    return {
        origin.espn_player_id: origin.first_nfl_season for origin in load_origins(derived_dir)
    }


def unresolved_ids(derived_dir: Path) -> set[int]:
    """Players ESPN was asked about and had no first season for."""
    document = load_document(derived_dir)
    return {int(row["espn_player_id"]) for row in document.get("unresolved") or []}


def warnings(derived_dir: Path) -> list[str]:
    """REVIEW text carried inside the file, for ``validate`` to re-surface.

    A warning written into a derived file and never read is indistinguishable from no warning.
    """
    document = load_document(derived_dir)
    return list((document.get("review") or {}).get("warnings") or [])


def merge(
    existing: Mapping[str, Any],
    resolved: Mapping[int, tuple[int, str]],
    unresolved: Iterable[int],
) -> dict[str, Any]:
    """Fold a run's findings into the existing document. **Never destructive.**

    ``resolved`` maps player id to ``(first_nfl_season, source)``. Every player already in the
    document survives, whatever this run managed to fetch — which is what makes a partial
    failure safe by construction rather than by careful sequencing.

    A value that comes back **different** from the one on record is not adopted. A draft class
    is immutable, so a change means ESPN drift or a parser bug, and quietly taking the new
    number would reprice a keeper on the strength of whichever run happened to be last. The
    old value is kept and the disagreement is written to ``review.warnings``, where
    ``validate`` will surface it.
    """
    document = dict(existing)
    document.setdefault("_about", list(ABOUT))
    document.setdefault("source", {"endpoint": ENDPOINT, "fields": ["draft.year", "debutYear"]})

    on_record = {int(row["espn_player_id"]): row for row in document.get("players") or []}
    complaints: list[str] = []

    for player_id, (season, source) in resolved.items():
        previous = on_record.get(player_id)
        if previous is None:
            on_record[player_id] = {
                "espn_player_id": player_id,
                "first_nfl_season": season,
                "source": OriginSource(source).value,
            }
            continue
        if previous["first_nfl_season"] != season:
            complaints.append(
                f"ESPN now says player {player_id} began in {season}, but "
                f"{previous['first_nfl_season']} is on record from {previous['source']}. A draft "
                f"class does not change; the recorded value is kept and this needs a human."
            )

    # Resolving a player clears him from unresolved; he has an answer now.
    still_unresolved = {int(row["espn_player_id"]) for row in document.get("unresolved") or []}
    still_unresolved |= {int(player_id) for player_id in unresolved}
    still_unresolved -= set(on_record)

    was_on_record = len(document.get("players") or [])
    merged = dict(document)
    merged["players"] = sorted(on_record.values(), key=lambda row: row["espn_player_id"])
    merged["unresolved"] = [
        {"espn_player_id": player_id, "reason": "ESPN carries no draft.year and no debutYear"}
        for player_id in sorted(still_unresolved)
    ]
    review = dict(document.get("review") or {})
    review["warnings"] = complaints
    merged["review"] = review

    # The invariant this whole module rests on: a merge only ever adds. If this ever fires,
    # something has turned the merge into a replace and a run that fetched nothing would be
    # about to publish an empty league.
    if len(merged["players"]) < was_on_record:
        raise OriginsError(
            f"merge would drop {was_on_record - len(merged['players'])} recorded players — "
            f"refusing to write. A merge adds; it never removes."
        )
    return merged
