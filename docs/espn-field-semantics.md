# Resolved: `keeperValue` vs `keeperValueFuture`

Settled 2026-07-28 against live ESPN data for league 535631, seasons 2025 and 2026. This
closes the open question in `phase-1-notes.md` and the `TODO` in the old `keepers.py`.

## The answer

ESPN reports both fields relative to **the season you are asking about**:

| Field | Meaning in season *Y*'s payload |
|---|---|
| `keeperValue` | the value carried **in** from *Y−1* |
| `keeperValueFuture` | the value established **in** *Y* — auction bid, FAAB, or $0 waiver |

They are the same number seen from adjacent seasons. Confirmed exactly: **2026's
`keeperValue` equals 2025's `keeperValueFuture` for all 188 rostered players** (106 of them
in cases where the two 2025 fields disagree, so it is not a coincidence of equal values).

`keeperValueFuture` is `0` league-wide until that season's draft happens — 188/188 zeros in
the 2026 payload today, because the 2026 auction has not been held.

### Correction, found during Phase 5: this only holds while a season is UNDRAFTED

The table above was settled against 2026, which had not drafted. It does **not** generalise
backwards. **Once a season has drafted, ESPN overwrites its `keeperValue` to equal its
`keeperValueFuture`** — both then hold what that season's own auction charged, and the
carried-in price is no longer in the payload at all.

Puka Nacua, read live:

| Season | `keeperValue` | `keeperValueFuture` | drafted |
|---|---|---|---|
| 2024 | 0 | 0 | yes |
| 2025 | **5** | 5 | yes |
| 2026 | 5 | 0 | no |

His 2025 `keeperValue` should be his 2024 salary of `$0` under the table above. It reads `$5`,
which is what the 2025 auction charged. Only the undrafted 2026 row still shows a genuine
carried-in value.

So **the carried-in price for season *Y* is `keeperValueFuture(Y−1)`** — read from the previous
season's payload — and it cannot be recovered from *Y*'s own once *Y* has drafted.

This is exactly the failure mode `CLAUDE.md` warns about: it does not fail loudly. Building a
completed season's carried-in roster from its `keeperValue` makes every keeper appear to have
carried in precisely what he was charged, so `check_base_continuity` reports the entire league
as off by exactly its own fees and taxes. It was caught only by running the audit and reading
what it said — fifteen REVIEW items that were all off by $5 or by one fee tier.

`rs57.validate.check_carried_in_prices` is now the tripwire: a frozen season's carried-in
prices must equal the previous season's charged prices, and the two are recorded in separate
files so they can disagree.

### The rule for `RosterEntry.base_salary`

```
base_salary(Y) = keeperValueFuture  if season Y has been drafted
                 keeperValue        otherwise
```

`draftDetail.drafted` is the flag, and it is in the API (`view=mDraftDetail`). This is the
programmatic answer the old script's TODO asked for. The switch is **not** "before vs during
the season" — it is *which season's payload you are reading*, which the payload itself tells
you. There is no manual toggle and no date arithmetic.

## Why — the auction record is the ground truth

This is an auction league, so `view=mDraftDetail` records what every drafted player actually
cost: `picks[].bidAmount`. Checked against both candidates for 2025:

| | matched the 2025 bid |
|---|---|
| `keeperValue` | 33 / 133 |
| `keeperValueFuture` | **106 / 133** |

Restricted to the players where the two fields disagree, `keeperValueFuture` matched the bid
**73 times and `keeperValue` zero times.** `keeperValue` matched only where it happened to
equal `keeperValueFuture` anyway.

The 27 non-matching rows are not field errors — they are players drafted, dropped, and
re-added mid-season, whose value correctly reset to the new waiver/FAAB value per the
drop-and-re-add rule in `CLAUDE.md`.

### Ruling out "`keeperValueFuture` is an ESPN projection"

The 2022 cached payload made `keeperValueFuture` look like a market projection with a $1
floor, and that reading is what the handoff notes flagged as unresolved. It no longer holds.
The zero-distribution settles it:

| acquisitionType | n | zeros | min |
|---|---|---|---|
| DRAFT | 100 | **0** | 1 |
| ADD | 80 | **60** | 0 |
| TRADE | 8 | 1 | 0 |

Every drafted player is ≥ $1 — nobody wins an auction player for $0 — and 60 of 80 waiver
adds are exactly $0. That is a distribution of *money actually paid*. A projection with a $1
floor cannot produce 61 zeros, and a projection could not match 106 auction bids on the nose.

**ESPN changed the semantics between 2022 and 2025.** The old script's reasoning was correct
for the era it was written in, which is why the cached files disagree with live data. The
handoff note's instruction not to resolve this from the cached files was the right call.

## Why the notes' three test players could not decide it

`Puka Nacua $5`, `Drake London $24`, `Bucky Irving $7` all read correctly — but all three have
`keeperValue == keeperValueFuture` in 2025, so they agree under either field and discriminate
nothing. They are a good regression check, not a discriminator. `tests/test_espn.py` keeps
them plus players where the fields *do* diverge.

## What this means for the ratchet

Reading season 2026 today gives `keeperValue` = each player's **2025** salary, which is
exactly the base the ratchet needs: Puka Nacua $5, and the engine adds the $5 tax to price
his 2026 keep at $10. This matches `CLAUDE.md` ("ESPN reports his current base as $5").

ESPN does **not** apply the league's $5 tax itself — Nacua's 2025 `keeperValueFuture` is $5,
not $10 — so there is no double-count when `keeper_rules` adds it.

## Waiver adds: confirmed against the FAAB record

Every waiver add's base equals the FAAB actually bid for him — **80 of 80 in 2025, no
exceptions.** Tyrone Tracy Jr.'s $79 and Emanuel Wilson's $21 are real winning bids, not
artefacts.

This is the independent confirmation the auction record could not give. `bidAmount` on a draft
pick only covers drafted players; a waiver base had no other witness. Now it has one, and
`sync` checks every waiver base against it on each run rather than assuming it.

Only `EXECUTED` transactions count — losing claims are recorded too, and counting one would
invent money nobody spent. Where a player was added more than once, the latest add is the one
that set his base.

### The `scoringPeriodId` trap

`view=mTransactions2` **needs a `scoringPeriodId`**. Without one ESPN answers `200 OK` and
silently omits the `transactions` array:

```
?view=mTransactions2                        -> 200, no transactions key
?scoringPeriodId=3&view=mTransactions2      -> 200, transactions present
```

That failure mode reads exactly like a permissions problem and will send you off configuring
cookies you do not need. It is not auth. A transaction can appear under several scoring
periods, so de-duplicate on `id` — `EspnClient.fetch_transactions` sweeps 0–18 and does this.

The `x-fantasy-filter` header is a separate red herring: the league filter accepts only
`players`, `transactions`, `communication`, `schedule`, and an unrecognised key gives a real
`400`. No filter is needed here at all.

## Authentication is not required — for any of it

Every endpoint this pipeline uses answers unauthenticated, including the historical seasons:

| Endpoint | Unauthenticated |
|---|---|
| current season `mSettings` / `mTeam` / `mRoster` | works |
| `mDraftDetail` (auction prices, keeper flags) | works |
| `mTransactions2` + `scoringPeriodId` (FAAB) | works |
| `seasons/{year}/segments/0/leagues/{id}` back to 2019 | works |
| `leagueHistory/{id}?seasonId=…` | **404 — wrong path shape, not auth** |

Phase 5 established the far end of that range: **2019 is the oldest season served.** 2018
answers `401` (so it may be reachable with cookies, untested) and 2017 and earlier answer
`404`. Rosters, draft records, FAAB transactions and box scores are all complete for
2019-2025. Note 2019 and 2020 ran **13** regular-season weeks, not 14 —
`scheduleSettings.matchupPeriodCount` says so, and anything that assumes 14 is wrong for
those two.

The `leagueHistory` route 404s for every season tried. That is a path-shape problem, not a
permissions one — auth failures here return 401, and the per-season `seasons/{year}` path
returns the same league fine. **Use the per-season path for the history backfill.**

`EspnClient.from_env` still reads `ESPN_S2`/`SWID` and sends them when both are set, so
cookies remain available if ESPN ever tightens up. Nothing needs them today.
