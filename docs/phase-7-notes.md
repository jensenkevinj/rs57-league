# Phase 7 — prospect eligibility from ESPN's draft class

Rule 1 (**a prospect must be a rookie**) was the one rule the system could not check.
`validate_team_claims` had taken a `seasons_played` mapping since Phase 5 and
`PROSPECT_TOO_MANY_SEASONS` was fully implemented behind it, but no production caller ever
passed the mapping, so the check silently passed. It is now checked.

Read the Corrections log entry dated 2026-08-01 first. The short version: the repo asserted in
six places that ESPN carries no rookie year, and that assertion came from a **trimmed test
fixture** rather than from the API.

## What to know before touching this

**`experience.years` is a trap.** Accrued seasons, not a draft class. Jawhar Jordan was drafted
in 2024 and reports 1. It also ignores the season in the request URL. `first_nfl_season` reads
`draft.year` then `debutYear`, never that, and `test_experience_years_is_never_read` replays
his real payload to keep it so.

**The arithmetic is `claim.season - first_nfl_season`, with no `+ 1`.** A prospect is kept from
the season just completed, so a genuine rookie gives exactly 1. The engine owns the subtraction
so it exists once rather than in each of three callers.

**The page and the claim answer different questions.** A `KeeperClaim` states its season, so it
needs no phase logic. The public grid has a pool and no claim, so it derives
`decision_season = season + 1 if drafted else season` — the same `drafted` pivot that decides
`base_salary_field`, for the same reason. Every prospect rule keys off
`qualifying_season = decision_season - 1`, read from one place.

**Rule 2 is provisional mid-season.** Before that year's trade deadline has passed, every
rostered player trivially clears a deadline that has not happened. It settles on its own.

## The file

`data/derived/player-origins.json`, written by `python -m rs57.origins_sync` and nothing else.
Merge-only, so a failed run adds nobody and removes nobody. Three states, expressed in the
schema rather than in a nullable field: a resolved player is a row, an unresolved one is in
`unresolved`, a never-fetched one is in neither. `PlayerOrigin.first_nfl_season` is required.

Deliberately not a field on `Player`: `sync.season_document` rewrites a season file wholesale,
so a degraded core API on the night of the keeper deadline would blank the field league-wide.

Fully reconstructible from ESPN. If a value is ever wrong, delete the file in a commit that
says why and let the nightly rebuild it.

## Three sources, and only two of them may confer eligibility

`draft.year` and `debutYear` **state** a first season. The earliest season in
`.../athletes/{id}/statisticslog` only **bounds** it: a player rostered without recording
anything appears a season late. Across the roster the log agreed with the draft class 159 of
162 times and all three misses were late by one.

So a bound rules a player out and never rules him in. `EXACT_SOURCES` is the split,
`load_player_origins()` is exact-only and feeds the engine, `load_first_season_bounds()` is
everything and may only produce "not eligible". Do not merge them for convenience.

A **D/ST** is not an unknown — negative id, 404 by construction, never prospectable. Settled
"not eligible" by position, before ESPN is consulted at all.

## Open
- One unresolved id league-wide: Zach Ertz (15835), retired and purged from ESPN's core API
  — season-less and older-season paths 404 too. On no current roster. Genuinely unknowable,
  and reported as such.
- `tests/data/espn_2025.json`'s player objects are still trimmed to four keys. That trimming
  is what produced the wrong conclusion; re-recording them whole would stop the next one.
- A bad claim in `data/manual/claims.json` still makes the nightly's validate step discard the
  run's good derived ESPN files. Pre-existing, and worth its own look.
