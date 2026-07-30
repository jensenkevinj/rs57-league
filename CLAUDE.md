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
  `python -m rs57.sync --year <yr> --dry-run` (keepers) and
  `python -m rs57.stats_sync --year <yr> --dry-run` (stats), which report without writing.
  They write separate files — `{year}.json` and `{year}-stats.json` — so a broken box
  score cannot blank a season of salaries.
- `python -m rs57.validate` reads data/ and reports. It is never a writer. CI runs it.
- ESPN reads need no credentials — public league, historical seasons included.
  **2019 is the oldest season served**; 2018 answers 401 and earlier 404.
  `rs57/espn.py` does the I/O; `keeper_rules.py` must never import it.
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
- Prospects: **must be a rookie**, rostered before the trade deadline, kept at
  acquisition value, no fee allocation. **Prospects may be started** — the old
  "never started by any league team" rule is retired, as is the allowance for
  second-year players (commissioner, 2026-07-30). Nothing in data/ records a rookie
  year, so the enforceable form is a repeat claim: nobody has two rookie seasons.
  Not applied before `PROSPECT_RULES_TIGHTENED`; the record holds legal repeats.
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

**Three real ones are still unrecorded** — the workbook's `Manually Changed Salaries` rows
(Saquon Barkley, Jaxon Smith-Njigba, Jonathan Taylor, all season 2025, all still un-reverted).
They exist nowhere in `data/`, so the ratchet audit reports them every run. Entering them
through the admin tool clears them. Open items live in `docs/open-reconciliations.md`.

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

## Conventions
- Key franchises on espn_team_id. Display names change yearly and are unreliable
  (one carries a double space: `Belichick's  Spy`).
- **Match players on espn_player_id, never on name.** This has already cost real money:
  the current spreadsheet under-charges James Cook by $5 because its keeper list says
  `James Cook` and ESPN now returns `James Cook III`.
- All money is integer dollars. No floats.
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
