# Phase 1 handoff — ESPN sync

Written at the end of Phase 0. Everything here is context a fresh session cannot recover from
the repo alone. Read `CLAUDE.md` first; it holds the rules. This holds the open questions.

> **RESOLVED 2026-07-28 — see [espn-field-semantics.md](espn-field-semantics.md).** Read
> `keeperValue` before a season's draft and `keeperValueFuture` after; `draftDetail.drafted`
> decides. The section below is kept as the record of what was open and why. Its three test
> players turned out not to discriminate — all three read the same under either field. The
> 2025 auction record (`bidAmount`) is what settled it.

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

Also resolved along the way, and now derived from data instead of guessed:

- **`kept_prior_year`** comes from the previous season's draft picks, which carry a `keeper`
  flag (`view=mDraftDetail`). This retires the old script's hand-maintained list of *names* —
  the thing that under-charged James Cook. A player kept into last season's auction stays
  taxed unless he is back via `ADD`, which is a drop's fingerprint; `TRADE` keeps the tax, as
  the rules require.
- **`acquisitionType`** emits only `DRAFT`, `ADD` and `TRADE`. The `WAIVER`/`FAAB` spellings
  the note below asks about do not appear, and ESPN does not say which an `ADD` was.
- **The trade deadline** is on `settings.tradeSettings.deadlineDate` (epoch ms), so the
  prospect rule does not need it hand-entered.

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

> **CONFIRMED 2026-07-28 — no credentials are needed anywhere,** including historical seasons
> and the FAAB transaction log. Details in [espn-field-semantics.md](espn-field-semantics.md).
> Two things that *look* like auth failures and are not: `mTransactions2` returns `200` with
> the array missing unless you pass `scoringPeriodId`, and the `leagueHistory` path 404s while
> the per-season `seasons/{year}` path serves the same data back to 2019. Use the per-season
> path for the history backfill. `EspnClient.from_env` reads `ESPN_S2`/`SWID` if they are ever
> required.

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

### Status 2026-07-28: tab diff RUN, and it found a real bug

It was first closed without running the diff, on the argument that ESPN's own auction and FAAB
records were stronger evidence than a sheet dumped from the same source. **That argument was
wrong**, and the record of why is worth keeping.

The workbook was then read directly (it is reachable — see "Reading the workbook" below) and
the diff produced a genuine defect: **Tyjae Spears was being taxed $5 he did not owe.** He was
kept in the PROSPECT slot in 2025, and `draftSettings.keeperCount` is 4 — three keepers plus a
prospect — with ESPN marking all four `keeper: True` and nothing on the pick saying which slot
it filled. Every ESPN-only check agreed with itself and missed it, because ESPN does not hold
the distinction at all. Only the workbook's `Fee Allocations` tab records the slot.

The lesson generalises: **cross-checks against the same source cannot find what that source
does not know.** The tab was not redundant; it was the only independent witness for slot.

Fixed by `build_season(prior_prospect_ids=...)`, fed from `data/manual/prospects.json`, with a
loud warning when prospects are unknown rather than a silent tax. Locked by
`test_a_prospect_keep_is_not_taxed` and `test_matches_the_workbook_keeper_column`.

What the ESPN-side checks did confirm, and still stand:

- **Drafted players' bases** against the 2025 auction record (`picks[].bidAmount`) — the two
  agree, and every row that does not is a drop-and-re-add whose base correctly reset.
- **All 80 waiver bases** against the FAAB transaction log — 80/80, no mismatches. Re-checked
  on every sync, so a future drift shows up as `waiver_base_mismatches`.
- **`kept_prior_year`** against ESPN's own draft keeper flags, then cross-checked against the
  old script's hand list: 28 agree, and all 3 divergences are the sync being right (James Cook
  III, Tyjae Spears, a stale Jayden Daniels).

**Franchise assignment, the one thing the checks above could not confirm,** was then closed
separately against the draft record's `picks[].teamId` — a source independent of the per-team
`mRoster` reads the sync uses. All 100 players still held via `DRAFT` sit on the franchise that
drafted them: **0 mismatches.**

That makes every roster entry independently witnessed:

| Source | n | Confirmed against |
|---|---|---|
| `draft` | 100 | auction `bidAmount` **and** `teamId` — 100/100 on both |
| `waiver` | 80 | FAAB transaction log — 80/80 |
| `trade` | 8 | original auction or FAAB price — 8/8, and a trade does not reprice |
| | **188** | |

### Reading the workbook

It is reachable through the Drive tooling, by id — `Keepers` is
`1ypljsxlVVRE1PzZqmfYufCFXu_l9J8hCw-rnl571r8Y`. No export step, no local OAuth token, and no
need to ask for a CSV. An earlier session assumed otherwise and waived the acceptance check on
that assumption; don't repeat it.

Two things to know before diffing against it:

- **Roster membership will not match, and that is expected.** The tab is a snapshot from
  whenever the old script last ran (acquisition dates stop at Nov 19), while ESPN's roster
  endpoint always returns the *current* roster. Cooking Rice alone differs by seven players.
  Compare `Base` and `Kept` for players present in both; do not compare row counts.
- **Jayden Daniels is `Kept=TRUE` in the tab and on nobody's roster now.** Dropped after the
  sheet was written. Not a disagreement.

### The three live overrides are the normal state, not a defect

The workbook's `Manually Changed Salaries` tab lists three un-reverted overrides. **This is how
the league runs** — the commissioner confirmed it on 2026-07-28. ESPN holds the distorted value
on purpose until he reverts it before the next draft, which is exactly what
`SalaryOverride.reverted = False` means. Do not "fix" these.

| Player | ESPN base | actual | delta |
|---|---|---|---|
| Jonathan Taylor | 33 | 32 | −1 |
| Jaxon Smith-Njigba | 18 | 19 | +1 |
| Saquon Barkley | 68 | 71 | **+3** |

They net to **+$3**, which is precisely the unpaired Saquon Barkley row `CLAUDE.md` describes:
Taylor and Smith-Njigba are a matched draft-cash pair that cancels, and Saquon's +3 is the
orphan whose counterparty nobody can identify. So `check_override_balance` will pass against
this exact set once they are loaded, with Saquon flagged `unpaired_ok`.

They are **not** loaded — `data/manual/` has no overrides file — so `effective_base_salary` has
nothing to apply and those three players carry ESPN's distorted base in `data/derived/`. Left
that way by decision, not oversight. The sync is doing the right thing: it never bakes an
override into the base and always defers to the engine.

The ratchet risk `CLAUDE.md` warns about is handled by the commissioner's own process — he
reverts in ESPN before the auction, so a re-sync after the draft picks up clean values. The
sync already prints that reminder while a season is undrafted.
