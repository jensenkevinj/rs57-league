# RS57 League App

12-team ESPN keeper/auction fantasy football league. Manages keeper salaries, prize
tracking, and league history. NOT live scoring — that stays on ESPN.

## Architecture
- Python throughout. JSON files in data/ are the store — no database.
- Nightly GitHub Action writes data/derived/ and site/. Local admin tool writes
  data/manual/. NO FILE HAS TWO WRITERS. Never make the Action touch manual/,
  and never commit derived/ or site/ from the laptop. Locally use
  `python -m rs57.sync --year <yr> --dry-run`, which reports without writing.
- ESPN reads need no credentials — public league, historical seasons included.
  `rs57/espn.py` does the I/O; `keeper_rules.py` must never import it.
- data/history/ is written once per completed season, then frozen.
- Serialize with `dump_json()` from rs57.models — sort_keys=True, stable ordering,
  trailing newline. Diffs must stay readable.

## Security
- The repo and site are PUBLIC.
- Manager emails live ONLY in data/private/ (gitignored). Never import that from
  the site generator. Publish display names only.
- ESPN espn_s2/SWID come from env or Actions secrets. NEVER log them — Action
  logs on a public repo are public.
- Jinja autoescaping stays on. No |safe on manual text fields.

## Rules
- Max 3 keepers + 1 prospect per team
- salary = base + allocated fee + $5 if kept last year
- Fee tiers: 1 keeper → $0, 2 → $5, 3 → $15, distributed freely by the manager
- Consolation bracket winner: fees waived one year (salaries still owed in full)
- Prospects: ≤1 NFL season, never started by any league team, rostered before the
  trade deadline, kept at acquisition value, no fee allocation
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
- keeper_rules.py stays pure — no I/O, no Flask, no ESPN. It is the tested core.
- Validation issues are ERROR (blocks) or REVIEW (needs the commissioner's eyes, renders as
  unverified). Never let a REVIEW item pass silently as if it had been checked.

## Testing
`fixtures/keeper_cases.json` is hand-built ground truth reviewed by the commissioner. The
Keepers workbook cannot supply it — its Fee Allocations tabs are live VLOOKUPs that recompute
against today's roster, so they are not a record of what anyone actually paid.

If a fixture disagrees with the engine, that is a bug or an undocumented rule. Chase it down;
do not adjust the fixture.
