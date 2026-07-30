# Phase 4 handoff — the admin tool

Written at the end of Phase 3. Everything here is context a fresh session cannot recover from
the repo alone. Read `CLAUDE.md` first; it holds the rules. `docs/phase-3-notes.md` records
what the site and the nightly Action settled and why. This holds what Phase 4 needs and what is
still open.

Phase 4 is: Flask + HTMX on localhost, keeper claim entry with live salary math, salary
overrides with reasons, payout tracking, season settings, and one button that validates,
commits and pushes `data/manual/`. *Done when* an offseason runs without opening a spreadsheet.

## Opening prompt

Paste this to start Phase 4 in a fresh session.

> I'm building a fantasy football league app. Read `CLAUDE.md` and `docs/phase-4-notes.md` in
> full, then execute **Phase 4 only**: the local admin tool.
>
> Scope: a Flask + HTMX app that runs on localhost and writes `data/manual/` — keeper claim
> entry with live salary math and validation, salary overrides with reasons, payout tracking,
> season settings, and one button to validate, commit and push. Stop there — no history
> backfill, no changes to the keeper or stats engines, no changes to the nightly Action or the
> site generator beyond what consuming new `KeeperClaim` rows requires.
>
> Read `rs57/keeper_rules.py` and `rs57/site.py` before writing any of it. `compute_team_keepers`
> already returns priced keepers and structured issues in a single pass — that function is the
> screen. Do not reimplement any part of the salary formula in a view, a template, or
> JavaScript.
>
> Constraints that are not negotiable:
> - `keeper_rules.py` and `stats.py` stay pure. The admin tool imports them, never the reverse,
>   and neither one learns about Flask.
> - **This tool is the ONLY writer of `data/manual/`.** It must never write `data/derived/` or
>   `site/` — those belong to the nightly Action — and never `data/history/`, which is frozen.
> - **The repo is PUBLIC and so is everything this tool commits.** Franchise names only. No
>   manager first names, no full names, no emails. `data/private/` does not exist and is not to
>   be created. It runs on localhost, which is not a reason to relax any of this — its output
>   is a public commit.
> - Everything keys on `espn_team_id`; `manager_id` is `t{espn_team_id}`. Render franchise names
>   from `FranchiseName` rows, and never key on one.
> - Serialize with `dump_json()` — sorted keys, trailing newline. A commit button that produces
>   an unreadable diff is worse than no commit button.
> - Jinja autoescaping stays on. **`SalaryOverride.reason` is free text a human types into this
>   tool** — it is the injection path, and it is rendered on the public site.
> - An ERROR blocks a claim; a REVIEW never blocks but must never render as though it had been
>   checked.
>
> Done when an offseason runs without opening a spreadsheet. **Actually drive the tool and
> submit a real claim** — every phase so far has had an acceptance check a session was tempted
> to wave through, and Phase 1 and Phase 3 were each hiding a real bug that only running the
> check found. Read the Corrections log of `rs57-league-app-plan.md` before you decide any
> verification is unnecessary.
>
> Before writing any code, tell me anything about the claim workflow that's ambiguous or that
> you'd have to guess at — especially anything that decides what a manager owes.

That last line follows Phases 0, 2 and 3. What is unrecorded here is not the salary formula —
that is settled and tested — but the **workflow around it**: who submits, when it locks, what
happens to a claim after the deadline, and whether the tool is the record or merely a shortcut
into ESPN. See "Still open" below.

## What Phase 3 leaves you

### `compute_team_keepers` is the screen

`keeper_rules.compute_team_keepers(claims, roster, overrides, ...)` prices every claim **and**
validates the team in one pass, returning `TeamKeeperResult` with `.keepers`, `.issues`,
`.total_salary`, `.total_fees` and `.blocked`. It deliberately prices claims even when the team
has blocking errors, because "you owe $5 more in fees" is far more useful next to the salaries
than instead of them. That is the HTMX partial: post the form, call the function, re-render the
fragment. There is no second implementation to write.

The site generator (`rs57/site.py`) shows the shape of a consumer that gets this right — it
hands templates numbers that are already computed and lets the templates loop and format. There
is a test, `test_no_template_does_arithmetic_on_money`, that greps templates for arithmetic on
money. **Point it at the admin tool's templates too.**

### There are no `KeeperClaim` rows anywhere yet

This is the thing Phase 4 creates. Until it does:

- The site publishes each rostered player's *potential* keeper price — `base + $5 tax`, with no
  fee — and says in as many words that nobody has declared anything. Once real claims exist,
  that page should distinguish declared keepers from priced candidates. That is a small change
  to `build_keeper_season`, and it is the one site change Phase 4 legitimately owns.
- `validate.py`'s `check_base_continuity` and `check_override_balance` have **never executed**.
  They report SKIPPED every run because they need recorded claims to compare against. They are
  wired in and tested; they are simply starved. Phase 4 feeding them is what finally audits the
  ratchet.
- `Season.consolation_winner_id` has no store. 2025's winner is *derived* into
  `review.consolation_winner_manager_ids` and the site renders it as unconfirmed, explicitly not
  applied to anyone's fees. **Season settings is where it gets recorded**, and recording it is
  what turns a derived guess into a decision. Read it off by one year and every waiver lands on
  the wrong team: `Season(year=2024).consolation_winner_id` waives fees in **2025**.

### `data/manual/` today

Two files, both hand-written, both read-only to everything else:

- `payouts.json` — prize amounts per season. 2023 is deliberately absent; its $9.29 weekly prize
  is not an integer dollar. Do not add it, and do not widen `Payout.amount` to a float.
- `prospects.json` — players kept in the PROSPECT slot, by season. It exists only because ESPN
  cannot tell a keeper from a prospect: `draftSettings.keeperCount` is 4 and all four picks are
  marked `keeper: True` with nothing recording which slot each filled. **Once this tool records
  claims with slots, derive it from `slot == PROSPECT` and delete the file** — its own header
  comment says so.

Both have `_about` keys holding prose. Whatever writes them must preserve those, or the next
person loses the only explanation of why 2023 is missing.

### The nightly Action, and what it must never see

`.github/workflows/nightly.yml` owns `data/derived/` and `site/`. It has a step that fails the
run if anything wrote `data/manual/` or `data/history/`, checked against the working tree.
**The reverse guard is Phase 4's to write**: this tool must fail just as loudly if it is ever
pointed at `derived/` or `history/`.

Its scheduled sync covers the current season only. Completed seasons are populated once by a
`workflow_dispatch` run with an explicit season list.

## Rules the admin tool cannot break

- **`SalaryOverride.reason` is the injection path.** It is free text typed by a human into this
  tool, stored in `data/manual/`, committed to a public repo, and rendered on a public site.
  Autoescaping on, no `safe` filter, no exceptions.
- **A salary override is a draft-cash trade, not a correction.** `actual_salary` is the TRUE
  value and ESPN holds the distorted one while `reverted` is False. The UI must not invite
  anyone to "fix a wrong ESPN value" — that is what the plan doc says and the plan doc is wrong.
  Un-reverted overrides should net to zero league-wide; one historical row has an unrecoverable
  counterparty and is flagged `unpaired_ok`.
- **A negative fee is a `ValidationIssue`, not a form error.** `KeeperClaim.fee_allocated` is
  plain `Money` for exactly this reason: the engine reports `NEGATIVE_FEE` alongside every other
  rule problem rather than raising from inside a JSON load. Do not add a `min=0` on the input and
  call it validated.
- **Never let a REVIEW pass silently.** Every prospect claim raises
  `PROSPECT_START_HISTORY_UNVERIFIED`, because prospect rule 2 — never started by any team in the
  league — cannot be checked without box-score history, which is Phase 5. Show it. A prospect
  screen that looks clean is lying.
- **There is no salary cap.** Don't add one, don't add a disabled one, don't show a budget bar.

## Still open — decide these before building

- **Who uses this?** The plan says localhost and one button to commit and push, which reads like
  a commissioner-only tool where managers still send their keepers by text. If managers are meant
  to enter their own claims, that is not a localhost Flask app and the whole shape changes. Ask
  before building a login.
- **What locks, and when?** `Season.keeper_deadline` exists in the model and nothing reads it.
  Whether a claim can be edited after the deadline, and whether the tool enforces that or merely
  records it, is a commissioner call.
- **Is the tool the record, or a shortcut into ESPN?** A declared keeper eventually has to be
  entered into ESPN's auction at his keeper price. If the tool is the record and ESPN is entered
  by hand from it, the two can drift, and `check_base_continuity` is what will find that drift a
  year later. Worth deciding deliberately rather than discovering.
- **Where do claims live?** `data/manual/claims.json` keyed by season is the obvious answer, but
  `data/history/` is where a *completed* season's claims are meant to freeze. Which season is
  "current" and when a season graduates from `manual/` to `history/` is a Phase 5 boundary that
  is easier to agree now than to migrate later.
- **Does the commit button push to `main` directly?** Every phase so far has committed to `main`.
  A tool that pushes on a button press with no review is a different risk profile from a tool
  that opens a PR.

## Leftovers worth knowing

- **Scheduled Actions get auto-disabled after roughly 60 days without a commit**, which is
  exactly what this repo looks like in June and July. Expect to re-enable the nightly workflow
  each August. This is in the plan's Risks section and is now a live concern rather than a
  hypothetical one.
- **`CLAUDE.md` still describes `data/private/` as the home for manager emails**, and
  `models.py`'s `Manager` docstring points at it. Phase 2 decided the directory was not needed
  and never created it, and Phase 3's constraints forbid creating it. The `.gitignore` entry is
  harmless belt-and-braces; the prose is stale. Raised, deliberately not edited — `CLAUDE.md` is
  the commissioner's file.
- **The `RS57` sheet is readable through the Drive tooling by id**
  (`1ez6Hf1-vUIkj4rnuZR09a1Z6qIefzy-A5UkVchNrxh8`), no export needed. Three phases have now
  confirmed this and two earlier ones wrongly doubted it. Its standings tabs carry **full real
  names** — read them if you must, never copy them into the repo.
- **`fixtures/keeper_cases.json` and `fixtures/prize_cases.json` are hand-built ground truth**
  reviewed by the commissioner. If a fixture disagrees with the engine, that is a bug or an
  undocumented rule. Chase it down; do not adjust the fixture.
- **The site's own test suite is a template for this one.** `tests/test_site.py` mutation-checks
  its safety rules — dropping the REVIEW flags, adding a `safe` filter, computing money in a
  template, and disabling autoescaping each make a specific test fail. A test that cannot fail is
  worse than no test, and that was verified rather than assumed.
