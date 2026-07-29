# Phase 2 handoff — derived stats + validation

> **CLOSED 2026-07-28.** Every open question below is resolved; see "Found during Phase 2" in
> §13 of `rs57-league-app-plan.md` for the answers and `CLAUDE.md` for the prize rules
> themselves. The acceptance criterion **was run**: 2025 reproduces the `RS57` sheet exactly,
> and 2024 does too as an independent second check. `data/private/` was not needed and was not
> created. The section below is kept as the record of what was open and why.

Written at the end of Phase 1. Everything here is context a fresh session cannot recover from
the repo alone. Read `CLAUDE.md` first; it holds the rules. `docs/espn-field-semantics.md`
holds the ESPN field semantics Phase 1 settled. This holds what Phase 2 needs and what is
still open.

Phase 2 is: weekly high scores, season points, positional studs, survivor, standings, and
`validate.py` wired into CI. *Done when* recomputing 2025 reproduces the `RS57` sheet's prize
winners exactly.

## Opening prompt

Paste this to start Phase 2 in a fresh session.

> I'm building a fantasy football league app. Read `CLAUDE.md` and `docs/phase-2-notes.md`
> in full, then execute **Phase 2 only**: derived stats + validation.
>
> Scope: weekly high scores, season points, positional studs, survivor, Unlucky, and
> standings, plus `validate.py` wired into CI. Stop there — no site generation, no admin
> tool, no history backfill.
>
> Read `rs57/espn.py` before writing any ESPN code; Phase 1 already settled the endpoints,
> the auth question, and the field semantics. `docs/espn-field-semantics.md` matters only if
> you touch `base_salary` — don't, in this phase.
>
> Constraints that are not negotiable:
> - `keeper_rules.py` stays pure. Put new stats in their own module; don't import I/O into it.
> - The repo and site are PUBLIC. No real manager names, ever. `data/private/` is deferred
>   by decision — do not create it, and do not design around having it.
> - Everything keys on `espn_team_id`. Report winners as franchise names, not people.
> - The nightly Action owns `data/derived/`. Locally use `--dry-run`.
> - A REVIEW issue must never pass silently as if it had been checked.
>
> Done when recomputing 2025 reproduces the `RS57` sheet's prize winners exactly. **Actually
> run that check** — the sheet is readable directly through the Drive tooling by id, no
> export needed. Phase 1 waived its equivalent check as "redundant" and that was wrong: it
> was hiding a real $5 bug, because a cross-check against one source cannot find what that
> source does not record. Read the Phase 1 entry in `§13 Corrections` of
> `rs57-league-app-plan.md` before you decide any verification is unnecessary.
>
> Before writing any code, tell me anything in the prize rules that's ambiguous or that
> you'd have to guess at.

That last line is the same one that opened Phase 0, and it earns its place again — several
prize rules are recorded nowhere, and each one changes what gets built:

- **Survivor's elimination rule.** The sheet records name/week pairs through week 11 but never
  says what causes elimination. Lowest score that week, presumably.
- **Ties on a weekly high score** — split the $10, or a tiebreak?
- **Most Points (Season)** — regular season only, or through the playoffs? The tab header says
  "Thru: Week 17" while the high scores stop at 14.
- **`Unlucky`'s window** — weeks 1-14 like the high scores, or all 17?
- **Positional studs** — the prize appears to follow the manager who *started* him, which
  decides whether `mBoxscore` lineup slots are needed at all.

Phase 1's opening prompt is not recorded, and deliberately isn't being backfilled — that phase
is closed and nobody will read it. The transferable part of it was one instruction: *resolve
the `keeperValue` vs `keeperValueFuture` question first, before writing the sync.* Front-loading
the single decision everything rests on is what stopped it being settled implicitly, halfway
through an implementation. For Phase 2 that decision is the ambiguity list above.

## The `RS57` sheet is readable — and it is an input, not just an acceptance target

`1ez6Hf1-vUIkj4rnuZR09a1Z6qIefzy-A5UkVchNrxh8`, owned by the commissioner. Prize money is
league-specific and appears nowhere in ESPN, so this sheet is the **only** source for the
payout structure. Phase 1's acceptance target (the `Keepers` tab) could be waived because
ESPN's own auction and FAAB records were stronger evidence. **That does not apply here** —
there is no second source for what anyone was paid.

Read it directly rather than re-deriving. One tab per season (`2025`, `2024`, `2023`), plus
standings tabs and a large unrelated side-bet ledger from 2016-2019 that is **out of scope** —
those are personal prop bets between members, not league payouts. Don't model them.

### The payout structure, as recorded

| Prize | 2025 | 2024 | 2023 |
|---|---|---|---|
| Champion | $500 | $500 | $500 |
| 2nd Place | $200 | $200 | $200 |
| 3rd Place | $100 | $100 | $100 |
| Most Points (Season) | $100 | $100 | $100 |
| Survivor | $40 | $40 | **$50** |
| QB / RB / WR / TE Stud | $25 each | $25 each | $25 each |
| **Unlucky** | $20 | $20 | $20 |
| Week 1-14 High Score | $10 each | $10 each | **$9.29 each** |
| | $1,200 | $1,200 | $1,200.06 |

Three things in that table matter more than they look:

- **`Unlucky` is a real $20 prize that the plan doc does not list.** §9 enumerates "champion,
  2nd, 3rd, most points, survivor, positional studs, and weekly high scores" and stops. Build
  from this table, not from §9, or you will be $20 short every season.
- **Amounts are per-season, not constants.** 2023 paid Survivor $50 and weekly high scores
  $9.29. `Payout` already carries `season`, `label` and `amount` — keep it that way and resist
  hard-coding a dollar figure anywhere. The $9.29 was a one-off: when the NFL went to 18 weeks
  the pot was divided across the extra weeks, and **the league has since gone back to whole
  dollars.** So `Payout.amount` stays `NonNegMoney` — integer dollars, no model change. 2023 is
  the only season that cannot be represented, and it is a Phase 5 backfill problem, not a
  Phase 2 one. Phase 2's target season, 2025, is whole dollars throughout.
- **Weekly high scores run weeks 1-14 only**, which lines up with ESPN's
  `scheduleSettings.matchupPeriodCount: 14`. Don't pay weeks 15-17.

### Definitions the sheet reveals, that nothing else records

- **Positional studs are a single best week, not a season total.** Each stud row is followed by
  a row naming the player, the week, and the score — 2025's QB stud is Josh Allen at 42.68 in
  W11; the WR stud is Puka Nacua at 40.5 in W16. So it is `max(single-week score)` per position
  across the league, and the prize goes to the manager who started him.
- **`Unlucky` goes to the highest score that still lost its matchup** — confirmed by the
  commissioner. It is a **season-long award, not a weekly one**: one $20 prize for the single
  highest losing score all season, which is why the row beneath it holds one week and one score
  (2025: W14, 127.86; 2024: W14, 141.4). Computing it per week would pay it fourteen times.
  It needs matchup results, not just weekly totals — a high score only counts if it *lost*.
- **Survivor** is tracked as a name/week elimination table, one row per manager, winner last
  standing. Weeks 1-11 appear as elimination weeks, so it runs the first eleven weeks.

## Manager identity is the blocker, and it is a privacy question

The sheet names winners by **first name** ("Champion, $500, Jack"). The standings tabs carry
**full real names** alongside team names. Nothing keys on `espn_team_id`, which is what this
codebase keys on.

So Phase 2 needs a `first name -> espn_team_id` mapping, and that mapping is exactly the kind
of thing `CLAUDE.md` puts in `data/private/`. **The repo and site are public.** Do not commit
real names, and do not copy the standings tabs into the repo verbatim — publish display names
only.

Note the standings tabs are also **stale**: they carry team names from an older season
(`Titan's Pans`, `Pajama Sam`, `Hinkie Died For Your Sins`) that no longer match the 2026 names
the sync returns. Do not use them to build the mapping. Use `data/private/` plus
`espn_team_id`.

## What ESPN gives you for the scoring side

Untouched by Phase 1 and unverified — this is Phase 2's first job. The endpoints are the same
league path already in `rs57/espn.py`, with different views:

```
?view=mMatchupScore&view=mScoreboard       # weekly scores per matchup
?view=mBoxscore&scoringPeriodId={week}     # per-player scoring, for positional studs
?view=mStandings&view=mTeam                # standings, PF/PA, streaks
```

`mBoxscore` is what positional studs need, since the prize is about a *started* player's single
week. Confirm the started-vs-benched distinction is available before designing around it —
`lineupSlotId` on a roster entry is the likely signal, and the bench slot id needs checking.

Two things Phase 1 already established that apply here:

- **`scoringPeriodId` is load-bearing.** `mTransactions2` silently returns no array without it.
  Assume other week-scoped views behave the same way, and check the response shape rather than
  trusting a `200`.
- **No credentials are needed**, including for historical seasons via
  `seasons/{year}/segments/0/leagues/535631`. The `leagueHistory` route 404s — that is a path
  shape problem, not auth.

`WeeklyScore.points` is a `float` on purpose. It is the one place decimals are correct; money
stays integer dollars everywhere else. Note 2023's $9.29 weekly prize is a real fractional
dollar amount, so `Payout.amount` being `NonNegMoney` will reject it — **decide whether to
store 2023 payouts in cents, round, or widen the model, and record the choice.**

## `validate.py`

CI has a placeholder comment for it already (`.github/workflows/ci.yml`): cross-file reference
checks, no orphans, no duplicate keeper slots, fee sums correct. It should also run
`check_base_continuity()` and `check_override_balance()` from `keeper_rules`, both of which sit
unused today because `data/history/` is empty.

## Still open

- **`data/private/` is deferred by decision (2026-07-28).** Real names must not enter the repo,
  and the commissioner would rather revisit the mapping later than build it now. So **do not
  create it, and do not design Phase 2 around having it.** Build the stats that key on
  `espn_team_id` alone — weekly high scores, season points, positional studs, survivor,
  standings — and leave payout *attribution* until the mapping exists. The prize structure and
  amounts above need no names; only tying a winner to a manager does.

  This still leaves the acceptance criterion reachable. Report each 2025 winner as a
  **franchise name**, which is already in `data/derived/` and publishable, and let the
  commissioner check those against the sheet's first names by eye. That is a one-time human
  step, not a mapping the repo has to hold — and it is the same shape as Phase 1's workbook
  diff, which is worth actually running this time.
- **`Season.consolation_winner_id` has no source.** The keeper engine already consumes it for
  the fee waiver, and it is currently unpopulated for every season. Standings data may supply
  it — the consolation bracket is a playoff result, so check `mStandings` before assuming it
  has to be typed by hand.

## Phase 1 leftovers worth knowing

- `data/derived/` is written **only** by the Action. Locally, run
  `python -m rs57.sync --year 2026 --dry-run` — it reports without writing, so it cannot leave
  an untracked file for someone to commit by accident.
- The Phase 1 acceptance diff against the `Keepers` tab was **never run** — closed instead on
  ESPN's auction and FAAB records, which are stronger. See `phase-1-notes.md`. Drive access
  does exist, so that diff is runnable at any time if a keeper number is ever disputed.
