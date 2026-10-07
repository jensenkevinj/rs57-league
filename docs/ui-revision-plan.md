# RS57 Site UI Revision: Development Plan

Oct 6, 2026 · Commissioner

## Overview

This plan ships the UI review of the public RS57 site as one change set covering seven workstreams across the Home, Keepers, Seasons, archived season, and Rules pages, plus dark mode and link previews. It changes how results are presented, never how they are computed.

**In scope:** `rs57/templates/` (public site only), `rs57/site.py` view-model builders, `render_markdown`, a new `rs57/static/` folder for icons, and `tests/test_site.py`.

**Out of scope:** the admin tool (`rs57/admin/templates/`), keeper rules, prize logic, sync jobs, and any file under `data/`.

**Constraints carried from CLAUDE.md** that every workstream must respect:

- Templates do no arithmetic on money. Any new number (alive count, days to deadline, season pot) is computed in `site.py` and passed in.
- Autoescaping stays on and no template uses `|safe`. New markup from `render_markdown` (heading ids, section links) is built after escaping, from digits only.
- `site/` has one writer, the nightly build. Static assets live in `rs57/static/` and `build_site` copies them; nothing is committed into `site/` by hand.
- No real names anywhere. Link previews and the Seasons matrix use franchise names only, keyed on `espn_team_id`.
- Displayed dates go through `mdy` and `to_league_time()`. The deadline state uses `models.utc_now()`, injected as `now` for tests.
- Status keeps its word label alongside any color, and every guard added gets a mutation-checked test.

**For Claude Code:** this file lives at `docs/ui-revision-plan.md`. Before starting, read `CLAUDE.md` and the Corrections log in `rs57-league-app-plan.md`, as earlier phases did. When done, write `docs/phase-8-notes.md` in the same shape as the earlier phase notes. One non-template file also changes: `pyproject.toml` (see 7.1).

## Decision log

Seventeen decisions shape the build. Fourteen came from the commissioner's answers; three are defaults chosen where he had no preference or the answer opened a follow-on question.

| Area | Decision | Source |
| --- | --- | --- |
| Home: unplayed weeks | Keep every row, render unplayed rows lighter and shorter | Commissioner |
| Home: Survivor | "Still alive: N" fills the Winner slot; played eliminations newest first | Commissioner |
| Home: Survivor order | Unplayed weeks sit below the played ones, in week order, under an "Upcoming" divider | Default |
| Home: LEADING tag | Not rendered while the season is in progress | Commissioner |
| Home: Moneylist on phones | Moves above the prize columns under 34rem | Commissioner |
| Keepers: deadline alert | Neutral until 14 days out, amber inside 14 days, red once passed | Commissioner |
| Keepers: desktop layout | Grid widens to the full page and the Franchise column returns at 60rem+ | Commissioner |
| Keepers: Acquired | Acquisition type plus date (Draft, Trade, Add) | Commissioner |
| Keepers: Add label | "Add" covers both waiver and free-agent pickups, since ESPN does not separate them | Default |
| Keepers: acquisition filter | None; the label is display only | Commissioner |
| Seasons | One hall-of-fame matrix, seasons as rows, prizes as columns | Commissioner |
| Seasons 2019 to 2023 | Show winners from ESPN; mark prize money as not on record | Commissioner |
| Rules | Sticky contents sidebar on desktop, collapsible contents on mobile, linked section references | Commissioner |
| Dark mode | Follows the phone's system setting, no toggle | Commissioner |
| Branding | Favicon set plus a static link-preview card; no header logo | Default |
| Favicon monogram | "RS57" | Commissioner |
| Delivery | One change set, built in the order under Build order | Commissioner |

## Workstream 1: Global fixes

Seven small fixes in `base.html` and the shared macros apply on every page and should land first, since later workstreams build on the new tokens.

**1.1 Inline tag spacing.** Add `margin-left: 0.35rem` to `.tag-inline`. This covers the remaining tags (split, name unknown) once LEADING is gone.

**1.2 Money formatting on Seasons.** `seasons.html` prints `${{ season.pot }}` and `${{ row.amount }}` raw, giving `$1200`. Route both through the existing `money` filter. This is folded into the new matrix in Workstream 4, but the test lands here.

**1.3 Gold text contrast.** `#b8860b` is 3.25:1 on white and fails AA for text. Add `--gold-text: #8a6508` (5.32:1 on white, 5.0:1 on panel) and use it for `.spot-1 .spot-place`, `.spot-1 .spot-money`, and `.leaders li.top .leader-money`. Borders keep `--gold`.

**1.4 Mobile form inputs.** Inside the `max-width: 34rem` query, set `.controls input, .controls select { font-size: 1rem; }`. Safari zooms any focused input under 16px; the current value computes to 14.4px.

**1.5 Tooltips on phones.** Three changes to `.info`, `.pbadge` and `.wbadge`:

- Hit area: add `::before { content: ""; position: absolute; inset: -0.45rem; }` so the target is about 31px while the circle stays 17px. This clears the WCAG 2.2 24px minimum.
- Position: under 34rem, make `.col-head` `position: relative` and the trigger `position: static`, so the bubble anchors to the heading row and spans it (`left: 0; right: 0; transform: none; width: auto;`). In the keeper grid, do the same with the Player cell. The bubble can then never run off either screen edge.
- iOS focus: Safari does not reliably give a tapped `<button>` focus, so the `:focus` rule may never fire on an iPhone. Add a few lines of script that toggle `aria-expanded` on tap and close on outside tap or Escape, and style `[aria-expanded="true"] .info-bubble` as visible. Keep the CSS hover path for desktop.

**1.6 Moneylist rules.** Each row draws a dotted leader and a solid bottom border. Remove the `border-bottom` from `.leaders li` and keep the dotted leader, which is the printed-sheet look the comments describe.

**1.7 Sort header wrap.** Add `white-space: nowrap` to `.grid .sort` so the sort arrow never drops below "SALARY" on a phone.

## Workstream 2: Home page

The mid-season board keeps its full shape but stops competing with the results: unplayed rows recede, Survivor leads with what has happened, and the LEADING tags go.

**2.1 Lighter unplayed rows.** Applies to Weekly Top Score and Survivor while the season is in progress.

- Add `pending: bool` to `BoardRow` and `SurvivorWeek`, set in `build_home` from the weeks played. The template reads the flag; it never works out which weeks are played.
- Render pending rows as `li.thin.pending` with no dash glyph, just the week key.
- CSS: `padding: 0.12rem 0; font-size: 0.82rem; border-bottom-style: dotted;` and key text in a new `--faint` token, `#6e7681` (4.59:1 on white, so it still passes AA).
- Expected effect: each pending row drops from about 33px to about 22px. At week 4 that recovers roughly 190px on a phone.

**2.2 Survivor column.**

- Add `alive: int | None` to `SurvivorPanel`, computed in `site.py` as franchises minus teams eliminated. Count teams, not weeks, so a tied elimination week stays correct.
- Split `weeks` into `played` (newest first) and `upcoming` (week order).
- While in progress, the Winner slot reads "Still alive" over "8 of 12". Once final, it shows the winner as today.
- Under the played rows, a small uppercase "Upcoming" label (the `.spot-place` style) introduces the pending rows. It renders only when `upcoming` is not empty, so finished seasons are unchanged.

**2.3 Drop LEADING while in progress.** Remove the `row.leading` tag from `index.html`. Keep the `leading` field on `BoardRow`, since it is cheap and may return elsewhere. The "In progress, through week 4" pill carries the meaning. This also fixes the awkward wrap on long names like "Burrdwalk and Parker's Place".

**2.4 Moneylist above the prizes on phones.** Wrap the podium, `.boards`, the pot and the Moneylist in one `.board-page` container with `display: flex; flex-direction: column;`, and give the Moneylist its own `<section class="moneylist">` holding its heading and list. Under 34rem, set `order` so the sequence is podium, Moneylist, boards, pot. Desktop order and the DOM order are unchanged, so screen readers still meet the boards first; that mismatch is acceptable here because no content depends on reading order. On a final season the podium stays first.

**Tests:** a pending row renders with the `pending` class and no dash; `alive` equals franchises minus eliminated teams, including a fixture with a two-team elimination week; no "leading" tag text appears on an in-progress board; a final season's Survivor column has no "Upcoming" label; the Moneylist renders exactly once, inside a `.moneylist` section.

## Workstream 3: Keepers page

The Keepers page gains a heading, a deadline alert that escalates with time, a full-width desktop grid with its own Franchise column, and acquisition type on every row.

**3.1 Page heading.** Add `<h1>Keepers</h1>` and a one-line lede above the alert, naming the season the prices apply to (from `KeeperSeason`, never hard-coded) and that the grid sorts and filters. The page currently opens on a red box with no title.

**3.2 Deadline alert states.** Add `deadline_state` and `days_left` to `KeeperSeason`, computed in `build_keeper_season` from the injected `now` (defaulting to `models.utc_now()`). Days are counted in league time so the count matches the date printed beside it.

**Which date:** the states key off `qualifying_deadline`, the date the alert already prints (the qualifying season's trade deadline). Do not use `source.keeper_deadline`; it drives the tax window in `_charges_in_base` and is a different date. `build_site` gains a `now: datetime | None = None` parameter, passed through to `build_keeper_season`, so tests can render the whole site at a fixed instant.

| State | When | Style | Copy |
| --- | --- | --- | --- |
| upcoming | More than 14 days out | Neutral: `--panel` fill, `--line` border, muted icon | Keeper deadline: 11/25/2026 |
| soon | 14 days or fewer, not yet passed | Amber: the `--flag` tokens | Keeper deadline: 11/25/2026 · 9 days left ("1 day left", "today") |
| passed | After the deadline instant | Red: the `--bad` tokens, as today | Keeper deadline passed: 11/25/2026 |
| unknown | No deadline on file | Unchanged from today | Unchanged |

The site rebuilds daily at 09:17 UTC (about 5 AM Eastern), so a state flips at the first build after its threshold and `days_left` is accurate as of that build. Late-acquisition rows stay red in every state; red now means "cannot act on this" everywhere on the page.

**3.3 Full-width grid on desktop.** At `min-width: 60rem` only:

- Lift the 44rem cap: `.grid, .controls { max-width: none; }`.
- Show the Franchise column (`nth-child(3)`) and hide the folded `.sub` line, which leaves the Player cell as one line: name, then position and NFL club.
- Widths: Franchise `13rem` with ellipsis, Acquired `8.5rem` (wider for the type label), Base and Tax `4.6rem`, Salary `5.6rem`. Player takes the rest.
- Franchise text uses `--muted` so twelve repeats down a column read quietly, the concern the existing CSS comment raises.
- Between 34rem and 60rem nothing changes: the capped, folded grid stays.
- Check the sticky header's `--head-top` measurement still lines up, since the controls will wrap differently at full width.

**3.4 Acquisition type.** `KeeperLine.source` already carries `draft`, `trade`, `waiver` or `faab`. Add `acquired_label` in `site.py`: Draft, Trade, or Add (waiver and faab both map to Add). Desktop cell: "Draft · 9/3/2026". Phone sub-line: "Belichick's Spy · Draft 9/3/2026". Sort stays on the date via `data-k`. Late rows keep the date in red.

No filter is added for acquisition type. The label is display only, and the controls bar is unchanged.

**3.5 Mobile separator fix.** The phone sub-line renders "Belichick's Spy· 9/3/2026" because the leading space in `" · "` is the first character of a flex item, and flex collapses it. Remove the literal spaces and set `.grid .sub { gap: 0.3rem; }` with the dot as its own span.

**Tests:** deadline state at 15 days, exactly 14 days, one minute before, and one minute after the deadline, each mutation-checked against its threshold; every `AcquisitionSource` member maps to a label (iterate the enum so a new member fails the test instead of rendering blank); the sub-line contains a separator element; the h1 is present.

## Workstream 4: Seasons and archived seasons

Eight per-season tables become one hall-of-fame matrix, and each archived season page gets links back to the list and to its neighbors.

**4.1 The matrix.** One table, newest season first. Columns: Season, Champion, 2nd, 3rd, Most Points, Survivor, Unlucky, Pot.

- Build each row from `build_home(season)`, the same object the archived page renders. The matrix and the season page then cannot disagree, and no second derivation of winners exists.
- `table-layout: fixed` with set widths, so columns line up down the whole page.
- Season cell links to `season-{year}.html`. Champion is bold. Ties render through the existing `ui.teams` macro ("A & B").
- Pot uses the `money` filter (`$1,200`).
- In-progress season: one cell spanning Champion through Unlucky reads "In progress, through week 4". Pot still shows.
- 2019 to 2023: winners show as for any season, since the derived stats and ESPN standings exist for those years. Pot shows a dash with a footnote marker, and one line under the table reads "Prize money was not recorded before 2024. Winners are from ESPN." The line renders only if such a season is present.
- Phone (under 34rem): keep Season and Champion, fold 2nd and 3rd into a muted sub-line under the champion, and hide the remaining columns. The season link carries the reader to everything else.
- Rewrite the lede to match: every season at a glance, with the full board one tap away.

**4.2 Archived season navigation.** On each `season-{year}.html`, add a small line under the heading: "← All seasons" plus links to the previous and next years where they exist. `build_site` already loops over seasons in order, so pass `prev_season` and `next_season` into each render. The live home page gets neither.

**Tests:** one matrix row per synced season; each row's champion matches that season's podium rank 1; pot renders as `$1,200` and never as `$1200` or `$0`; the footnote appears only when a season without recorded money is present; no previous link on the oldest season and no next link on the newest.

## Workstream 5: Rules page

The rules become navigable: every numbered section gets an anchor, every "§2.3" becomes a link, a contents sidebar follows the reader on desktop, and prose narrows to a readable measure.

**5.1 Heading anchors in `render_markdown`.** Give each numbered heading an id built from its section number only: "1. Definitions" becomes `s1`, "2.3 Keeper Fee" becomes `s2-3`. Ids come from a digits-only regex match, so nothing user-typed reaches an attribute. Headings without a number get no id.

**5.2 Linked section references.** After escaping, replace `§(\d+(?:\.\d+)?)` with an internal link to the matching id, but only when that id exists. This is internal anchors only; the docstring's "no links" rule stays true for external URLs, and the docstring is updated to say so.

**5.3 Outline.** Add a pure `rules_outline(text)` that returns id, number, title and level for each numbered heading, and pass it to `rules.html`. Both the sidebar and the mobile contents are built from it.

**5.4 Layout.**

- Desktop (60rem and up): a two-column grid, `13rem` contents and `minmax(0, 42rem)` prose, `2.5rem` gap. The contents column is `position: sticky; top: 1rem;` with its own scroll if taller than the screen. Sections list with subsections indented one level.
- Below 60rem: a collapsed `<details>` labeled "Contents" above the rules. It is a second rendering of the same outline; whichever one is not in use is `display: none`, so screen readers meet only one.
- Prose `max-width: 42rem` at every width, down from about 95 characters a line to about 70.
- Headings get `scroll-margin-top: 1rem` so a jump does not land the heading against the screen edge.
- Optional enhancement: a short IntersectionObserver script highlights the current section in the sidebar. The page works fully without it.

**Deferred:** turning the Keeper Fee tiers into a table. `render_markdown` deliberately has no table support, and adding it is a larger change than this one item is worth.

**Tests:** every numbered heading has its id; every § reference in `docs/rules.md` resolves (mutation-check by adding a bogus §9.9, which must fail the test); a `<script>` in a heading still renders as text; outline order matches heading order.

## Workstream 6: Dark mode

Dark mode is one `@media (prefers-color-scheme: dark)` block that redefines the tokens, plus four new tokens to replace the colors currently hard-coded in rules. Every text pairing below was checked and clears 4.5:1.

**6.1 Replace hard-coded colors first.** These break in dark mode if left alone:

- `.info-bubble` uses `--ink` as its background and `#fff` as text. In dark mode `--ink` turns light and the text vanishes. Use new `--bubble-bg` and `--bubble-fg`.
- `.pbadge.is-eligible` uses `#fff` on `--accent`. Use new `--on-accent`.
- `.spot-1` hard-codes `rgba(184, 134, 11, …)` in its gradient and shadow. Move to a `--gold-wash` token, much fainter in dark mode, and drop the shadow there.

**6.2 Token values.**

| Token | Light | Dark | Dark contrast |
| --- | --- | --- | --- |
| `--bg` | #ffffff | #111418 | n/a |
| `--panel` | #f6f8fa | #1a1f25 | n/a |
| `--ink` | #1b1f24 | #e6e9ed | 15.2:1 on bg |
| `--muted` | #5b6470 | #9aa4b0 | 7.3:1 on bg, 6.6:1 on panel |
| `--faint` (new) | #6e7681 | #7d8691 | 5.0:1 on bg |
| `--line` | #dfe3e8 | #2e353d | n/a (rules only) |
| `--accent` | #0b5cad | #5aa2ec | 6.9:1 on bg |
| `--on-accent` (new) | #ffffff | #0b1a2b | 6.5:1 on accent |
| `--flag` / `--flag-bg` / `--flag-line` | unchanged | #e0b45a / #2a2214 / #6b5520 | 8.1:1 |
| `--bad` / `--bad-bg` / `--bad-line` | unchanged | #f08a78 / #2c1714 / #7a3a30 | 7.0:1 |
| `--good` / `--good-bg` / `--good-line` | unchanged | #6cc493 / #13261b / #2f5c42 | 7.5:1 |
| `--gold` (borders) | #b8860b | #d4a537 | n/a |
| `--gold-text` (new) | #8a6508 | #d4a537 | 8.1:1 on bg |
| `--silver` / `--bronze` | unchanged | #7d8590 / #b98a63 | n/a (borders) |
| `--bubble-bg` / `--bubble-fg` (new) | #1b1f24 / #ffffff | #e6e9ed / #111418 | 15.2:1 |

**6.3 Browser hints.** Add `color-scheme: light dark` on `:root` and `<meta name="color-scheme" content="light dark">` so form controls, the checkbox and scrollbars follow the theme. Add two `theme-color` metas with `media` queries so the phone's browser bar matches the page.

**6.4 Scope.** Public site only. The admin tool has its own `base.html` and stays light.

## Workstream 7: Favicon and link previews

A link pasted in the group chat will show a title, a one-line summary and a simple RS57 card instead of a bare URL, and the browser tab gets an icon.

**7.1 Static assets.** Create `rs57/static/` holding:

- `favicon.svg`: an "RS57" monogram in `--accent` on a rounded square.
- `apple-touch-icon.png` at 180×180 and `favicon.ico` at 32×32 as fallbacks.
- `og-card.png` at 1200×630: "RS57" wordmark, the line "12-team keeper auction league", and an accent rule on a plain background.

These are made once and committed as source: the SVG by hand, and the PNG and ICO files with a throwaway Pillow script that is run once and not committed (install Pillow ad hoc; never add it to `pyproject.toml`). `build_site` copies `rs57/static/*` into `site/` and includes them in the paths it returns. Nothing is committed into `site/` directly, so the one-writer rule holds.

**Packaging:** add `"static/*"` to `[tool.setuptools.package-data]` in `pyproject.toml`. The nightly installs the package, and today that list holds only `templates/*.html` and the admin assets, so without this line the icons and card are silently left out of the published site.

**7.2 Meta tags.** In `base.html`, add a `meta` block each page can fill: `og:title`, `og:description`, `og:url`, `og:image`, `og:type` and `twitter:card` set to `summary_large_image`. Absolute URLs come from one `SITE_URL` constant in `site.py`.

**7.3 Page descriptions,** computed in `site.py` from data already built:

| Page | Example description |
| --- | --- |
| Home, in progress | Through week 4. Moneylist leader: Jaxian McJigberson, $30. |
| Home, preseason | Draft 9/3/2026. Keeper deadline 11/25/2026. |
| Archived season | 2025 champion: Purdy Good at Fantasy. |
| Keepers | Keeper prices for every rostered player. Deadline 11/25/2026. |
| Seasons | Champions and prize winners for every season since 2019. |
| Rules | RS57 league rules in effect for 2026. |

Franchise names only, never a manager id or the "name unknown" fallback; when a name is unknown, the description drops that clause.

**Tests:** every page carries an absolute `https` `og:image`; no description contains a manager id; the static files land in the build output, and `pyproject.toml` package data includes `static/*` (a test that reads the file, so dropping the line fails it).

**After deploy:** paste a link into the league chat. Chat apps cache previews per URL for days, so the home page summary will lag the live page. That is expected.

## Build order

The work lands as one change set, built as nine commits on one branch so each step can be reviewed or reverted on its own. Tokens come first because every later step uses them.

1. **Tokens.** Add `--faint`, `--gold-text`, `--on-accent`, `--bubble-bg`, `--bubble-fg` and `--gold-wash` with light values, and swap the hard-coded colors over. The site should look identical after this commit.
2. **Global fixes.** Workstream 1, items 1.1 to 1.7, including the tooltip tap script.
3. **Home page.** Workstream 2: the new `pending` and `alive` fields in `site.py`, then the template and CSS.
4. **Keepers.** Workstream 3: heading, deadline state, acquisition label and separator fix first, then the desktop grid last, since it carries the most layout risk.
5. **Seasons.** Workstream 4: the matrix, then archived season navigation.
6. **Rules.** Workstream 5: anchors, outline and links in `render_markdown`, then the layout.
7. **Dark mode.** Workstream 6: the dark token block and the browser hints.
8. **Link previews.** Workstream 7: static assets, `pyproject.toml` package data, the copy step in `build_site`, meta tags and descriptions.
9. **Verification pass.** Everything under Testing and verification, then merge.

Preview locally with `python -m rs57.site --preview` (it writes `.preview/`) and never commit the generated `site/` from the laptop. After merge, the next nightly run (or a manual run of the workflow) publishes the new pages.

## Testing and verification

The change set is done when the automated tests pass with their guards mutation-checked, every page passes a visual review at two widths in both themes, and a real phone confirms the touch behavior.

**Automated (pytest).** Each workstream above lists its tests. Five of them guard an invariant and must be mutation-checked per the CLAUDE.md rule, meaning the guard is removed and a named test is seen to fail:

- Deadline state thresholds (14 days, the deadline instant).
- Survivor `alive` count, including a two-team elimination week.
- Every § reference in the rules resolves.
- Every `AcquisitionSource` member has a label.
- No manager id appears in any link-preview description.

**Visual review.** A dev-only Playwright script (not in CI) renders each page from `.preview/` and saves screenshots for side-by-side comparison with today's site.

| Dimension | Values |
| --- | --- |
| Pages | Home, Keepers, Seasons, Rules, season-2025 (final), season-2021 (no prize money) |
| Widths | 1280px, 390px |
| Themes | light, dark (Playwright `color_scheme`) |
| Home states | preseason, in progress, final, via test fixtures |
| Keepers states | upcoming, soon, passed, via an injected `now` |

**Real phone checklist.**

- [ ] iPhone Safari: each "i" opens on tap and closes on a second tap or an outside tap.
- [ ] iPhone Safari: tapping the keeper filter does not zoom the page.
- [ ] Both phones: the keeper column header stays stuck while scrolling.
- [ ] Both phones: switching the system to dark mode restyles the page with no unreadable text.
- [ ] Android Chrome: the same tooltip and filter checks.

**Accessibility.** Tab through every tooltip, sort header and filter with the keyboard, and run Lighthouse's accessibility audit on the six built pages, aiming for no new findings.

**Link previews.** After deploy, paste the home, keepers and a season link into the league chat and confirm the card, title and description appear.

## Risks and open items

The biggest risk is the desktop Keepers grid, which reverses a layout the existing CSS comments argued for; the rest are small and each has a fallback.

| Risk | Impact | Fallback |
| --- | --- | --- |
| Franchise column brings back the repeated-name "wall" the CSS comments warned about | Desktop Keepers reads busier | Muted color first; if still busy, revert the one 60rem media query |
| Tooltip tap script is the first script on the Home page | A script error leaves tooltips hover-only | Keep it under 20 lines and progressive; the CSS hover path stays |
| Eight-column Seasons matrix between 34rem and 60rem | Tablets may scroll sideways | Already in a `.scroll` wrapper; fold Survivor and Unlucky below 48rem if it looks cramped |
| Unverified notes on 2019 to 2023 stats | The matrix could present unchecked winners as settled | Follow the commissioner's 2026-09-14 decision for the board: notes stay in `validate` and CI, not on the page |
| Site updates only on the nightly run | Merged changes appear up to a day later | Run the workflow manually after merge |

**Resolved questions:** the Moneylist moves above the prize columns on phones (2.4). No acquisition filter is added (3.4). The favicon monogram is "RS57" (7.1).

**Acceptance checklist:**

- [ ] All seven workstreams merged; `pytest` green with the five guards mutation-checked.
- [ ] `pyproject.toml` package data includes `static/*`, and the icons appear on the published site.
- [ ] Screenshot review done at 1280px and 390px, light and dark, across the listed states.
- [ ] Real phone checklist complete on iPhone and Android.
- [ ] Lighthouse accessibility shows no new findings.
- [ ] Link preview confirmed in the league chat.
- [ ] No `site/` or `data/` files in the change set.
- [ ] `docs/phase-8-notes.md` written.
