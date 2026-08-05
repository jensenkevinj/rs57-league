# Admin keeper redesign

Replaces an earlier draft of this file. That draft described a keeper *selection* tool. This one
describes a fee-entry and pricing tool, which is what the console is actually for.

## The workflow this serves

Stated by the commissioner, 2026-08-04. It is the same every year.

| Step | Who | The console's job |
|---|---|---|
| 1. Managers pick their keepers in ESPN | managers | — |
| 2. They send their fee breakdowns separately | managers | — |
| 3. The keeper deadline passes | — | unlock |
| 4. All fees are entered | commissioner | be fast to type into |
| 5. Salaries are computed | **console** | already works |
| 6. Salaries are entered into ESPN | commissioner | — (worked directly off the form) |
| 7. A few salaries are hand-edited for draft-cash trades | commissioner | overrides, recorded here |
| 8. Verification | **console** | read ESPN back, compare |
| 9. Draft day | — | — |

**ESPN is upstream for *who* is kept and downstream for *how much*.** Managers make the
selections there; the price is computed here and typed back in. That split is the whole design.
`reconcile.py` already states the second half of it — "ESPN is downstream of this tool, never
upstream of it" — and the first half is new here.

What the console is **not**: a place to shop for keepers. The commissioner is transcribing twelve
messages that have already decided everything. Optimise for typing, not for browsing.

## Decisions settled 2026-08-04

All from the commissioner, in the conversation that produced this file.

* **The console is locked until the keeper deadline passes.** No salary is entered before it, so
  a lock costs nothing and stops a number being recorded while managers can still change their
  minds. This **reverses** the rule the tool was built on — see "Prose and tests that now assert
  the opposite" below. Nothing ever re-locks after the deadline.
* **Fee entry is plain text boxes.** No split-evenly affordance, no cleverness. A manager's
  message may say "$5/$10/$0" or "split it evenly"; either way the commissioner types dollars.
* **Overrides move onto the keeper page.** Step 7 happens in the same sitting as steps 4-6, so
  all keeper fee work belongs in one place.
* **No printable worksheet for step 6.** The form itself is what gets worked from.
* **Verification reads the roster, not the draft.** Same call `rs57.sync` already makes.

## Target behaviour

`/season/<year>` is the only keeper page, and it is a **board**: a grid of twelve franchise
cards, styled after ESPN's own keeper page. Each card is four slot rows — K1, K2, K3, prospect —
holding a player picker, a fee box and the engine's price, plus a footer of total keeper value
and fees owed. Viewing and editing are the same screen because they are the same job: no Edit
button and no drill-down needed.

**No salary cap row**, whatever ESPN's page shows. That column is ESPN's auction budget; this
league has no keeper cap, and a disabled one lying around is how a rule nobody has gets enforced
by accident.

Verify and overrides live on the same tab, folded away below the board — the same sitting's work,
but not what the page is for. The Reconcile tab is gone.

Everything is still priced by `keeper_rules.compute_team_keepers` through the htmx round-trip:
change a field, POST, the engine prices and validates in one pass, the fragment swaps back.
**No arithmetic on money moves into a template or into JavaScript** — `test_no_javascript_computes_a_salary`
asserts the admin templates carry no inline script at all, and `test_no_admin_template_does_arithmetic_on_money`
greps every `{{ }}`. Both stay.

## The one open question, and why it needs no separate probe

**Does ESPN prune rosters to the kept players when the keeper deadline passes?**

The commissioner's read is yes — whoever is left on the roster after the deadline is by
definition a keeper. It is plausible and unconfirmed. Nothing in the repo can settle it: the
derived files begin 2026-07-28 and none of them spans a keeper deadline, and the 2019-2025
rosters on disk are end-of-season snapshots, all 15-16 deep. The one hard observation available —
2025's completed draft returned 33 picks with `keeper: true` and real `bidAmount`s — is
consistent with either answer.

It resolves itself on 2026-08-31, two ways, both free:

1. **The nightly says which regime it read.** `espn.check_roster_sizes` now judges roster depth
   league-wide instead of per team, and a pruned league records a warning into
   `data/derived/{year}.json` naming the state. If that warning appears the morning after the
   deadline, rosters pruned. Done 2026-08-04 — see "The degraded-response guard" below.
2. **The console says so.** The verify screen reports which regime it is in, from the payload
   itself — see "Verification" below.

**What it decides.** If rosters prune, step 4 loses its hardest part: ESPN has already recorded
*who*, so each franchise shows four rows and the commissioner types four fees and marks one
prospect. If they do not prune, each franchise shows fifteen rows and four of them get a slot set.

**Superseded 2026-08-04.** An earlier version of this plan said to keep the player-keyed form
(a slot dropdown on every rostered player, `slot_{player_id}`) and revisit on 2026-08-31 with the
pruning answer in hand. The commissioner asked for the ESPN keeper page's shape instead — a grid
of team cards — and that settles it independently of pruning: **the form is slot-keyed**,
`player_K1` / `fee_K1`, four rows per franchise.

That is the shape of what a manager actually sends in ("K1 Nacua $5, K2 Cook $10, prospect
Skattebo"). The player-keyed form read the roster instead of the message and put fifteen rows on
screen to record four.

If rosters do prune, the picker simply has four options instead of sixteen. Nothing else changes.

## Implementation

### 1. The board

`/season/<year>` renders twelve franchise cards in a grid. `_claim_form.html` is the card.

* **One Record button for the whole board**, not one per card. Twelve franchises are entered in
  a single sitting, so recording them is one action taken once; a button per card is twelve
  chances to forget one.
* The isolation that mattered was never the twelve buttons — it was the *write*. The league save
  prices, validates and writes **team by team**: franchises that validate are recorded, ones with
  errors are skipped and **named with the reason**. A single illegal fee spread neither records
  itself nor discards the other eleven teams' work.
* **A franchise absent from the request is left as it was; only one submitted empty is cleared.**
  The board posts all twelve every time, so "missing" means the submission was not about that
  team — writing an empty claim list for it would delete a record nobody touched. The same rule
  `save_settings` follows, and the same trap.
* Field names are manager-scoped (`t1__player_K1`) so one POST can carry twelve teams.
  `split_league_form` splits it back into plain per-team forms, so `claims_from_form` never
  learns a league-wide POST is possible — there is still exactly one parser.
* Live pricing still posts **one card** (`hx-include="closest .team-card"`) and swaps only that
  card, so a keystroke re-renders one franchise rather than twelve.
* The card id is keyed on the manager. A shared `#claim-form` was correct while exactly one lived
  on a page; twelve is a silent failure, because htmx swaps *something* and the numbers land on
  the wrong team.
* **Four slot rows always**, even with nothing declared. An empty slot is a fact worth rendering,
  and a card whose height depends on how many keepers a team has makes a grid impossible to scan.
* The board passes `detailed=false` so twelve cards stay scannable; everything else — the team
  page, and the fragment htmx swaps back after an edit — defaults to true. **That default is the
  important half**: a card that dropped its notes would drop REVIEW items with them. The card you
  are working on always shows them; the eleven you are not looking at stay quiet.
* Keep `/season/<year>/team/<manager_id>`. It is the same card with full notes, and deleting it
  buys nothing.

### 2. The deadline lock

`SeasonScreen.keeper_deadline` and `.deadline_passed` already exist and are already rendered.
What is new is gating on them, in three states rather than two:

| `keeper_deadline` in `data/manual/seasons.json` | Console |
|---|---|
| recorded, in the future | **locked** — the normal pre-deadline state |
| recorded, in the past | editable |
| **not recorded** | **editable**, with the existing REVIEW note saying why |

The third row is the one to get right. `store.season(year)` returns `Season | None`, so a year
with no settings row yields `deadline = None` — which today is indistinguishable from "the
deadline is in the future". Collapsing them makes a missing deadline a permanent lockout, on a
freshly synced season, with nothing on screen saying why. The note already exists at
`screens.py:472`; it just has to also mean "so nothing is locked."

`SeasonScreen` needs a third field — `editable`, or `locked_until` — computed in
`build_season_screen`. Do not have the template infer it from `deadline_passed`, which is False
in two different situations.

**The lock must be enforced server-side.** `disabled` on an input is a courtesy to the browser;
a tab left open from before the deadline, or a stale htmx request, still POSTs and still writes.
The `save` route refuses the write and says why. `preview` stays open — it writes nothing, and
seeing what a keeper would cost before the deadline is harmless.

### 3. Overrides on the same page

Step 7 happens in the same sitting, so the add-override form and the season's existing overrides
render on `/season/<year>`.

**Keep `/overrides` as well.** It is cross-season, it is where the revert workflow lives, and
`check_override_balance` is league-wide — un-reverted overrides net to zero across the two teams
in a cash trade, which a single-season view cannot show. The season page gets the entry form and
that season's rows; the tab stays the ledger.

`SalaryOverride.reason` is free text typed here, committed to a public repo, and rendered on a
public site. It is the injection path. Autoescaping stays on and no template gains a `safe`
filter.

### 4. Verification

`POST /season/<year>/verify`, rendering into a region on the same page. Never on GET — an
unreachable ESPN must not take the entry page down.

It fetches each team's roster live (`EspnClient.fetch_roster`, the same call `rs57.sync` makes),
compares to what is recorded, and **writes nothing**. Not `data/derived/`, not `data/manual/`.
It reads, compares in memory, renders.

**Which field to compare is half-pinned.** Pre-draft the base is `keeperValue`; post-draft ESPN
overwrites it with `keeperValueFuture` (`espn.base_salary_field`, settled in
`docs/espn-field-semantics.md`). The verify reads whichever field
`DerivedSeason.base_salary_field` names, so it cannot disagree with the pipeline that produced
the numbers it checks, and that choice lives in one place.

**Observed live 2026-08-04, before step 6 had been done for anybody:** `keeperValue` still holds
the price each keeper carried in, so every recorded claim came back differing by exactly its own
fee and tax — Josh Allen $33 recorded against $28 held (his fee), Trey McBride $32 against $22
and Chase Brown $18 against $8 (fee plus tax), Cam Skattebo agreeing at $5 because a prospect
owes neither. That is the tripwire `CLAUDE.md` describes almost verbatim: "reports the whole
league as off by precisely its own fees and taxes."

So the screen distinguishes **`not_yet_entered`** — ESPN holding the sync-time base, unchanged —
from a real **`mismatch`**. It is not a pass either: ESPN does not hold the number yet, so
`clean` stays False. Without the distinction the console shows the whole league red before
anybody has typed anything into ESPN, and the mismatch it exists to catch gets scrolled past
with the rest.

**Still open:** whether step 6 writes `keeperValue`, `keeperValueFuture`, or both. Settle it by
entering **one** keeper into ESPN and running verify. If that row flips to `agrees`, the field is
right. If it stays `not_yet_entered`, step 6 writes the other field and `base_salary_field` is
the wrong side of the comparison in this window.

Compare against each claim's stored `computed_salary` — the frozen record of what the manager was
told they owed — never a fresh recomputation, which would compare a number against itself.

**Report both directions, and say when one could not run.** A roster read verifies the salary of
every *recorded* claim. Whether it can also catch a keeper that was never recorded depends on the
open question above, and the payload announces which regime it is in:

* **more than `MAX_KEEPERS + MAX_PROSPECTS` players on a team** — rosters did not prune. Every
  player carries a `keeperValue` whether he is a keeper or not, so "kept but never recorded"
  cannot be detected from the roster. Report that direction as **unavailable**, not as passing.
* **at or under that** — rosters pruned. Every rostered player is a keeper, so a rostered player
  with no recorded claim is an unrecorded keeper, and the comparison runs both ways.

Silence reads exactly like success. A check that cannot run is reported as SKIPPED with the
reason — that is the rule the whole validator is built around and it applies here.

Keep `fetch_keeper_picks` and the draft-detail comparison as a secondary check. It is already
written, it costs nothing to keep calling, and post-draft it catches the direction a roster read
cannot. Report it as unavailable before the draft rather than as clean.

### 5. Remove the Reconcile tab

* Delete `rs57/admin/templates/reconcile.html` and the `reconcile_espn` route.
* Remove the nav link from `base.html` and the prose link in `team.html:35`.
* **Keep `rs57/admin/reconcile.py`.** `ReconcileRow`, `Reconciliation` and `reconcile()` are the
  comparison logic and the verify action reuses them. `fetch_deadlines` is used by the settings
  screen and is unrelated.
* Move the reconcile tests to cover the verify route rather than deleting them.

## The degraded-response guard — done 2026-08-04

Not part of the console, but it had to move before the keeper window opens. Decided by the
commissioner in the same conversation.

`rs57.espn` used to refuse any team returning fewer than `MIN_ROSTER_SIZE = 10` roster entries.
That encodes an assumption true for most of the year and false in exactly the window this
redesign is built for — a pruned league is four deep on all twelve teams, and the floor would
have failed the nightly every night from the keeper deadline to draft day.

**The tell for a degraded response is disagreement, not size.** A truncated response hits one
team; a pruned league hits all twelve at once. `check_roster_sizes` now requires the league to
land wholly in one regime — every team at `MIN_ROSTER_SIZE` or deeper, or every team at
`KEEPERS_ONLY_ROSTER_SIZE` (`MAX_KEEPERS + MAX_PROSPECTS`, read off the rules rather than typed
as a 4) or shallower — and raises on a mix. All twelve empty is refused too: keeping nobody is a
legal choice for one team, but an empty league is what a dead API looks like.

A pruned read records a warning naming the state, so a season holding only keepers says so
rather than reading as a league that dropped three quarters of its players.

Downstream readers were checked: `origins_sync`'s degraded guard is about resolution rate, not
roster size, and `validate.py:172` only reports the count. Nothing else assumes a full roster.

## Two existing bugs to fix in passing

Both are small, both are in code this redesign touches, and both get worse once the season page
becomes the primary screen.

**`build_season_screen` does not pass `first_nfl_season`.** It calls `build_team_screen` without
it (`screens.py:441`), so `origins` becomes `{}` rather than `None`. In `keeper_rules`, `{}` means
"prospect rule 1 *is* being applied" while `None` means "not applied" — so every prospect on the
season page raises `PROSPECT_ROOKIE_UNVERIFIED`, and a prospect the team page verified as a
genuine rookie shows as unverified in the league report. Fix: pass `derived.first_nfl_seasons()`
through. A test should assert the two screens agree about the same prospect.

**`ReconcileRow.state` misreports an unpriced claim.** The first check is
`if self.recorded_salary is None: return "espn_only"` (`reconcile.py:66`), but a claim's
`computed_salary` is legitimately `None` when `compute_team_keepers` skipped it — which happens
when the claim names a player no longer on the roster, i.e. exactly when someone is reconciling.
ESPN then has a matching value and the screen says "ESPN has a keeper you never recorded." He was
recorded; there is just no price for him. Add a fourth state (unpriced) rather than a false
accusation. Where a number cannot be known, record no number.

## Prose and tests that now assert the opposite

The lock reverses a rule the tool was deliberately built on. These change **in a commit that says
why**, not quietly:

* `tests/test_admin.py:1196`, `test_the_deadline_is_shown_and_never_enforced` — it posts a claim
  *after* the deadline and asserts it saves. That half stays true. The name and docstring become
  the mirror image: enforced before, never after.
* The `_about` block in `data/manual/seasons.json` — currently "keeper_deadline is shown and
  stamped against submissions, never enforced. A tool that locks the commissioner out at 5:01pm
  is a tool that gets worked around by editing this file by hand." Rewrite to the new rule, and
  keep the second sentence's point: nothing ever re-locks after the deadline, so there is still no
  state that forces a hand edit.
* `screens.py:480` — the note reading "The keeper deadline has passed. Claims are still
  editable…" stops being a warning and becomes the ordinary working state. It should read as
  "the console is open", not as a flag.

## Acceptance

Mutation-check anything that guards an invariant — removing the guard has to make a specific test
fail, verified rather than assumed.

* `/season/<year>` shows all twelve franchises with fee entry inline, no per-team navigation.
* With a deadline recorded in the future: inputs disabled **and** a direct POST to `save` refused
  and nothing written. Remove the server-side refusal and a test fails.
* With a deadline recorded in the past: claims save.
* With **no** deadline recorded: claims save, and the screen says the deadline is unrecorded.
* An illegal fee spread still blocks the save, still prices the team, and still reports which rule
  broke — through the engine, not through JavaScript.
* No admin template carries inline script; no `{{ }}` does arithmetic on money. Both existing
  greps still pass.
* Verify reports a mismatch when ESPN disagrees with a recorded `computed_salary`, reports the
  unrecorded-keeper direction as unavailable when rosters are unpruned, and writes nothing —
  assert `data/derived/` and `data/manual/` are byte-identical across a verify.
* The season page and the team page agree about whether a given prospect's rookie rule was
  checked.
* An unpriced claim that ESPN holds a value for does not report as "ESPN has a keeper you never
  recorded".
* `/season/<year>/reconcile` is gone and nothing links to it.
* `python -m pytest tests/test_admin.py` passes.

## Files

* `rs57/admin/__init__.py` — verify route, reconcile route removed, lock enforced in `save`
* `rs57/admin/screens.py` — `SeasonScreen.editable`, `first_nfl_season` passed through
* `rs57/admin/reconcile.py` — roster comparison, unpriced state, regime detection
* `rs57/admin/templates/season.html` — twelve inline franchises, verify region, overrides
* `rs57/admin/templates/_claim_form.html` — locked state
* `rs57/admin/templates/team.html` — reconcile link removed
* `rs57/admin/templates/base.html` — reconcile nav link removed
* `rs57/admin/templates/reconcile.html` — deleted
* `data/manual/seasons.json` — `_about` rewritten (through the admin tool, which owns it)
* `tests/test_admin.py`

Already done, outside the console: `rs57/espn.py` and `tests/test_espn.py` — the guard swap
above.

Not touched: `rs57/keeper_rules.py`, `rs57/stats.py`, `rs57/sync.py`. The engine is not changing.
If a rule looks wrong, that is a separate conversation.
