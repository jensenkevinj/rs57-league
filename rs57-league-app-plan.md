# RS57 League App — Build Plan for Claude Code

*Rebuilding Since 1957 — 12-team ESPN keeper/auction fantasy football league.*

---

## 1. Goal

Replace the Drive-folder-of-spreadsheets workflow with:

- **A public static site** — rosters with keeper salaries, league rules, season history,
  weekly and season-long prize tracking.
- **A nightly job** that pulls the current season from ESPN and rebuilds the site.
- **A local admin tool** for the handful of things ESPN can't tell us.

Live scoring, matchups, and transactions stay on ESPN. This app owns the keeper/salary/prize
domain that ESPN doesn't model.

---

## 2. Architecture

```
   COMMISSIONER'S LAPTOP                GITHUB                      LEAGUE
   ─────────────────────                ──────                      ──────
   python -m rs57.admin                 data/manual/*.json          static site
     ├─ edit overrides, keeper     ──▶  data/derived/*.json    ──▶  (public,
     │  claims, payouts                 data/history/*.json          instant,
     ├─ validate                        site/                        free)
     └─ push manual/ only
                                        nightly Action:
                                          ESPN → derived/ → site/
```

**Everything is Python.** Same language as the existing ESPN script. No JS build tooling.

**Storage is JSON files in the repo.** No database server, no binary artifacts, no hosting
account. Every change is a readable Git diff.

**The site is static.** Pre-rendered HTML plus a `data.json` for client-side sorting and
filtering, served by GitHub Pages — free, permanent, no cold starts.

### Why JSON and not a database

Roughly 190 roster rows per season, about 2,000 across league history; weekly scores add
another ~2,200. Even full box-score history is on the order of 20,000 rows. All of it loads
into memory in well under a second. SQLite and Postgres earn their keep when data outgrows
RAM or when concurrent writers need coordinating — neither will ever apply here.

JSON also avoids a real problem: a SQLite file is a binary blob Git cannot merge, so a
nightly job and a laptop both committing to it would produce unresolvable conflicts.

### Why not a hosted web app

Considered and rejected: Next.js/Postgres on Vercel + Neon, or a Python server on Render or
Fly. As of mid-2026 the "free always-on server with a persistent database" tier has largely
disappeared — Render's free web services sleep after 15 minutes and its free Postgres is
deleted after 30 days; Fly's old free allowance ended for new signups. With one writer and
twelve readers there is nothing for a server to do that a laptop and a static host can't.

**Consequence:** admin work requires the commissioner's computer. No phone edits. If that ever
becomes a real problem the escape hatch is Cloudflare Workers + D1, but that's a TypeScript
rewrite of the admin layer — don't build for it speculatively.

---

## 3. Who writes what

This is the rule that keeps the nightly job and the laptop from ever colliding. **No file has
two writers.**

| Path | Written by | Contents |
|---|---|---|
| `data/manual/` | Laptop only | Salary overrides, keeper claims, payouts, season settings |
| `data/derived/<year>.json` | Nightly Action only | Current-season rosters, scores, transactions |
| `data/history/<year>.json` | Written once, then frozen | Completed seasons |
| `data/private/` | Laptop only, **gitignored** | Manager emails and anything identifying |
| `site/` | Nightly Action only | Generated HTML |

The nightly Action pulls ESPN, writes `derived/`, reads `manual/` + `history/`, regenerates
`site/`, and commits. The admin tool edits `manual/` and pushes. They never touch the same
file, so there is nothing to conflict.

The local tool may regenerate `derived/` and `site/` for preview, but **must not commit
them** — put that behind a `--preview` flag writing to a gitignored scratch directory.

**Freeze old seasons.** Pull each completed season from ESPN once, write it to `history/`,
and never re-fetch. ESPN's historical endpoints get less reliable the further back you go,
and a flaky API should never be able to rewrite settled results.

**Serialize deterministically** — `sort_keys=True`, stable ordering, trailing newline.
Otherwise every nightly commit looks like the whole file changed and the Git history becomes
useless, which throws away the main reason to use JSON.

---

## 4. Stack

| Layer | Choice | Note |
|---|---|---|
| Language | Python 3.12+ | Matches existing script |
| ESPN client | `espn-api` | Verify current version and endpoints |
| Storage | JSON files | Loaded into Pydantic models at read time |
| Admin UI | Flask + Jinja + HTMX | Localhost, server-rendered, no npm |
| Site generation | Jinja → static HTML + `data.json` | Vanilla JS for sort/filter |
| Tests | pytest | Keeper rules engine especially |
| CI | GitHub Actions | Validation on every push |

```
rs57/
  data/
    manual/      overrides.json, keeper_claims.json, payouts.json, seasons.json
    derived/     2026.json
    history/     2015.json … 2025.json
    private/     managers.json          # GITIGNORED
  rs57/
    models.py            # Pydantic models — the schema
    keeper_rules.py      # pure logic, no I/O, heavily tested
    espn_sync.py         # ESPN → derived/
    stats.py             # derived weekly/season stats
    validate.py          # integrity checks, runs in CI
    generate.py          # data → site/
    admin/               # Flask app, localhost only
  site/
  tests/
  fixtures/
  CLAUDE.md
```

**Load JSON into typed models immediately; never pass raw dicts around.** Write
`roster.players[3].salary`, not `data["players"][3]["salary"]`. Pydantic validates at load
time and fails with a useful message. This single discipline is what keeps "JSON as a store"
from degenerating into a pile of `.get()` calls and silent `None`s.

**What you give up versus a database:** enforced referential integrity. Nothing stops a keeper
claim from naming a player ID that doesn't exist. `validate.py` covers this — cross-file
reference checks, no orphans, no duplicate keeper slots, fee sums correct — and runs in CI on
every push. Fifty lines, catching the same bugs a foreign key would.

---

## 5. Security

The repo is public and the site is public. That's fine — nobody cares about our rosters — but
three things need care.

**Manager emails must never reach the repo or the site.** The Drive sheets contain all twelve
managers' addresses. A public URL with a dozen real emails on it is a scraper's breakfast.
Keep anything identifying in `data/private/managers.json`, gitignored, read only by the local
admin tool. The site generator must never import it. Publish display names and franchise names
only, keyed on `espn_team_id`.

**ESPN cookies live in GitHub Actions secrets, never in the repo.** `espn_s2` and `SWID` grant
access to the league. Locally they go in a gitignored `.env`. Critically: **a public repo's
Action logs are public too**, so the sync must never print them — no debug logging of headers,
cookie jars, or full request objects. GitHub masks registered secrets, but masking is not a
substitute for not printing them.

**Escape manual text on render.** Override reasons, bet descriptions, and team names all get
typed by hand and rendered into HTML. Leave Jinja autoescaping on and don't reach for `|safe`.
Low risk since the commissioner is the only input source, but it's free to get right, and one
franchise name in your sheets already contains characters worth escaping.

Two smaller notes: add `data/private/` and `.env` to `.gitignore` in the very first commit,
before there's anything to leak. And if a cookie ever does get committed, rotate it — Git
history is forever and the repo is public.

---

## 6. What's automated vs. what's typed

**Computed from ESPN — never entered by hand again:**
- Weekly high score
- Season points leader
- Positional highs (QB/RB/WR/TE studs — best single-week score at each position)
- Survivor pool (lowest score each week is eliminated; last standing wins)
- Final standings, playoff and consolation bracket results
- Rosters, acquisition values, transactions

**Genuinely manual — the entire admin surface:**
- Salary overrides, with a reason
- Keeper fee allocations submitted by managers
- Payout amounts and paid/unpaid status
- Prospect eligibility review (see §7)
- The betting board, if it's worth carrying over at all

Build the admin app around exactly these, nothing more.

---

## 7. Keeper rules engine

A **pure module with no I/O and no web imports.** The part that must be right, and the part
easiest to verify against history.

**Keeper salary**

```
salary = acquisition_value + allocated_fee + $5 keeper tax (if kept the previous season)
```

- Acquisition value = auction draft price or FAAB/waiver bid
- The $5 tax is waived if the player was **dropped**; **not** waived if **traded**

That second point matters more than it looks: keeper-tax state is a property of the player's
history, not of the current roster. It survives a trade to a new manager. Model it explicitly
or it will silently drift.

**Keeper fees** — total by number kept: 1 → $0, 2 → $5, 3 → $15. The manager distributes the
total across their keepers however they like ($13/$2/$0, $5/$5/$5, etc.). The consolation
bracket winner has fees waived for one year — keepers still cost salary, just no fee on top.

**Prospects** — max 1, in addition to keepers:
1. No more than 1 NFL season played
2. Never started by *any* team in the league, in any season
3. Rostered before the trade deadline
4. Kept at acquisition value
5. Cannot be allocated keeper fees

> **Rule 2 needs historical starting-lineup data** for all 12 teams across every season. ESPN
> can return per-week box scores, but pulling and storing that is its own milestone. Until it
> exists, mark prospect eligibility as commissioner-reviewed in the UI rather than
> auto-validated — and say so on screen, so nobody assumes it was checked.

**Validation** — replaces the `VALID` column:
- ≤ 3 keepers, ≤ 1 prospect
- Fee allocations sum exactly to the tier total; no negative or fractional fees
- Every claimed player was on that manager's roster at season end
- Prospect rules 1, 3, 4, 5 automatic; rule 2 flagged for review

**There is no salary cap.** Keeper totals are unbounded — don't add a cap check, and don't
leave a disabled one lying around for someone to switch on later.

---

## 8. Data shapes

Pydantic models, one per concept:

```
Manager        id, display_name, espn_team_id, active          # NO email
FranchiseName  manager_id, season, name                        # names change yearly
Season         year, season_start, trade_deadline, keeper_deadline,
               consolation_winner_id
Player         espn_player_id, name, position, nfl_team
RosterEntry    season, manager_id, espn_player_id, acquired_at, base_salary,
               kept_prior_year, source(draft|waiver|faab|trade)
SalaryOverride espn_player_id, season, actual_salary, reason, created_at, reverted
KeeperClaim    season, manager_id, espn_player_id, slot(K1|K2|K3|PROSPECT),
               fee_allocated, computed_salary, submitted_at
WeeklyScore    season, week, manager_id, points
Payout         season, label, amount, winner_manager_id, paid
```

**Key franchises off `espn_team_id`, never the display name.** Names change every season
(`Titan's Pans` → `Cooking Rice`), and one already carries a double space
(`Belichick's  Spy`) that has leaked into the sheets.

**All money is integer dollars.** No floats, no cents.

---

## 9. Source data being replaced

The `Keepers` workbook (`1ypljsxlVVRE1PzZqmfYufCFXu_l9J8hCw-rnl571r8Y`):

- **`Keepers` tab** — `Franchise | AcqDate | Name | Pos | Team | Base | Kept | Salary`
- **`Manually Changed Salaries` tab** — commissioner overrides where ESPN reports the wrong
  acquisition value. A first-class feature, not a patch.
- **`20XX Fee Allocations` tabs** — manager keeper declarations, one per offseason
- **`Variables` tab** — `TradeDeadline`, `SeasonStart`
- **Unnamed tab** — prior-year keeper names as Python literals for pasting into the script.
  Replaced by a lookup.

Also: `League Rules` (`1kZlkwErydkpf9OMFArUlFgpin-G-8XUHpXrZHg2N6x4`) and the `RS57` sheet
(`1ez6Hf1-vUIkj4rnuZR09a1Z6qIefzy-A5UkVchNrxh8`) with per-season payouts and standings.

### Gotchas

**ESPN auth.** Cookies expire. Fail loudly — a sync that "succeeds" with zero players would
quietly blank a season.

**Confirm current ESPN endpoints.** Check the current `espn-api` release before assuming
anything about request shapes. (Note: the `keepers.py` sitting in the Drive `leaguemaintainer`
folder is a 2017 backup, not the live script — ignore it. The current code is local.)

**The `League Rules` doc is out of date on payouts.** It describes a $600 pot ($50 × 12) split
three ways. **The `RS57` sheet is authoritative:** $1,200 across champion, 2nd, 3rd, most
points, survivor, positional studs, and weekly high scores. Transcribe the payout structure
from the sheet, not the doc, and treat the doc's payout section as superseded. Everything else
in the doc (keeper and prospect rules) still stands.

**The constitution doc is an unfilled template** — still contains `[specific scoring system]`
and `[League Name]`. Fill it in or leave it out of scope.

**The $5 trade-vs-drop tax rule may be obsolete.** The `League Rules` doc says the tax is
waived on a drop but not a trade. The commissioner has flagged that this may no longer be in
force. **Confirm before implementing.** If Phase 0's fixtures disagree with the engine on a
previously-kept traded player, that's the likely cause — raise it rather than tuning the
engine to match.

**Historical name matching.** Old sheets won't match ESPN's current strings (`Kyle Pitts` vs.
`Kyle Pitts Sr.`, `James Cook` vs. `James Cook III`). Match on `espn_player_id`; backfilling
old sheets needs a manual mapping pass.

**Scheduled Actions get auto-disabled after repo inactivity** — roughly 60 days of no commits,
which is exactly what this repo looks like in June and July. Expect to re-enable each August.

---

## 10. Phases

**Phase 0 — Models + keeper rules engine**
New repo, built from scratch — not a fork or continuation of the existing `rs57` repo. A clean
history means nothing to audit before making it public.
Repo scaffold, `.gitignore` covering `data/private/` and `.env` **in the first commit**,
Pydantic models, `keeper_rules.py` with full pytest coverage against fixtures extracted from
the 2024 and 2025 `Fee Allocations` tabs.
*Done when:* the engine reproduces every historical `K*_sal` value. A mismatch is either a bug
or an undocumented rule — chase it down rather than adjusting the fixture.

**Phase 1 — ESPN sync**
Written fresh against the current `espn-api`, not ported. Writes `data/derived/<year>.json`
with deterministic serialization. Cookie handling via env, loud failures, no secret logging.
*Done when:* generated JSON matches the current `Keepers` tab row for row, overrides aside.

> **Read the old script first — as a specification, not as code.** The existing sync script
> (local, not in this repo) encodes nine years of commissioner decisions that never made it
> into the rules doc. Before writing anything, read it and write down every conditional
> touching keeper eligibility, the $5 tax, trades, drops, or prospects. Those are the answers
> to §7's open edge cases. Then set it aside and build clean.

**Phase 2 — Derived stats + validation**
Weekly high scores, season points, positional studs, survivor elimination, standings.
`validate.py` wired into CI.
*Done when:* recomputing 2025 reproduces the `RS57` sheet's prize winners exactly. This is
where the time savings start — worth reaching quickly.

**Phase 3 — Static site + nightly deploy**
Jinja → `site/`. Home/standings, rosters with keeper salaries, rules (Markdown in the repo),
season history. Nightly Action, GitHub Pages.
*Done when:* keeper questions get answered with a link instead of a screenshot.

**Phase 4 — Admin tool**
Flask + HTMX on localhost. Overrides with reasons, keeper claim entry with live salary math
and validation, payout tracking, season settings. One button to validate, commit, and push
`manual/`.
*Done when:* an offseason runs without opening a spreadsheet.

**Phase 5 — History and backfill**
Import prior seasons from the archived sheets into `history/`, freeze. Box-score history for
prospect rule 2.

---

## 11. `CLAUDE.md`

```markdown
# RS57 League App

12-team ESPN keeper/auction fantasy football league. Manages keeper salaries, prize
tracking, and league history. NOT live scoring — that stays on ESPN.

## Architecture
- Python throughout. JSON files in data/ are the store — no database.
- Nightly GitHub Action writes data/derived/ and site/. Local admin tool writes
  data/manual/. NO FILE HAS TWO WRITERS. Never make the Action touch manual/,
  and never commit derived/ or site/ from the laptop.
- data/history/ is written once per completed season, then frozen.
- Serialize with sort_keys=True and stable ordering — diffs must stay readable.

## Security
- The repo and site are PUBLIC.
- Manager emails live ONLY in data/private/ (gitignored). Never import that from
  the site generator. Publish display names only.
- ESPN espn_s2/SWID come from env or Actions secrets. NEVER log them — Action
  logs on a public repo are public.
- Jinja autoescaping stays on. No |safe on manual text fields.

## Rules
- Max 3 keepers + 1 prospect per team
- salary = acquisition value + allocated fee + $5 if kept last year
- $5 tax waived on drop, NOT on trade — it follows the player across trades
- Fee tiers: 1 keeper → $0, 2 → $5, 3 → $15, distributed freely by the manager
- Consolation bracket winner: fees waived one year
- Prospects: ≤1 NFL season, never started by any league team, rostered before the
  trade deadline, kept at acquisition value, no fee allocation

## Conventions
- Key franchises on espn_team_id. Display names change yearly and are unreliable.
- All money is integer dollars. No floats.
- Load JSON into Pydantic models immediately. Never pass raw dicts around.
- keeper_rules.py stays pure — no I/O, no Flask. It is the tested core.
```

---

## 12. Phase 0 opening prompt

This is Phase 0's kickoff, written before any code existed — not an index of every phase.
Later phases carry their own opening prompt at the top of their handoff note, next to the
context that prompt depends on (`docs/phase-2-notes.md`, and so on). Phase 1's is not recorded.

> I'm building a fantasy football league app. Read `PLAN.md` in full, then execute **Phase 0
> only**: repo scaffold, `.gitignore`, Pydantic models, and `keeper_rules.py` with full pytest
> coverage against the fixtures in `fixtures/`. Stop there — don't start the ESPN sync.
>
> Before writing any code, tell me anything in the keeper rules that's ambiguous or that you'd
> have to guess at.

That last line matters. The keeper rules have real edge cases the doc doesn't address: a
previously-kept player traded mid-season, a manager keeping fewer than three, a prospect
promoted to a regular keeper slot, and what happens to the $5 tax when a player is dropped and
re-added by the same team. Better to surface those before code rests on a guess.

---

## 13. Corrections

Things in this document that turned out to be wrong, recorded here so nobody re-derives them.
Where this document and `CLAUDE.md` disagree, `CLAUDE.md` wins.

### Found during Phase 0

**§9 is wrong about `Manually Changed Salaries`.** It is not "commissioner overrides where ESPN
reports the wrong acquisition value." Managers trade draft cash; ESPN has no native support, so
the commissioner hand-edits a few player salaries on the two teams involved and changes them
back before the next draft. `actual_salary` is the true value and ESPN holds the distorted one
while `reverted` is False. Un-reverted overrides should net to zero league-wide, since a cash
trade moves money between two teams — one historical row (Saquon Barkley, +$3) has an
unrecoverable counterparty and is flagged `unpaired_ok`.

**Base salary is not the original acquisition value.** It is what the player cost his manager
*this* season. Keepers enter ESPN's auction at their keeper price, so ESPN's per-season value
already carries every prior fee and tax forward, and next season's base is this season's
computed salary. Puka Nacua: $0 on waivers → $0 in 2024 (no tax on a first keep) → $5 in 2025 →
$10 in 2026. Reading it as a frozen acquisition value makes every multi-year keeper look
under-priced when nothing is wrong.

**§10's Phase 0 acceptance criterion is not reachable as written.** "The engine reproduces every
historical `K*_sal` value" assumes those cells are records. They are live VLOOKUPs into the
current roster, so they recompute today's base against old declared fees — the 2024 tab's
`K*_kept` flags are the *2025* keep flags. Phase 0 is instead verified against a hand-built
fixture table reviewed by the commissioner, plus an end-to-end diff of all 12 teams' current
numbers.

### Found during Phase 1

Full detail in `docs/espn-field-semantics.md` and `docs/phase-1-notes.md`.

**§5 and §9 are wrong that ESPN auth is a risk. No credentials are needed anywhere.** The
league is public and every endpoint the pipeline uses answers unauthenticated — settings,
rosters, the draft record, the FAAB transaction log, and historical seasons back to 2019. There
is no cookie to expire, so the headline risk in §5 and §9 does not apply to any read path.
`EspnClient.from_env` still reads `ESPN_S2`/`SWID` if ESPN ever tightens up.

Two failures *look* like auth and are not. `view=mTransactions2` returns `200 OK` with the
`transactions` array silently **missing** unless you pass a `scoringPeriodId`. And the
`leagueHistory/{id}?seasonId=…` route 404s for every season, while
`seasons/{year}/segments/0/leagues/{id}` serves the same league fine — use the per-season path
for the history backfill.

**§9's "check the current `espn-api` release" and §10's "written fresh against the current
`espn-api`" were not followed, deliberately.** The package (0.46.0, actively maintained) models
none of the fields this pipeline runs on: zero references to `keeperValue`,
`keeperValueFuture`, or `acquisitionDate`, and its `Player` keeps no raw payload to reach past
the wrapper. Its strengths are box scores, standings and power rankings — the *scoring* side,
which §1 puts out of scope. Phase 1 uses stdlib `urllib` against four endpoints and adds no
dependency. **Worth revisiting at Phase 2**, where the scoring side is the whole job.

**§9's payout list is incomplete, and the amounts are not constants.** It stops at "champion,
2nd, 3rd, most points, survivor, positional studs, and weekly high scores" and omits
**`Unlucky`, $20 every season** — the highest score that still lost its matchup, awarded once
per season, not weekly. Amounts also move between seasons (2023 paid Survivor $50 and weekly
high scores $9.29, a one-off from the 18-week change; the league is back on whole dollars).
Positional studs are a **single best week**, not a season total. Build from
`docs/phase-2-notes.md`, not from §9.

**§10's Phase 1 acceptance criterion *is* reachable, and skipping it would have shipped a
bug.** The `Keepers` workbook reads directly through the Drive tooling by id — an earlier
session assumed otherwise and waived the check on that assumption. Running it found a $5 error:
a prospect keep was being taxed. `draftSettings.keeperCount` is 4 — three keepers plus a
prospect — and ESPN marks all four picks `keeper: True` with nothing recording which slot each
filled, so **ESPN alone cannot tell a keeper from a prospect.** Every ESPN-side cross-check
agreed and missed it. A check against one source cannot find what that source does not know.

One caveat when diffing: the tab is a snapshot from whenever the old script last ran, while
ESPN returns the *current* roster, so **membership will not match and should not be compared**.
Compare `Base` and `Kept` for players present in both.

Confirmed and unchanged: the $5 tax survives a trade and is cleared only by a drop; a drop and
re-add is a full reset; a prospect keep never sets the tax flag; repeat prospect claims are
invalid; there is no salary cap.

### Found during Phase 2

Full detail in `rs57/stats.py`'s module docstring and `fixtures/prize_cases.json`. The prize
rules now live in `CLAUDE.md`; where this document and `CLAUDE.md` disagree, `CLAUDE.md` wins.

**Every unrecorded prize rule was settled by reproducing the sheet, and two of them are not
what a reasonable person would guess.** The `RS57` sheet's 2025 tab reproduces exactly — all 14
weekly high scores, all four studs (player, week *and* score), Survivor's eleven eliminations
in order, Unlucky, Most Points and the three placings — and so does 2024, independently. The
non-obvious ones:

- **Positional studs run the whole season, including the playoff weeks.** 2025's WR stud is
  week 16 and its TE stud is week 15. Only the *weekly high scores* stop at 14.
- **Most Points is the regular season, weeks 1-14.** 2025 cannot distinguish the two windows,
  since the same franchise wins under either — but **2023 can**: the two windows name
  *different* franchises there, and the sheet's winner is the one weeks 1-14 picks. The tab
  header reading "Thru: Week 17" is simply wrong. Had this been closed on 2025 alone it would
  have been a coin flip recorded as a fact.
- **Survivor eliminates the lowest score among the still-alive**, not the lowest in the league,
  and its window is `teams - 1` weeks rather than a constant 11.

**§9's `Unlucky` omission is confirmed and cost-bearing**, as Phase 1 recorded: $20 a season,
awarded once, to the highest score that *lost*. A tie is not a loss.

**Commissioner decisions, 2026-07-28.** `Unlucky` covers the regular season only — no recorded
season can distinguish the windows, so this is a choice, not a reproduction. Ties **split the
prize evenly**; no tie has ever occurred in three seasons of the sheet. Money stays integer
dollars, so an indivisible split pays the floor and reports the remainder as REVIEW rather than
rounding it away. `Season.consolation_winner_id` is derived and reported for confirmation but
**never auto-populated** — a 12-team league runs two consolation ladders and ESPN does not say
which one the league means, and reading it wrong waives the wrong team's fees for a year.

**The 2023 payout question is decided: `Payout.amount` stays `NonNegMoney`.** 2023's $9.29
weekly prize cannot be represented, and 2023 is therefore absent from
`data/manual/payouts.json` — its stats still compute, it just gets no payout rows. Widening the
model to carry one retired edge case would put floats into every salary in the league.

**`data/private/` was not needed and was not created.** ESPN's public league endpoint exposes
`members[].firstName` joined through `teams[].owners`, which is enough to check the sheet's
first-name winners against `espn_team_id` in a session without storing anything. The mapping is
deliberately not in the repo, and `tests/data/espn_scoring_2025.json` is recorded with
`members` stripped — there is a test asserting no real name is in it.

**Phase 1's "worth revisiting at Phase 2" on `espn-api` was revisited: still no.** Box scores
are the package's strength, but `fetch_boxscore` is nine lines and its `BoxPlayer` wrapper
hides `statSourceId` — the difference between what a player scored and what he was *projected*
to score. Awarding a stud prize off a projection is exactly the kind of silently-wrong answer
this pipeline exists to prevent.

### Found during Phase 3

Full detail in `rs57/site.py`'s module docstring and `.github/workflows/nightly.yml`'s header
comment.

**The phase breakdown's "nightly Action" did not exist, and neither did anything it was
supposed to have written.** `data/derived/` was empty: no CI job had ever written it, so the
2025 stats that derive perfectly from ESPN existed nowhere as a file the site could read. The
distinction that matters is that this is *not* the Phase 5 history backfill — `data/history/`
is still empty and still Phase 5's. A completed season's `derived/` files are simply an ESPN
read that nothing had ever run.

**Commissioner decisions, 2026-07-29.** The site publishes prize winners, prize amounts, and
per-franchise season earnings. The rules page is a new `docs/rules.md`, written for league
members rather than for an agent. The nightly schedule syncs **the current season only**;
completed seasons are static and are populated once by a `workflow_dispatch` run with an
explicit season list, then left alone.

**An unplayed season is blocked, not broken, and the Action must not treat it as a failure.**
`stats_sync --year 2026` in the offseason exits non-zero with 19 ERROR issues — fourteen
`missing_week`, four `no_stud_for_position`, one `no_unlucky` — because a season with no games
correctly refuses to award anything. `validate.py` re-surfaces those as errors, so a nightly
job that simply ran both syncs for the current season would fail its own gate every night from
February to September. The workflow therefore discards a blocked stats file and keeps the
committed one, and goes red only when that season *had* derived on a previous run — checked by
asking git whether the file is tracked, before the sync runs. Failing every night until the
season starts is how a gate gets ignored, which is the same reasoning that kept `--strict` off
`validate` in CI.

**A season page needs both derived files.** Franchise names live in `{year}.json`'s
`franchises[]`, keyed per season, while winners in `{year}-stats.json` are only `manager_id`.
A stats file without its keeper file can therefore only render `t3`, and the site says so
rather than borrowing an adjacent season's names — names change yearly, so a borrowed name is
a wrong name on a real result.

**The fee is deliberately absent from every published keeper price.** The tier depends on how
many keepers a manager declares and the split across them is the manager's own choice, and no
`KeeperClaim` exists to read until Phase 4. The site publishes `base + $5 tax` per player and
the fee tiers as a table, and says in as many words that nobody has declared anything. Reading
the page and adding the tier by hand reproduces `compute_team_keepers` to the dollar; that was
checked end to end against t3's roster, not assumed.

**Rendering Markdown without a Markdown dependency was the safer option, but only just.** The
usual libraries pass raw HTML straight through, which would have meant handing a repo file the
ability to inject markup and reaching for the `safe` filter to do it. `render_markdown` escapes
every line *before* it adds a single tag instead. Getting the subset wrong is still a real bug:
the first version formatted line by line, so a bullet that wrapped onto a second line broke the
entire list and a bold run spanning a line break left both pairs of asterisks on the page. The
rules page's whole prize list rendered as one run-on paragraph. **Nothing caught this but
opening the page and reading it** — the tests were green, and a test asserting the rendered
rules file contains no stray asterisk was written only after the eye found it.

**The phase breakdown says "GitHub Pages" and Pages had never been enabled** — `has_pages` was
false and `GET /repos/.../pages` returned 404. It is published from an uploaded artifact rather
than from a branch, because branch-based Pages can serve only the repository root or `/docs`
and neither is available here. Serving the root would scatter generated HTML among human-owned
files, make a clean rebuild impossible (you cannot `rm -rf` the repo root, so a page that stops
being generated is served forever), and reduce the workflow's ownership guard from a
self-maintaining directory prefix to a hand-listed set of filenames. Serving `/docs` is worse:
the generator *reads* `docs/rules.md`, so the Action would be writing into the directory it
takes its input from. The artifact leaves `site/` committed and diffable and costs fourteen
lines.
