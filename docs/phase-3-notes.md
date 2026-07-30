# Phase 3 handoff — static site + nightly deploy

> **CLOSED 2026-07-29.** Every question in "Still open" below is answered; the answers are in
> "Found during Phase 3" in the Corrections log of `rs57-league-app-plan.md`. The acceptance
> criterion **was run**: the pages were generated from real ESPN data and read, a manager's
> reading of the keeper page was checked against `compute_team_keepers` and matched to the
> dollar, and reading them found a real rendering bug (the rules page's entire prize list
> collapsing into one paragraph). The section below is kept as the record of what was open.

Written at the end of Phase 2. Everything here is context a fresh session cannot recover from
the repo alone. Read `CLAUDE.md` first; it holds the rules. `docs/phase-2-notes.md` records
what the stats engine settled and why. This holds what Phase 3 needs and what is still open.

Phase 3 is: Jinja → `site/`, home/standings, rosters with keeper salaries, a rules page, season
history, and the **nightly Action** — which does not exist yet. *Done when* keeper questions get
answered with a link instead of a screenshot.

## Opening prompt

Paste this to start Phase 3 in a fresh session.

> I'm building a fantasy football league app. Read `CLAUDE.md` and `docs/phase-3-notes.md` in
> full, then execute **Phase 3 only**: the static site and the nightly deploy.
>
> Scope: Jinja templates rendering `site/`, covering standings, rosters with keeper salaries,
> prize winners, a rules page, and per-season history; plus the nightly GitHub Action that
> syncs ESPN, regenerates the site, and commits. Stop there — no admin tool, no history
> backfill, no changes to the keeper or stats engines.
>
> Read `rs57/stats_sync.py` and `rs57/sync.py` before writing the Action; Phase 2 already
> settled the derived file shapes, and there are **two files per season** that fail
> independently. Don't collapse them.
>
> Constraints that are not negotiable:
> - `keeper_rules.py` and `stats.py` stay pure. The site generator imports them, never the
>   reverse, and neither one learns about Jinja.
> - **The repo, the site, and the Action logs are all PUBLIC.** Publish franchise names only.
>   No manager first names, no full names, no emails. `data/private/` does not exist and is not
>   to be created.
> - Everything keys on `espn_team_id`; `manager_id` is `t{espn_team_id}`. Render franchise
>   names from `FranchiseName` rows, and never key on one — they change yearly and one carries
>   a double space.
> - NO FILE HAS TWO WRITERS. The Action owns `data/derived/` and `site/`. It must never write
>   `data/manual/` or `data/history/`. Local preview goes to a gitignored scratch dir behind a
>   `--preview` flag, never to `site/`.
> - Jinja autoescaping stays on. No `|safe` on any manual text field.
> - **A REVIEW issue must never render as though it had been checked.** The derived stats file
>   carries them; the page has to show them as unverified.
>
> Done when a manager can answer "what will my keepers cost me next year" from the site without
> a screenshot. **Actually open the generated pages and read them** — the last two phases each
> had an acceptance check that a session was tempted to wave through, and one of them was
> hiding a real $5 bug. Read the Phase 1 and Phase 2 entries in the Corrections log of
> `rs57-league-app-plan.md` before you decide any verification is unnecessary.
>
> Before writing any code, tell me anything about what belongs on a public page that's
> ambiguous or that you'd have to guess at — especially anything involving money.

That last line is deliberately narrower than the equivalent in Phases 0 and 2. Those asked
about *rules*, because the rules were unrecorded. Phase 3's rules are settled; what is not
settled is **what should be published at all**, and that is a judgement the commissioner has to
make rather than a fact to be reproduced. See "Still open" below.

## What Phase 2 leaves you

### Two derived files per season, and they fail independently

`rs57.sync` writes `data/derived/{year}.json`. `rs57.stats_sync` writes
`data/derived/{year}-stats.json`. They are separate on purpose: they read different ESPN views
and break for different reasons, and a broken box score must not be able to blank a season of
salaries. **Keep them separate in the Action too** — one step failing should not stop the other
from writing.

`{year}.json` (keepers):

```
season, source{drafted, base_salary_field, trade_deadline},
franchises[], players[], roster[],
review{waiver_bases_verified, waiver_base_mismatches, warnings[]}
```

`{year}-stats.json` (scoring and prizes):

```
season, source{regular_season_weeks, weeks_with_results[]},
standings[], weekly_high_scores[], season_points[], positional_studs[],
survivor{eliminations[], winner_manager_ids[]}, unlucky, payouts[],
review{consolation_winner_manager_ids[], warnings[], issues[]}
```

Four things about that shape will bite if you assume otherwise:

- **`manager_id` is `t{espn_team_id}`, not a name.** Franchise names live in `{year}.json`'s
  `franchises[]`, keyed per season. Joining the two files is how a page shows a readable team
  name. There is no mapping to a person anywhere in this repo, by design.
- **A prize can have several rows.** Ties split evenly, so `payouts[]` may hold two
  `Week 6 High Score` rows at $5 each. Don't render one row per label.
- **A prize can have no winner.** `winner_manager_id` is nullable and the row still carries its
  money, so the pot always reconciles. Render "unawarded", not an empty cell.
- **A season can have stats and no payouts.** 2023 is deliberately absent from
  `data/manual/payouts.json` — its $9.29 weekly prize is not an integer dollar. Its stats still
  compute.

### The nightly Action does not exist yet — you are building it

`.github/workflows/ci.yml` is tests plus `python -m rs57.validate`, and that is all there is.
`CLAUDE.md` describes a nightly Action in the present tense; it is describing the intended
architecture, not something already running. Nothing has ever written `data/derived/` in CI.

What it has to do, from the plan's ownership table: pull ESPN, write `derived/`, read `manual/` +
`history/`, regenerate `site/`, commit. Notes that are easy to get wrong:

- It must run **both** syncs. `rs57.sync --year <yr>` then `rs57.stats_sync --year <yr>`.
  Neither takes `--dry-run` in the Action; that flag is for the laptop.
- Run `python -m rs57.validate` **after** the syncs and before the commit, so a bad sync is
  caught before it lands.
- **Credentials: there are none, and none are needed.** Every endpoint answers unauthenticated.
  `ESPN_S2`/`SWID` are read from env if present, purely as insurance. **Never echo them** —
  Action logs on a public repo are public.
- ESPN's offseason responses are thin. `build_scoring_season` already refuses to derive a
  season from a degraded response and warns rather than writing rubbish; let it, and don't add
  a retry loop that turns a warning into a silent success.

### The engines are pure and must stay that way

`keeper_rules.py` and `stats.py` import nothing impure — there is a test that walks their ASTs.
The site generator depends on them; nothing about Jinja, paths, or HTTP may travel the other
way.

## Rules the site cannot break

- **Everything published is public forever.** The site is GitHub Pages off a public repo.
  Phase 2 leaked four manager first names into a doc and they are now permanently in the
  git history at `f4c0c84` — a force-push would not have retracted them. Grep before you
  publish, not after. Franchise names are fine; people's names are not.
- **REVIEW is not a pass.** `review.issues[]` in the stats file is the list of things nobody
  has verified. A prize table that shows a winner while hiding an unverified flag next to it is
  the exact failure this project keeps guarding against. Render them.
- **Autoescaping on, no `|safe`.** `SalaryOverride.reason` is free text the commissioner types.
  That is the injection path.
- **Don't recompute anything in a template.** Salaries come from `keeper_rules`, prizes from
  `stats`. A Jinja expression doing arithmetic on money is a second implementation of the rules
  that nobody will test.

## Still open — decide these before building

- **Should the site publish money at all?** Prize amounts, who won what, and totals per
  franchise are all derivable and all currently in `data/derived/`. Whether they belong on a
  public page that anyone can find is a commissioner call, not a technical one. The plan's phase
  breakdown says "keeper salaries" for Phase 3 and never mentions publishing payouts. Ask first.
- **Which file is the rules page?** The phase breakdown says "rules (Markdown in the
  repo)" without naming one.
  `CLAUDE.md` holds the rules today but is written for an agent, not for a league member.
  Probably a new `docs/rules.md`, but that is a decision.
- **Which seasons get pages?** `data/history/` is empty until Phase 5, so "season history" can
  only cover seasons that have been synced into `derived/`. Decide whether the site renders
  what exists or waits for the backfill.
- **Rosters show what, exactly?** There are no `KeeperClaim` rows anywhere yet — the admin tool
  that records them is Phase 4. So the site can show each rostered player's *computed potential*
  keeper salary (base + tax, before any fee allocation), but it cannot show what anyone has
  actually declared. Make sure a page never implies a keep has been claimed.
- **`Season.consolation_winner_id` still has no store** (Phase 4 owns "season settings"). 2025's
  winner is derived and sits in `review.consolation_winner_manager_ids`. Until it is recorded,
  a fee waiver is *derived*, not *decided* — don't render it as settled.

## Leftovers worth knowing

- **Two `validate.py` checks have never actually executed.** `check_base_continuity` and
  `check_override_balance` are wired in and report SKIPPED every run, because `data/history/` is
  empty. They need recorded `KeeperClaim` rows, which arrive in Phase 4 or Phase 5. This is not
  a Phase 3 problem, but don't mistake a green CI run for the ratchet having been audited.
- **`tests/data/espn_scoring_2025.json` is 604 KB**, stored compactly rather than
  pretty-printed. It is a machine recording of 3,206 player-weeks that keeps CI off the
  network, not a file to read by eye.
- **`fixtures/prize_cases.json` is hand-transcribed ground truth** from the `RS57` sheet and the
  only record of what anyone was paid. If it disagrees with the engine, that is a bug or an
  undocumented rule. Chase it down; do not adjust the fixture.
- **The `RS57` sheet is readable through the Drive tooling by id**
  (`1ez6Hf1-vUIkj4rnuZR09a1Z6qIefzy-A5UkVchNrxh8`), no export needed. Both phases that doubted
  this were wrong, and Phase 1 shipped a $5 bug on the assumption. Its standings tabs carry
  **full real names** — read them if you must, never copy them into the repo.
