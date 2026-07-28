# Phase 1 handoff — ESPN sync

Written at the end of Phase 0. Everything here is context a fresh session cannot recover from
the repo alone. Read `CLAUDE.md` first; it holds the rules. This holds the open questions.

## The one that matters: which ESPN field is the base?

`RosterEntry.base_salary` is **what the player cost his manager this season**. The whole keeper
ratchet rests on reading the right ESPN field into it, and getting it wrong compounds silently
for years rather than failing loudly.

The old script uses `playerPoolEntry.keeperValueFuture`. In the cached 2022 payload
(`~/Developer/rs57/team1_response_0823.json`) the two candidates diverge hard:

| Player | `keeperValue` | `keeperValueFuture` |
|---|---|---|
| Ezekiel Elliott | 73 | 29 |
| Brandin Cooks | 10 | 32 |
| T.J. Hockenson | 24 | 6 |
| Mike Evans | 29 | 37 |
| Colts D/ST (waiver add) | 0 | 1 |

In that 2022 snapshot `keeperValue` looks like acquisition price (0 for waiver adds) and
`keeperValueFuture` looks like a projection with a $1 floor. But in the **2025** data the
script pulls with `keeperValueFuture`, the values look like acquisition prices — $0 for waiver
adds, $68 for Saquon. Either ESPN's semantics changed, or one of those readings is wrong.

**Do not resolve this from the cached files.** Pull both fields for the current season and check
them against a player whose history we know:

- **Puka Nacua** should read **$5** — his 2025 salary. (Kept for $0 in 2024, $5 in 2025.)
- **Drake London** should read **$24**, and **Bucky Irving $7**.

Whichever field produces those is the one. `check_base_continuity()` in `keeper_rules.py` is the
long-term guard: once `data/history/` has prior-season claims, it fails loudly if the field
drifts.

## The league is public — you may not need cookies at all

```
settings.isPublic: true      restrictionType: NONE      size: 12      draftSettings.type: AUCTION
draftSettings.keeperCount: 4        # 3 keepers + 1 prospect, as the rules say
```

The old script sends **no credentials** — no `espn_s2`, no `SWID`, no cookie jar — and works.
The plan doc treats cookie expiry as a headline risk (§5, §9); for the current-season read path
it may be a non-issue.

Verify before designing around it, and note the likely exception: historical seasons via the
`leagueHistory` endpoints are where auth usually starts mattering. If cookies do turn out to be
needed, every rule in `CLAUDE.md` about never logging them still stands — Action logs on a
public repo are public.

## The old script is a thinner spec than the plan doc expects

Plan §10 says to read `~/Developer/rs57/keepers.py` for "nine years of commissioner decisions
that never made it into the rules doc." Having read it: **they aren't in there.**

That script does exactly two things — emit each player's `Base` from ESPN, and set a
`KeptLastYear` boolean by testing the player's name against a hand-maintained list. All salary
math lived in spreadsheet formulas. The decisions the plan doc hoped to recover were settled by
asking the commissioner directly and are now recorded in `CLAUDE.md`.

The one real decision encoded in that file is the `keeperValue` / `keeperValueFuture` choice
above, and its comment admits uncertainty:

> `This needs to be keeperValue when running before the season and keeperValueFuture during
> the season. TODO: Can this be fixed programmatically?`

That TODO is Phase 1's actual job.

## API shape, as of the cached payloads

League `535631`. Host `lm-api-reads.fantasy.espn.com` (the old `fantasy.espn.com` host also
appears in commented-out code).

```
/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/535631?view=mSettings&view=mTeam&...
/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/535631?forTeamId={n}&view=mRoster
/apis/v3/games/ffl/seasons/{year}/?view=proTeamSchedules        # NFL team abbreviations
```

Roster entry fields worth having:

| Field | Use |
|---|---|
| `playerId` | **The join key. Match on this, never on name.** |
| `acquisitionType` | Maps to `AcquisitionSource`. Only `DRAFT` appears in the cached team; confirm `WAIVER` / `FAAB` / `TRADE` spellings against live data. |
| `acquisitionDate` | Epoch milliseconds. Needed for the prospect trade-deadline rule. |
| `playerPoolEntry.keeperValue` / `keeperValueFuture` | See above. |
| `player.fullName`, `defaultPositionId`, `proTeamId` | Display only. |

The plan doc calls for the `espn-api` package rather than raw `requests`. Both are viable — the
raw endpoints above are known to work unauthenticated. Worth confirming `espn-api` is still
maintained and handles this league's keeper fields before taking the dependency.

## Traps

- **A sync that "succeeds" with zero players would blank a season.** Fail loudly on an empty or
  short roster; do not write `derived/` from a degraded response.
- **Match on `espn_player_id`.** This has already cost real money: the current spreadsheet
  under-charges James Cook $5 because its keeper list says `James Cook` and ESPN now returns
  `James Cook III`.
- **Never let ESPN override an un-reverted `SalaryOverride`.** While `reverted=False`, ESPN
  holds a deliberately distorted salary from a draft-cash trade. If the sync treats ESPN as
  authoritative there, the distortion ratchets into that player's base permanently.
- **Serialize with `dump_json()`** from `rs57.models` — sorted keys, trailing newline. Without
  it every nightly commit looks like the whole file changed.
- **Freeze completed seasons.** Pull each one once into `data/history/`, then never re-fetch.
  A flaky historical endpoint should not be able to rewrite settled results.
- **Scheduled Actions get auto-disabled after ~60 days of repo inactivity** — which is exactly
  what June and July look like here. Expect to re-enable each August.

## Done when

Generated `data/derived/2026.json` matches the current `Keepers` tab row for row, overrides
aside — that tab is a direct dump of ESPN, so unlike Phase 0's fixtures it is a fair target.

Phase 0's acceptance criterion had to be rewritten because the `Fee Allocations` tabs turned out
to be live VLOOKUPs rather than records. The `Keepers` tab does not have that problem.
