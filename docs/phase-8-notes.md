# Phase 8 — the public site's UI revision

The plan is `docs/ui-revision-plan.md`. It changes how results are presented and never how they
are computed: no keeper rule, prize rule, sync job or file under `data/` was touched. Everything
is in `rs57/templates/`, the view-model half of `rs57/site.py`, a new `rs57/static/`, one line of
`pyproject.toml`, and `tests/test_site.py`. The admin tool has its own templates and is unchanged.

Built as eight commits in the plan's order, plus one that corrects a mistake made along the way
(below). 789 tests before, 837 after.

## What to know before touching this

**Every colour is a token, and dark mode is one block that redefines them.** A literal in a rule
is a colour the theme cannot reach — the tooltip was `--ink` with `#fff` on it, which is white
on white once `--ink` turns light. `test_no_rule_carries_a_colour_literal` holds the line, and
`test_dark_mode_redefines_every_colour_and_adds_none` catches a token left out of the dark
block. Contrast is not taken on trust from the plan's table: a test computes the WCAG ratio of
every text token on the background it is set on, in both themes, from the stylesheet itself.
A new pairing in the CSS needs a row in that test's `TEXT_ON` list.

**The Survivor column changed shape, and it reverses a decision three weeks old.** On
2026-09-14 the ladder was made latest-first so it "built up from Week 1 at the bottom". It now
shows played weeks newest first and the weeks to come in order under "Upcoming"
(`SurvivorPanel.played` / `.upcoming`; `.weeks` is gone). The old shape put eleven empty rows
above week 1's result. "Still alive" counts **teams** out, not weeks — a tie takes two.

**`pending` is not the same as empty.** A week after `weeks_played` in an unfinished season is
pending: a lighter row with no dash. A week at or before it with nothing on file is a hole and
keeps its "—". A finished season has neither; its empty prize says "unawarded".

**The deadline alert reads `qualifying_deadline`, not `source.keeper_deadline`.** They are
different dates with different jobs. `deadline_status` uses two clocks deliberately: *passed* is
the instant in naive UTC, the same comparison the late-row marking makes; *days left* are
calendar days in league time, so "today" agrees with the date printed beside it. It is display
only. Nothing gates on it and nothing may.

**The Seasons matrix is read off `Home`, not `StatsSeason`.** `build_home` is where a payout
row and the standings are reconciled and where an unfinished season is kept from claiming a
champion. Reading its answer back is the only way the index cannot disagree with the page each
row links to. `build_site` now builds each season's `Home` once and uses it three times.

**`render_markdown` now emits links, but only the ones it builds itself.** A numbered heading
gets `id="s2-3"` and `§2.3` links to it when that section exists. Both come from a digits-only
regex group, after escaping. Links written in the rules file are still unsupported.
`unresolved_section_refs` names a reference that points nowhere; a test holds `docs/rules.md`
to none, so renumbering a section fails a test rather than leaving a dead reference.

**The first script on the home page.** Twenty lines at the foot of `base.html` that set
`aria-expanded` on a tapped tooltip. Safari does not focus a tapped button, so `:focus` alone
never opened one on an iPhone. If it fails the bubbles still open on hover and on focus.

## Departures from the plan, and why

**There is no `SITE_URL` constant.** The plan asked for one. The address is
`https://<account>.github.io/<repo>/`, the account is a person's handle, and this repo holds
none — a search found zero occurrences before this phase. `site_url_from` reads
`RS57_SITE_URL` or the Action's own `GITHUB_REPOSITORY` at build time instead. A laptop
preview has neither, and then carries a title and a description but no `og:url` or `og:image`,
and `python -m rs57.site` says so. **The generated `site/` will still contain the address**
once the nightly builds it, because `og:image` has to be absolute. That is the same address in
the reader's own browser bar, but it is the commissioner's call whether it belongs in a
committed file.

**The favicon is white on an accent square, in two rows.** The plan said an "RS57" monogram in
`--accent` on a rounded square. Four characters across a 16px tab icon are not legible, and
accent letters on a white square disappear on a light tab bar.

**The tooltip's enlarged hit area hangs off the glyph, not the button.** On a phone the button
becomes `position: static` so its bubble can anchor to the heading row. A `::before` on the
button would then stretch to the whole heading.

**The Keepers column header reads "Franchise", not "Team".** It is visible on a desktop now,
and "Team" beside an NFL club abbreviation is ambiguous.

**`render()` in the tests reads `*.html` only.** The build now copies binary files into the
output directory, and reading every file as text failed 100 tests at once.

## The mistake

The plan was copied into `docs/` in the first commit with its byline and a decision log that
attributes each answer by first name. This repo is public and publishes franchise names only.
A later commit replaces both with "Commissioner", so the file is right going forward. **The
name is still in the first commit's version of the file.** The branch had not been pushed when
this was written; rewriting it before it is would keep the name off the remote entirely.

## Verified, and not

Verified here:
- `pytest`, 837 passing.
- The five guards the plan names, each mutation-checked by removing the guard and seeing a named
  test fail: the deadline thresholds (14 days, and the instant, in both directions), the
  Survivor `alive` count with a two-team week, a bogus `§9.9` added to the real rules file,
  a missing `AcquisitionSource` label, and a manager id in a description. Twenty-odd further
  mutants across the other guards were killed the same way.
- Six pages at 390px and 1280px, light and dark, in the app's browser: no horizontal overflow,
  no console errors. The home board in progress and final; the keeper alert in all three dated
  states via an injected `now`.
- Tooltips open on a click, close on a second click and on an outside click, and stay inside a
  375px screen.

**Not verified — these need a person or a deploy:**
- The real phone checklist. Whether an iPhone opens a tooltip on tap and stops zooming into the
  filter can only be seen on an iPhone. The desktop browser emulates a width, not Safari.
- Lighthouse's accessibility audit, and a keyboard walk through every control.
- Link previews. They need the published site, and chat apps cache a preview per URL for days.
- The pre-draft home page was checked by its tests only, not by eye.
- The plan's Playwright screenshot script was not written. The sweep above was done in the
  app's browser instead, and left no artefact to diff against next time.

## Open
- The name in the first commit, above.
- Whether the Pages address may appear in committed `site/` HTML, above.
- Rows on a 2019–2023 season page still show a "—" in the money column for every prize. The
  matrix explains the missing money once; the season page repeats it per row. Untouched here.
- `site.py`'s `seasons` template variable is no longer read by any template.
