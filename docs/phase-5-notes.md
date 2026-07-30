# Phase 5 handoff — history and backfill

Written at the end of Phase 4. Everything here is context a fresh session cannot recover from the
repo alone. Read `CLAUDE.md` first; it holds the rules. `docs/phase-4-notes.md` records what the
admin tool settled and why. This holds what Phase 5 needs and what is still open.

Phase 5 is: import prior seasons from the archived sheets into `data/history/`, freeze them, and
add the box-score history that prospect rule 2 needs. *Done when* `check_base_continuity` runs
across every season the league has played and the ratchet is audited rather than asserted.

## Opening prompt

Paste this to start Phase 5 in a fresh session.

> I'm building a fantasy football league app. Read `CLAUDE.md` and `docs/phase-5-notes.md` in
> full, then execute **Phase 5 only**: history and backfill.
>
> Scope: import completed seasons into `data/history/` and freeze them, and add the box-score
> history that prospect rule 2 needs. Stop there — no changes to the keeper or stats engines, no
> changes to the nightly Action, the site generator, or the admin tool beyond what reading
> `data/history/` requires.
>
> Read `rs57/keeper_rules.py`, `rs57/validate.py` and `rs57/admin/store.py` before writing any of
> it. `check_base_continuity` is the reason this phase exists, and it compares a season's bases
> against the **previous** season's recorded claims — `validate.audit_ratchet` already does that
> pairing, so read it before you decide where a season's claims belong.
>
> Constraints that are not negotiable:
> - `keeper_rules.py` and `stats.py` stay pure. Everything imports them, never the reverse.
> - **`data/history/` is written once per completed season and then frozen.** Nothing may rewrite
>   a season that is already in there — not a re-sync, not a "fix", not this phase's own importer
>   run twice. The admin tool writes `data/manual/` and the nightly Action writes
>   `data/derived/`; a third writer with a fourth directory is not an excuse to relax the rule.
> - **The repo is PUBLIC.** Franchise names only. No manager first names, no full names, no
>   emails. The `RS57` sheet's standings tabs carry **full real names** — read them if you must,
>   never copy them into the repo. `data/private/` does not exist and is not to be created.
> - Everything keys on `espn_team_id`; `manager_id` is `t{espn_team_id}`. **Match players on
>   `espn_player_id`, never on name** — that mistake is already costing $5 a year in the sheet.
> - All money is integer dollars. 2023's $9.29 weekly prize still cannot be represented and that
>   is still not a reason to widen `Payout.amount`.
> - Serialize with `dump_json()`.
> - An ERROR blocks; a REVIEW never blocks but must never render as though it had been checked.
>
> Done when the ratchet is audited across every recorded season. **Actually run the audit and
> read what it reports** — every phase so far has had an acceptance check a session was tempted
> to wave through, and Phases 1, 3 and 4 were each hiding real bugs that only running the check
> found. Read the Corrections log of `rs57-league-app-plan.md` before you decide any verification
> is unnecessary.
>
> Before writing any code, tell me anything about the historical record that's ambiguous or that
> you'd have to guess at — especially anything where the sheet and ESPN could disagree.

That last line follows Phases 0, 2, 3 and 4. What is unrecorded here is not the rules — they are
settled and tested — but **which source wins when two records of the same season disagree**, and
that is a judgement rather than a fact to reproduce. See "Still open" below.

## What Phase 4 leaves you

### `KeeperClaim` rows now exist, in `data/manual/claims.json`

`python -m rs57.admin` records them: slot, fee allocation, and a `computed_salary` **frozen at
submission**. That frozen figure is the point — it is what the manager was told they owed, and it
is what `check_base_continuity` compares next season's ESPN base against. The tool recomputes on
every screen and flags a disagreement rather than rewriting the record.

One team's 2026 claims are recorded as a live acceptance test of the tool (t9, four claims, $88).
Treat them as real data, not a fixture.

### The ratchet audit is wired but still starved, for a different reason than before

`validate.audit_ratchet` pairs each synced season with the claims from the season *before* it.
With only 2026 claims recorded it reports:

```
SKIPPED check_base_continuity: claims exist for 2026 but no synced season follows one of
        them, so the ratchet is UNVERIFIED.
```

That is the honest answer and it is what Phase 5 changes. **The audit needs claims for season
Y and a synced roster for Y+1.** Backfilling 2025's claims is what finally lets it run, against
the 2026 roster that already exists. Backfilling 2024 lets it run twice.

Do not be tempted to pass all claims and all rosters at once to make it "run" — that compares a
season's base against its own recorded salary, a number against itself, and reports a clean
ratchet having checked nothing. There is a comment in `audit_ratchet` saying so.

### `data/manual/` today

Five files. Two were hand-written before Phase 4 and are still read-only to everything else;
three are the admin tool's.

- `payouts.json` — prize amounts per season, hand-transcribed. 2023 deliberately absent.
- `prospects.json` — prior prospect keeps, by season. **Still needed.** Its own header says to
  delete it once claims exist; that is premature and this phase is what finally makes it true.
  `sync.py`'s `prior_prospect_ids(year - 1)` reads it to keep a prospect keep untaxed, and
  deriving it from `slot == PROSPECT` needs claims for **completed** seasons — which is exactly
  what you are backfilling. Once 2025's claims are in `history/`, point `sync.py` at those and
  delete the file. Not before: deleting it today silently re-taxes every prospect.
- `claims.json`, `overrides.json`, `seasons.json`, `payments.json` — written by the admin tool.

Every one of them carries an `_about` key holding prose. `ManualStore` merges into the loaded
document rather than replacing it, so the prose survives a write. Whatever reads `history/` should
do the same.

### Where a completed season's claims should end up

Phase 4 put the current season's claims in `data/manual/claims.json`, keyed by season, and this
was deliberately left as the Phase 5 boundary. `validate.py` reads claims from **both**
`history/` and `manual/` and does not care which file one came from, so moving a completed
season across is a file move plus a validate run, not a migration.

What is not decided: **when** a season graduates, and whether the admin tool should do it or a
separate importer should. See "Still open".

### The reverse ownership guard exists now

`rs57/admin/store.py` raises `OwnershipError` on any write aimed outside `data/manual/`, and
`gitops.commit_and_push` re-checks the git index after staging and refuses if anything else is in
it. Both are mutation-tested. **A `data/history/` writer needs the same treatment** — it is the
one directory in the repo where a second write is not merely untidy but destroys a frozen record.

## Rules the backfill cannot break

- **A frozen season is frozen.** The importer must refuse to overwrite an existing
  `data/history/{year}.json` rather than warn. Write it once, verify it, never again.
- **`fixtures/keeper_cases.json` and `fixtures/prize_cases.json` are hand-built ground truth**
  reviewed by the commissioner. If a fixture disagrees with what you import, that is a bug or an
  undocumented rule. Chase it down; do not adjust the fixture.
- **The `Keepers` workbook's `K*_sal` cells are not records.** They are live VLOOKUPs that
  recompute against today's roster, so the 2024 tab's `K*_kept` flags are the *2025* keep flags.
  This is in the Phase 0 corrections and it is the single easiest way to backfill wrong numbers.
- **ESPN returns the CURRENT roster, not the end-of-season one.** Membership will not match a
  historical snapshot and should not be compared; compare `Base` and `Kept` for players present
  in both. The reconciliation screen already shows what this looks like — a 2025 keeper pick for a
  player since dropped resolves to no name, because he is not in that season's player list.
- **Never let a REVIEW pass silently.** Prospect rule 2 is the whole reason box-score history is
  in this phase; until it is checkable, every prospect claim raises
  `PROSPECT_START_HISTORY_UNVERIFIED` and the admin tool shows it. When you make it checkable,
  make the engine check it — do not just stop reporting it.
- **Prospect rule 1 has no data source at all.** `validate_team_claims` takes a `seasons_played`
  mapping and nothing in `data/` populates it, so the check silently passes. The admin tool adds
  its own note saying rule 1 is unchecked. If the backfill can supply NFL seasons played, that
  note should become a real check; if it cannot, the note stays.

## Still open — decide these before building

- **Which source wins when the sheet and ESPN disagree about a completed season?** ESPN is the
  live record and the sheet is the only record of what anyone actually paid. They will not agree
  everywhere. This is the first phase where both cover the same season and neither is obviously
  authoritative.
- **When does a season graduate from `manual/` to `history/`, and who moves it?** After the
  draft? After the season ends? A button in the admin tool, or a separate importer? The admin
  tool must never write `history/`, so if it is a button, the button has to call something that
  owns that directory.
- **How far back does the backfill go?** ESPN answers unauthenticated back to 2019 and
  `status.previousSeasons` lists 2015 onwards. The sheet covers fewer. Decide whether partial
  seasons are worth importing or whether the record starts where both sources do.
- **Does `sync.py` start recording `keeperDeadlineDate`?** ESPN has it —
  `draftSettings.keeperDeadlineDate`, 2026-08-30 21:00 UTC — and Phase 4 deliberately did not add
  it to the derived file, because that would change what the nightly Action writes. The admin tool
  reads it from ESPN directly and stores it in `seasons.json`. If the derived files should carry it
  instead, that is a Phase 5 call.
- **Should the reconciliation screen become a nightly check?** It reads ESPN's keeper picks and
  diffs them against recorded claims, which catches a mistyped ESPN entry the same week instead of
  a year later. It is a manual screen today.

## Leftovers worth knowing

- **The `Derived` reader caches on file mtime**, because the admin tool runs for hours and a
  re-sync mid-session is normal — after the auction, `base_salary` moves from `keeperValue` to
  `keeperValueFuture` for the whole league. A year-only cache key served stale rosters silently;
  that was a real bug found by driving the tool.
- **Epoch milliseconds convert through UTC, naive.** `espn._epoch_ms` and
  `admin.reconcile._epoch_ms` both do it and both say why: the models and derived files are naive
  throughout, and `keeper_rules` compares `acquired_at` against `trade_deadline` directly. A local-
  time conversion put two records of one deadline five hours apart before it was caught.
- **htmx is vendored** at `rs57/admin/static/htmx.min.js` (2.0.4), not loaded from a CDN — the tool
  runs on localhost and should render without the network.
- **Flask is an optional extra**, `pip install -e ".[admin]"`, and is in `dev` too so CI has it.
  It is deliberately not a base dependency: the nightly Action installs this package to run the
  syncs and the site generator, and neither should pull in a web framework.
- **Scheduled Actions get auto-disabled after roughly 60 days without a commit**, which is exactly
  what this repo looks like in June and July. `data/derived/` was empty on a fresh clone when Phase
  4 started — the nightly had never committed. Expect to re-enable the workflow each August, and do
  not read an empty `derived/` as a bug in the pipeline.
- **`CLAUDE.md` still describes `data/private/` as the home for manager emails**, and `models.py`'s
  `Manager` docstring points at it. Phase 2 decided the directory was not needed and never created
  it; Phases 3 and 4 forbid creating it. The `.gitignore` entry is harmless belt-and-braces; the
  prose is stale. Raised in Phase 4's notes too, deliberately not edited — `CLAUDE.md` is the
  commissioner's file.
- **The `RS57` sheet is readable through the Drive tooling by id**
  (`1ez6Hf1-vUIkj4rnuZR09a1Z6qIefzy-A5UkVchNrxh8`), no export needed. Four phases have now
  confirmed this and two earlier ones wrongly doubted it — and Phase 1 shipped a $5 bug on the
  assumption that it could not be read. Its standings tabs carry **full real names**; read them if
  you must, never copy them into the repo.
- **`tests/test_admin.py` mutation-checks its safety rules**, the way `tests/test_site.py` does.
  Eight mutations — removing the ownership guard, adding a `safe` filter, computing money in a
  template, unlabelling a REVIEW note, letting an ERROR through the save, taking the prospect
  deadline from the wrong season, widening the commit path, and recomputing a frozen salary — were
  each verified to make a specific test fail. One of the eight did not fail on the first attempt,
  which is how the test that now covers note labelling came to exist. A test that cannot fail is
  worse than no test.
