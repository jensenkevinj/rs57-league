# RS57 League App

12-team ESPN keeper/auction fantasy football league. Manages keeper salaries, prize
tracking, and league history. NOT live scoring — that stays on ESPN.

All six build phases are complete. The plan and its Corrections log are in
`rs57-league-app-plan.md`; each phase's handoff is in `docs/phase-*-notes.md`. Read the
Corrections log before deciding any verification is unnecessary — every phase had an
acceptance check that was tempting to wave through, and each one was hiding a real bug.

## Architecture
- Python throughout. JSON files in data/ are the store — no database.
- **THREE WRITERS, ONE DIRECTORY EACH. NO FILE HAS TWO WRITERS.**
  - Nightly GitHub Action → `data/derived/` and `site/`
  - Local admin tool (`python -m rs57.admin`) → `data/manual/`
  - Backfill importer (`python -m rs57.backfill`) → `data/history/`

  Each refuses to write outside its own directory and the nightly fails its run if
  anything else wrote one. Never commit derived/ or site/ from the laptop. Locally use
  `python -m rs57.sync --year <yr> --dry-run` (keepers),
  `python -m rs57.stats_sync --year <yr> --dry-run` (stats) and
  `python -m rs57.origins_sync --year <yr> --dry-run` (draft classes), which report without
  writing. **Three writers, three files** — `{year}.json`, `{year}-stats.json` and
  `player-origins.json` — so a broken box score cannot blank a season of salaries, and a
  core-API outage cannot touch either. `player-origins.json` is merge-only on top of that:
  a failed run adds nobody and removes nobody.
  - **`data/derived/` was hand-corrected once, deliberately: 2026-09-02.** The 17:16 nightly
    copied in 27 entered keeper prices before `hold_entered_bases` existed, and a merge-only
    guard freezes wrong numbers in place rather than repairing them. The clean bases were
    restored from commit `50d06a6` — the last sync taken before the prices went in — and every
    one checked against `recorded claim − fee − tax`, which is an independent source. **A hand
    edit in `data/derived/` without a note like this beside it is a bug.**
- `python -m rs57.validate` reads data/ and reports. It is never a writer. CI runs it.
- ESPN reads need no credentials — public league, historical seasons included.
  **2019 is the oldest season served**; 2018 answers 401 and earlier 404.
  `rs57/espn.py` does the I/O; `keeper_rules.py` must never import it.
- **Two ESPN hosts.** The fantasy API (`lm-api-reads.fantasy.espn.com`) for everything about
  the league, and the **core** API (`sports.core.api.espn.com/v2/.../athletes/{id}`) for one
  fact the fantasy API does not carry in any view: when a player's NFL career began.
  - `draft.year` is the **draft class** — immutable, authoritative, ~93% of rostered players.
    `debutYear` is the fallback for the undrafted. Both public, no auth; the core client
    deliberately has nowhere to put a cookie.
  - **Two of the three sources are exact and one is a bound, and they must not be mixed.**
    `.../athletes/{id}/statisticslog` gives the earliest season a player has statistics for.
    It agreed with the draft class 159 times of 162 and every miss was **late by one** — a
    player rostered without recording anything shows up a season after he arrived. So a bound
    proves somebody is **not** a rookie and can never prove that he is. `EXACT_SOURCES` in
    `models.py` is the split; `load_player_origins()` returns exact only and is what the
    engine takes, `load_first_season_bounds()` returns everything and may only rule players out.
  - **A D/ST is never prospectable.** Negative id, 404 by construction — the question does not
    apply, so it is a settled "not eligible", never an unknown.
  - **`experience.years` is a trap and is never read.** It counts *accrued* seasons, not a
    draft class — Jawhar Jordan was drafted in 2024 and reports 1 — and it ignores the season
    in the request URL entirely. Reading it makes third-year players prospect-eligible.
  - Written to `data/derived/player-origins.json` by `python -m rs57.origins_sync`, which is
    its **only** writer and the only module that touches the core host. Merge-only: a value is
    never overwritten, so an outage costs nothing already recorded. Locally use `--dry-run`.
- Serialize with `dump_json()` from rs57.models — sort_keys=True, stable ordering,
  trailing newline. Diffs must stay readable.

### data/history/ — written once per completed season, then frozen
`HistoryStore.write_season` **raises** on a season already on disk rather than warning: a
warning is something you read after the write has happened. Correcting a frozen season is a
deliberate act — delete the file in a commit that says why, then import it again.

Freezing a completed season: `python -m rs57.backfill --year <yr>`. It refuses a season that
has not finished, and it does **not** commit. Then clear that season out of the admin tool, so
one fact has one record; `validate` reports the duplication until you do.

Where a frozen season's claims come from depends on the era: 2024 and 2025 are transcribed from
the `Keepers` workbook's two `Fee Allocations` tabs, 2019-2023 have none and carry no claims,
and **from 2026 on the admin tool is the record** and its rows are copied across verbatim.

## Security
- The repo and site are PUBLIC.
- **No manager real names, emails, or handles anywhere in the repo.** Publish franchise
  names only, keyed on `espn_team_id`. `data/private/` was considered and **deliberately
  never created** — do not create it and do not design around having it. The `RS57` sheet's
  standings tabs carry full real names; read them if you must, never copy them in.
- ESPN espn_s2/SWID come from env or Actions secrets. NEVER log them — Action
  logs on a public repo are public.
- Jinja autoescaping stays on. No |safe on manual text fields. `SalaryOverride.reason` is
  free text a human types and the site renders — it is the injection path.

## Rules
- Max 3 keepers + 1 prospect per team
- salary = base + allocated fee + $5 if kept last year
- Fee tiers: 1 keeper → $0, 2 → $5, 3 → $15, distributed freely by the manager
- Consolation bracket winner: fees waived one year (salaries still owed in full).
  **The winner is the top finisher among the teams that missed the playoffs** — best
  `rankCalculatedFinal` among ESPN's `LOSERS_CONSOLATION_LADDER`, which is exactly that set.
  NOT "won the last ladder game" (2025 would name three) and NOT "went undefeated" (2024's
  ladder had four teams at 2-1). Confirmed against 2023/2024/2025; 2024 returns
  `Bijan's Mustard`, which is the `*` in the 2025 fee allocations.
  - This is the algorithm, not the trigger. The waiver only fires once the **prior** season's
    `Season.consolation_winner_id` is recorded in `data/manual/seasons.json` — a derived guess
    must never quietly waive a real team's fees. Unrecorded, `fees_waived_for()` prices that
    year's keepers with fees ON and reports the waiver unconfirmed rather than applying it.
- Keeper deadline: **shown and stamped, never enforced** (commissioner, 2026-09-01). It was a
  lock for four weeks (commissioner, 2026-08-04) on the reasoning that no salary is entered
  before the deadline, so a lock costs nothing. That premise was wrong. **ESPN publishes keeper
  selections to nobody but an authenticated league member** — every draft pick reads
  `keeper: false` and `mKeeperRosters` carries no marker, so ESPN's own League Keepers page
  prints `Selection Hidden` — which makes hand entry the tool's only input path, and it happens
  in exactly the window the lock closed. What the lock protected is now *reported*: a claim
  whose `submitted_at` precedes the deadline renders as provisional REVIEW until it is
  re-recorded. Weaker guarantee, wider net — the lock said nothing about the claims stamped
  2026-07-29, six days before the lock existed. `KeeperDeadline` is display-only and no route
  may gate on it. **Where it is shown is Season settings** — read-only, straight off ESPN, on
  the league clock. The keeper board printed it too until the actions moved to the top of that
  screen (commissioner, 2026-09-02); what the board carries instead is the per-claim provisional
  REVIEW, which is the half that has a consequence. The unrecorded state is still a REVIEW note
  on the board, because a missing deadline is a gap in the season's record. **The deadline and the draft
  date are ESPN facts, not admin fields** (commissioner, 2026-08-26) — `DerivedSeason.keeper_deadline`
  and `.draft_date` come straight from `draftSettings.keeperDeadlineDate` / `.date`, synced into
  `data/derived/{year}.json` by the nightly Action the same way `trade_deadline` already was.
  `Season.keeper_deadline` and `Season.draft_date` no longer exist and there is no admin UI to
  set either one: a hand-typed copy is a second record of a number ESPN already holds, and the
  two had already drifted apart once. An unrecorded deadline (ESPN hasn't set one, or the season
  hasn't synced) stays a distinct state from a future one — a missing fact is not the same as a
  future one, and a season with no deadline on file cannot call any claim provisional against it.
- Prospects: **must be a rookie**, rostered before the trade deadline, kept at
  acquisition value, no fee allocation. **Prospects may be started** — the old
  "never started by any league team" rule is retired, as is the allowance for
  second-year players (commissioner, 2026-07-30).
  - **The rookie rule is checked**, from ESPN's draft class — see below. It is REVIEW, not
    ERROR: the draft class comes from outside the league, so the final call is the
    commissioner's, in the admin console (commissioner, 2026-08-01).
  - A **repeat claim** stays ERROR and is the independent cross-check: nobody has two rookie
    seasons, and that is derivable from the league's own claims with no outside source.
    *The league's own record blocks; an outside data source flags.*
  - Neither applies before `PROSPECT_RULES_TIGHTENED`; the record holds legal repeats.
  - `elapsed = claim.season - first_nfl_season`, and `> 1` is the violation. **Not `+ 1`** —
    a prospect is kept *from the season just completed*, so a genuine rookie gives exactly 1.
- There is NO salary cap. Don't add one, don't leave a disabled one lying around.

### The ratchet — the thing most likely to be got wrong

`RosterEntry.base_salary` is **what the player cost his manager THIS season**, not his
original acquisition value. Keepers enter ESPN's auction at their keeper price, so ESPN's
per-season value already carries every prior fee and tax forward. Next season's base is this
season's computed salary — base, fee, and tax all roll.

Puka Nacua: acquired off waivers at $0 → kept for $0 in 2024 (no tax on a first keep) → kept
for $5 in 2025 → $10 in 2026. ESPN reports his current base as $5.

`check_base_continuity()` audits this: a kept player's base must equal last season's computed
salary unless an override explains it.

**Between the keeper deadline and the auction, ESPN's field is not ESPN's.** The commissioner
types the season's keeper prices into ESPN in that window — base + allocated fee + $5 tax, added
together — and the field hands them straight back. A sync that copies them in charges every kept
player his own fee and tax twice, and the ratchet then carries the doubling forward for good.
`sync.hold_entered_bases` is the guard: in that window the nightly keeps the salaries already on
disk and overwrites nothing, reporting how many it held. **Everything else in the file still
syncs** — rosters, dates, acquisitions. One field stops being ESPN's to give, not the season.
Because of it, every reader may trust `base_salary` unconditionally; nothing downstream needs to
know what week it is. It was briefly the other way round — `site.py` subtracted the tax in that
window for a few hours on 2026-09-02 — and a decoding rule in the readers is how you end up with
four copies of the same calendar test and a $5 error in whichever one you missed.

**Which ESPN field is the base depends on whether that season has drafted** — `keeperValue`
before the auction, `keeperValueFuture` after, decided by `draftDetail.drafted`. Getting this
wrong does not fail loudly; it silently reprices the whole league and compounds every year.
Settled against the auction record in `docs/espn-field-semantics.md` — read it before touching
anything that sets `base_salary`.

**And `keeperValue` stops being the carried-in price once a season drafts.** ESPN overwrites it
with `keeperValueFuture`, so a *completed* season's payload no longer holds what its keepers
carried in — that is `keeperValueFuture(Y-1)`, read from the previous season. This is why a
frozen season stores `roster_carried_in` separately. Reading it wrong makes every keeper appear
to have carried in exactly what he was charged, and the audit then reports the whole league as
off by precisely its own fees and taxes. `check_carried_in_prices` is the tripwire.

**The audit needs the carried-in base on one side and a claim on the other, and the claim must
be independent.** A `computed_salary` derived from ESPN's own `bidAmount` is tautological —
next season's `keeperValue` equals that bid by construction, so it would compare a number
against itself and report a clean ratchet having checked nothing.

### The $5 tax
- Waived on a **drop**, NOT on a **trade** — it follows the player across trades, because it
  is a property of the player's history, not of who holds him now.
- A **drop + re-add by the same manager is a full reset**: tax cleared AND base becomes the
  new waiver/FAAB value.
- A **prospect keep never sets the tax flag.** A player kept in the PROSPECT slot starts the
  next season untaxed.

### Salary overrides are draft-cash trades
Managers trade draft cash. ESPN has no native support, so the commissioner hand-edits a few
player salaries on the two teams involved and changes them back before the next draft.

`SalaryOverride.actual_salary` is the TRUE value; ESPN holds the distorted one while
`reverted=False`. This is **not** a patch for ESPN reporting a wrong acquisition value — the
plan doc says that and the plan doc is wrong.

Un-reverted overrides should net to zero league-wide (`check_override_balance`), since a cash
trade moves money between two teams. One historical row has an unrecoverable counterparty and
is flagged `unpaired_ok`.

#### The trade is recorded, not just its legs

An override is one **leg**. `CashTrade` in `data/manual/trades.json` is the trade itself — two
franchises, an amount, a direction, keyed by **draft year** — and a leg names it through
`SalaryOverride.trade_ids`. A leg carrying any `trade_ids` is excluded from
`check_override_balance` so it is not reported twice.

**A leg can belong to several trades, and that is the normal case.** One salary edit routinely
expresses more than one deal: a franchise owing $1 from one and $2 from another gets a single
$3 edit, and a franchise that both pays and receives $5 gets no edit at all. `trade_ids` is
plural for that reason.

**What netting costs, stated plainly:** per-trade balancing is then impossible *in principle* —
nothing records which dollar belonged to which deal, and splitting one would be recording a
number nobody decided. So the unit of audit is **a franchise's net across the trades that share
edits**:

    expected[manager] = Σ over the group's trades (−amount to payer, +amount to payee)
    actual[manager]   = Σ of that manager's live legs' (actual_salary − espn_base)

Trades group by *shared edits*, not by shared franchises — otherwise one bad trade would redden
every other between the same pair. A trade netted with nothing is a group of one, and then
`expected` is `{payer: −amount, payee: +amount}`: exactly the two-sided check this replaced.
Nothing got weaker for the simple case.

**The sign convention is the thing to get right.** `amount` moves FROM `from_manager_id` TO
`to_manager_id`. The receiving team is the one ESPN **under**-charges — its player's salary is
edited *below* the true figure, which frees that much auction budget — so the receiving leg's
`actual_salary - espn_base` is **positive** and the paying leg's is negative.

Settled against the record: the 2025 workbook's Jonathan Taylor (ESPN $33, true $32, so `-1`,
Bijan's Mustard) and Jaxon Smith-Njigba (ESPN $18, true $19, so `+1`, Jaxian McJigberson) are
one $1 trade and cancel exactly. Saquon Barkley's `+3` is the orphan.

Summed **per franchise, never league-wide**. A league-wide net of zero is also what two legs on
one team give, and that moves no budget at all.

`trade_ids` may be empty and every finding is REVIEW, deliberately: the rows predating this file
are legitimately unlinked, and an ERROR would turn the existing record red for a rule it was
recorded before. A trade whose legs are all reverted is **finished, not broken** — that is the
intended end state and it reports nothing.

The workbook's three `Manually Changed Salaries` rows (Saquon Barkley, Jaxon Smith-Njigba,
Jonathan Taylor, all 2025) **have since been entered** and are the whole of `overrides.json`.
A duplicate set was briefly recorded against 2026 as well, on the theory that ESPN carried the
distortion forward; every one of those had `actual_salary` equal to ESPN's own 2026 base, so
they asserted nothing and were deleted (commissioner, 2026-08-10).

**One thing about them is unresolved.** All six rows are stored `reverted: true`, but the
workbook says `changed back? FALSE`. If ESPN was never actually put back, `reverted: true` is
wrong in the costly direction: it drops them from `check_override_balance`, from
`check_cash_trades`, and from `effective_base_salary`, so a live distortion would ratchet
forward every season while reading as settled. Confirm against ESPN before trusting the flag.
Open items live in `docs/open-reconciliations.md`.

## Prize rules

None of these are written down anywhere but the `RS57` sheet, and the plan doc's payout list is
wrong. The engine is `rs57/stats.py`; the ground truth is `fixtures/prize_cases.json`.
Amounts are **per-season, not constants** — they live in `data/manual/payouts.json`.

- **Weekly high score** — top score of the week, **the regular season only**. Not the playoffs.
  That is 14 weeks today but **13 in 2019 and 2020** — take it from
  `scheduleSettings.matchupPeriodCount`, never hardcode it.
- **Most Points (Season)** — the same regular-season window, matching ESPN's `team.points`.
  2025 cannot tell 1-14 from 1-17; 2023 can, and says 1-14. The tab header reading
  "Thru: Week 17" is wrong.
- **Positional stud** — best single **started** week by any player at the position, over the
  **whole season including the playoff weeks**. Not a season total. 2025's WR stud is W16 and
  its TE stud is W15, which is what settles the window. Started = `lineupSlotId` not in
  {20 bench, 21 IR}. QB/RB/WR/TE only; DEF wins nothing.
- **Survivor** — lowest score among the **still-alive** goes out each week, last standing
  wins. Runs weeks 1 to `teams - 1`. Derive the window from the league size, don't hardcode 11.
- **Unlucky** ($20, which the plan doc omits) — the single **highest score that still lost**,
  **once per season, not once per week**. Computing it weekly would pay it fourteen times.
  Regular season only (commissioner, 2026-07-28). A tie is not a loss.
- **Champion / 2nd / 3rd** — ESPN's `rankCalculatedFinal`, cross-checked against the winners'
  bracket final.
- **Ties split the prize evenly** (commissioner, 2026-07-28). No tie has ever occurred. Money
  is integer dollars, so an indivisible split pays the floor and reports the remainder as
  REVIEW — never a float, never rounded away.

Everything keys on `espn_team_id`. The sheet names winners by first name; **that mapping is
not in this repo and must not be added.** Report winners as franchise names.

`data/manual/payouts.json` holds amounts for **2024 and 2025 only**. 2023 is deliberately
absent — its $9.29 weekly prize is not an integer dollar and widening `Payout.amount` to a
float would put floats into every salary in the league. 2019-2022 predate the `RS57` sheet
entirely and have no source. Those seasons derive stats and award nothing, which `validate`
reports as REVIEW rather than passing over.

**Whether a prize was actually paid is a separate fact from what it pays**, and it is recorded
**per franchise, not per prize.** Prizes accrue all season and are settled in one payment at the
end of it, so "has the Week 6 high score been paid?" is a question nobody asks and could not
answer — the money moves once, in aggregate (commissioner, 2026-08-31). `Payment` in
`data/manual/payments.json` is keyed on `(season, manager_id)`, exactly like `Dues`, and what a
franchise is owed is the **sum of what it won**, computed from the derived payout rows rather
than stored. No amount field, and no payment method, handle, or note: public repo.

`Payout.paid` is derived and is not that record — `stats.award_prizes` computes it and the
nightly Action rewrites it every run, so a flag set there vanishes on the next sync.

**`PAYOUT_LEDGER_FROM` (2026) is the boundary.** Every earlier season was paid out and
reconciled outside this repo and is settled (commissioner, 2026-08-31). The console renders
those as *settled* and the endpoint refuses them, because printing a red "not paid" against a
2021 franchise invents a debt the league does not have. `check_payouts` reports a row against
an earlier season as REVIEW rather than accepting it.

A franchise that **won nothing is owed nothing** and is not a line on the settlement sheet; the
endpoint refuses a payout to one. Such a row would be invisible on screen and therefore
impossible to undo there — a payment that never happened, recorded permanently.

## Dues — money paid IN

Three things in this repo are called money and only two of them are. Keep them apart:

- **Dues** (`data/manual/dues.json`, the `Dues` model) — real dollars a manager pays **into**
  the league for a season. Recorded at the start of it.
- **Prize payments** (`data/manual/payments.json`, `Payment`) — real dollars paid **out** to a
  franchise. Recorded at the end of the same season, per franchise and in one lump: see the
  Prize rules section and `PAYOUT_LEDGER_FROM`.
- **Keeper fees, the $5 tax, and draft-cash trades** — **not money at all.** Auction budget,
  which never changes hands. The word "fee" is taken by this meaning throughout; do not
  overload it for dues.

Dues and prizes are the same season's two ends, which is why one admin screen — the **Money**
tab, `/season/<year>/money` — shows both, keyed on one year. The nav lands on `current_season`
because that is where collection happens; a season still being played has live dues and an
empty prize half, and that half fills in on the same page once the season derives. The old
`/season/<year>/payouts` URL redirects there. **Draft cash** is the separate tab next to it and
is auction budget, per above.

**Dues record no amount, and no franchise-level anything else.** A row is
`(season, manager_id, paid, paid_at)`. The buy-in is one figure the whole league knows and
nothing in `data/` records it today — putting it on twelve rows would be twelve copies of a
number nobody has written down once, and the panel exists to say who still owes, not what it
costs. No payment method, handle, or note either: public repo.

**Absence means unpaid.** Un-marking removes the row rather than storing `paid: false`, so a
mistyped id cannot linger after it is undone. This is why `all_paid` must be counted against
the **season's franchise list**, never against the dues file — zero of twelve would otherwise
read as the finished state and hide the panel exactly when it is most wanted.

The admin tool refuses a `manager_id` that has no franchise in that season: the season's own
derived file is the league's own record, so it blocks rather than flags. `check_dues` in
`validate.py` is the second line, and reports SKIPPED — never silence — when a season it names
has no derived file to check against.

The public homepage panel shows dues only, and **removes itself once every franchise has paid**
(decided in `site.build_site`, not in the template). Prize *handoff* status stays admin-only.

## Conventions
- Key franchises on espn_team_id. Display names change yearly and are unreliable
  (one carries a double space: `Belichick's  Spy`).
- **Match players on espn_player_id, never on name.** This has already cost real money:
  the current spreadsheet under-charges James Cook by $5 because its keeper list says
  `James Cook` and ESPN now returns `James Cook III`.
- All money is integer dollars. No floats.
- **Every stored datetime is naive UTC; every displayed one is Eastern.** `espn._epoch_ms`
  converts on the way in and naive is deliberate — `keeper_rules` compares `acquired_at`
  against `trade_deadline` directly, and an aware value would raise from inside the engine.
  What was missing for a while was the way back: nothing converted for display, so the 2026
  home page published the draft as 9/4 when ESPN says 9/3 at 9pm ET. Every league date is in
  the evening, so UTC has already crossed midnight and *all of them* printed a day late,
  including every auction-day acquisition on the keepers page. `models.to_league_time()` is
  the one border — the `mdy` filter on the site and the `et` filter in the admin tool both go
  through it, and `models.utc_now()` is the only clock allowed to meet a stored deadline.
  It is a real tz database and not a fixed offset: the draft is in September and the trade
  deadline is in December, so a hardcoded `-5` is wrong for half the calendar.
  - **`CashTrade.agreed_at` is the exception and must never be converted.** It is a calendar
    date a human types into a date input, not an instant — converting it walks the date
    backwards a day every time the form is reopened.
- Load JSON into Pydantic models immediately. Never pass raw dicts around.
- Models enforce types; keeper_rules enforces rules. A negative fee is a ValidationIssue,
  not a Pydantic error.
- keeper_rules.py and stats.py stay pure — no I/O, no Flask, no ESPN. They are the tested core,
  and `tests/test_purity.py` enforces it.
- Validation issues are ERROR (blocks) or REVIEW (needs the commissioner's eyes, renders as
  unverified). Never let a REVIEW item pass silently as if it had been checked.
- **A check that cannot run is reported as SKIPPED, with the reason.** Silence reads exactly
  like success. This is the rule the whole validator is built around.
- **Where a number cannot be known, record no number.** `KeeperClaim.computed_salary` is
  optional and the audit skips unpriced claims, so an unknowable figure is left out rather
  than guessed. Guessing one manufactured three confident false findings in Phase 5.

## Testing
`fixtures/keeper_cases.json` and `fixtures/prize_cases.json` are hand-built ground truth
reviewed by the commissioner. The Keepers workbook cannot supply the first — its Fee
Allocations tabs are live VLOOKUPs that recompute against today's roster, so they are not a
record of what anyone actually paid.

If a fixture disagrees with the engine, that is a bug or an undocumented rule. Chase it down;
do not adjust the fixture.

**Mutation-check anything that guards an invariant.** `test_admin.py`, `test_site.py`,
`test_history.py` and `test_validate.py` all do: removing a guard has to make a specific test
fail, verified rather than assumed. A test that cannot fail is worse than no test, and in
Phase 5 four supposedly-covered guards turned out not to be — each gap was a missing check,
not just a missing test.
