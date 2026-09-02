"""Render the public static site from ``data/`` into ``site/``.

    python -m rs57.site              # writes site/ — this is the Action's mode
    python -m rs57.site --preview    # writes .preview/, which is gitignored

The nightly Action owns ``site/`` and ``data/derived/``. NO FILE HAS TWO WRITERS, so a laptop
run belongs behind ``--preview``; ``.preview/`` is in ``.gitignore`` precisely so a local
render cannot be committed by accident.

Direction of dependency
-----------------------

This module imports ``keeper_rules``; ``keeper_rules`` knows nothing about it. Salaries come
out of the engine and are handed to the templates already computed, because a Jinja expression
doing arithmetic on money is a second implementation of the keeper rules that nobody tests.
The templates loop and format. They do not calculate.

What may be published
---------------------

The repo and the site are PUBLIC. Franchise names and NFL player names only — this module
never reads anything that holds a manager's real name, and no such mapping exists in the repo
to read. Managers appear as ``manager_id`` (``t{espn_team_id}``) resolved through the season's
``FranchiseName`` rows, and as ``t{id}`` when that season has not been synced.

Autoescaping is on and no template uses the ``safe`` filter. The one place text becomes markup
is ``render_markdown``, which escapes *before* it adds any tags — see its docstring.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from pydantic import ValidationError

from rs57.keeper_rules import (
    FEE_TIERS,
    KEEPER_TAX,
    MAX_KEEPERS,
    MAX_PROSPECTS,
    Severity,
    charges_in_base as _charges_in_base,
    effective_base_salary,
    keeper_salary,
)
from rs57.models import (
    STALE_WAIVER_WARNING,
    Base,
    Dues,
    FranchiseName,
    KeeperClaim,
    KeeperSlot,
    Payout,
    Player,
    RosterEntry,
    SalaryOverride,
    Season,
    SeasonPoints,
    StandingRow,
    StudAward,
    SurvivorElimination,
    UnluckyAward,
    WeeklyHigh,
    to_league_time,
    utc_now,
)
from rs57.history import HistoryStore
from rs57.origins import load_first_season_bounds, load_player_origins

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DERIVED = DATA / "derived"
HISTORY = DATA / "history"
MANUAL = DATA / "manual"
RULES_MD = ROOT / "docs" / "rules.md"
TEMPLATES = Path(__file__).resolve().parent / "templates"
SITE = ROOT / "site"
PREVIEW = ROOT / ".preview"

SURVIVOR_RULE = (
    "Every week the lowest score among the teams still alive is eliminated, and the last team "
    "standing wins. It runs one week for every team but one."
)
"""Stated once. Survivor is shown as a column when it has a ladder and as a block when it does
not, and the two must not drift apart."""


class ReviewIssue(Base):
    """One unverified item, as ``stats_sync`` writes it into ``{year}-stats.json``.

    Loaded into a model rather than passed around as a dict, like everything else here. A
    REVIEW item is the whole reason this file has a schema: it must reach the page, and it
    must reach it labelled.
    """

    code: str
    severity: Severity
    message: str
    manager_id: str | None = None
    week: int | None = None
    position: str | None = None


@dataclass(frozen=True)
class Note:
    """Something the site is showing that nobody has checked.

    ``kind`` is ``"review"`` or ``"error"``. Both render as unverified; neither is ever
    rendered as though it had passed.
    """

    kind: str
    message: str
    where: str | None = None


@dataclass(frozen=True)
class KeeperLine:
    """One rostered player and what keeping him would cost, before any fee allocation.

    ``declared`` separates a *claim* from a *price*. Every rostered player carries a price,
    because pricing him needs nothing but the roster; only a player the admin tool has recorded
    a ``KeeperClaim`` for is actually being kept, and only he has a fee and a final salary. A
    page that blurred the two would publish a number nobody owes.
    """

    player_name: str
    position: str
    nfl_team: str
    base: int
    tax: int
    price: int
    kept_prior_year: bool
    source: str
    declared: bool = False
    slot: str = ""
    fee: int = 0
    salary: int | None = None
    """The salary recorded at declaration — the figure the manager was told they owed."""
    first_nfl_season: int | None = None
    """From ESPN's draft class, or ``None`` when ESPN has nothing for him."""
    prospect_state: str = "unknown"
    """``eligible`` | ``ineligible`` | ``unknown``. All three rules, never just the rookie one —
    see ``build_keeper_season``. ``unknown`` says so on the page rather than reading as a no."""
    acquired_at: datetime | None = None
    """When this manager got him, straight off the roster entry."""
    team_name: str = ""
    """The franchise holding him, so a flat row needs no enclosing team to read itself."""
    overridden: bool = False
    """A live draft-cash override is correcting this base, so ESPN currently disagrees.

    Derived from the numbers rather than from the presence of a row: a reverted override, or
    one that happens to match ESPN, is not a discrepancy and must not be flagged."""
    after_deadline: bool = False
    """Acquired after the qualifying season's trade deadline.

    A **fact**, not a verdict. It is the prospect rule's third test, and the page marks it so a
    manager can see it, but the engine applies it to prospects only — see the template."""


@dataclass(frozen=True)
class TeamKeepers:
    manager_id: str
    name: str
    name_known: bool
    lines: tuple[KeeperLine, ...]
    taxed: int
    fees_waived: bool
    declared_count: int = 0
    declared_salary: int = 0
    declared_fees: int = 0


@dataclass(frozen=True)
class KeeperSeason:
    """The keeper side of one season: what every rostered player would cost to keep."""

    season: int
    drafted: bool
    base_field: str
    teams: tuple[TeamKeepers, ...]
    notes: tuple[Note, ...]
    waiver_manager_id: str | None
    waiver_name: str | None
    waiver_from_season: int | None
    decision_season: int = 0
    """The season this page's prices and eligibility marks are *about*, which is not always
    ``season`` — see ``build_keeper_season``. Set there; the default is never used."""
    any_eligible: bool = False
    """Whether any player is prospect-eligible. Before an NFL draft class reaches the rosters
    the honest answer is none, and the page says that rather than showing an empty filter."""
    qualifying_season: int = 0
    """The completed season keepers are chosen from — ``decision_season - 1``."""
    qualifying_deadline: datetime | None = None
    """That season's trade deadline, printed above the grid. ``None`` when it is not on disk,
    in which case the page says so rather than showing a date it does not have."""
    charges_in_base: bool = False
    """Whether the salaries below already carry their fee and $5 tax — the window between the
    keeper deadline and the auction. Printed above the grid, because otherwise an empty Tax
    column beside a taxed player is a number the page is quietly not showing."""
    rows: tuple[KeeperLine, ...] = ()
    """Every rostered player in the league, flat, **dearest first**.

    The grid's default order, and it is produced here rather than by the script so the page a
    browser with no JavaScript receives is already in it. The script's initial sort state is
    set to match; if the two ever disagree, the first click on Salary would appear to do
    nothing."""


@dataclass(frozen=True)
class Named:
    """A manager id with the franchise name for that season, when it is known."""

    manager_id: str
    name: str
    known: bool


@dataclass(frozen=True)
class PrizeRow:
    winner: Named | None
    amount: int


@dataclass(frozen=True)
class PrizeGroup:
    """One prize. Several rows when a tie split it; a single unawarded row when nobody won."""

    label: str
    rows: tuple[PrizeRow, ...]


@dataclass(frozen=True)
class Earning:
    team: Named
    total: int


@dataclass(frozen=True)
class StandingLine:
    team: Named
    wins: int
    losses: int
    ties: int
    points_for: float
    points_against: float
    final_rank: int | None
    playoff_seed: int | None


@dataclass(frozen=True)
class HighLine:
    week: int
    teams: tuple[Named, ...]
    points: float


@dataclass(frozen=True)
class StudLine:
    position: str
    player_name: str
    week: int
    points: float
    teams: tuple[Named, ...]


@dataclass(frozen=True)
class PointsLine:
    team: Named
    points: float


@dataclass(frozen=True)
class StatsSeason:
    """The scoring side of one season: standings, prizes, and what paid out."""

    season: int
    regular_season_weeks: int
    weeks_played: int
    playoff_team_count: int
    """How many teams make the bracket, which is what says how many rounds it takes. Zero
    means the season was synced before this was recorded — the round is then not named."""
    final: bool
    """Whether the playoffs have decided a final rank. A season still being played has none,
    and the home page must not present its prize board as settled."""
    names_known: bool
    standings: tuple[StandingLine, ...]
    season_points: tuple[PointsLine, ...]
    weekly_highs: tuple[HighLine, ...]
    studs: tuple[StudLine, ...]
    survivor_eliminations: tuple[HighLine, ...]
    survivor_winners: tuple[Named, ...]
    unlucky: HighLine | None
    prizes: tuple[PrizeGroup, ...]
    earnings: tuple[Earning, ...]
    pot: int
    unawarded: int
    consolation_winners: tuple[Named, ...]
    waiver_season: int
    """The season this season's consolation winner would have their fees waived in."""
    notes: tuple[Note, ...]


# ---------------------------------------------------------------------------
# The home page
# ---------------------------------------------------------------------------
#
# The league ran on a spreadsheet of prizes, so that is what the home page is: every prize,
# who won it, what it paid, and the number that won it. Everything on it is assembled here.
# The template lays it out and formats it; it decides nothing.


@dataclass(frozen=True)
class PodiumSpot:
    """A placing prize — the top row is the three the playoff bracket decided, and only those.

    Most Points pays what third place pays but is deliberately NOT here: the podium is what
    the bracket decided, and a team can lead the league in points and miss the playoffs
    entirely. It leads the season awards instead.

    ``winners`` is empty when nobody was identified, which renders as unawarded rather than
    as a blank.
    """

    rank: int
    place: str
    winners: tuple[Named, ...]
    amount: int
    recorded: bool
    """Whether an amount is on record. A season with no recorded prize money shows no
    figure at all — ``$0`` would be a claim that it paid nothing."""
    detail: str


@dataclass(frozen=True)
class BoardRow:
    """One line of the prize board: the prize, who won it, why, and what it pays."""

    label: str
    winners: tuple[Named, ...]
    amount: int
    recorded: bool
    split: bool
    """A tie. The amount shown is the whole prize; the split is what each winner took."""
    detail: str
    """Context for the number, on its own line under the winner — the player and week behind a
    stud, say. Empty when the number speaks for itself."""
    value: str = ""
    """The number that won the prize, in the row's last column. Every row on the board ends in
    one, which is what makes them line up."""
    short_label: str = ""
    """What to call the prize in a narrow column. Empty means ``label`` reads fine as is.

    **Display only.** ``label`` is what the payout rows are matched on, so it is not free to
    change — "Week 1 High Score" is the key that finds the money in ``payouts.json``."""


@dataclass(frozen=True)
class BoardBlock:
    """One headed block of prizes. A column of the board is a stack of these.

    The heading always belongs to the block, so every block on the board is headed the same
    way whether it shares a column or has one to itself.
    """

    title: str
    rows: tuple[BoardRow, ...]
    caption: str = ""
    """The block's rule, shown behind an "i" beside its heading. Empty when the prizes under
    it each carry their own."""
    heading_is_the_prize: bool = False
    """Whether the heading IS this block's prize, in which case the row beneath does not repeat
    it. **Not inferrable from the row count.** "Other prizes" is a category heading that can
    hold a single row, and hiding that row's label would take the prize's name off the page
    entirely — which is how a prize disappears without anything looking wrong."""

    @property
    def each(self) -> int:
        """The one figure every prize here pays, or 0 when they do not all pay the same.

        The money belongs to the heading whenever it is true of the whole block — one prize
        or fourteen. A block of unequal prizes says nothing and every row keeps its own, and
        a block with an unrecorded amount in it is not uniform either: "$25 each" over a row
        whose money is unknown would be inventing that row's.
        """
        amounts = {row.amount for row in self.rows}
        if self.rows and len(amounts) == 1 and all(row.recorded for row in self.rows):
            return amounts.pop()
        return 0

    @property
    def each_is_per_row(self) -> bool:
        """Whether the heading's figure is paid repeatedly. One prize is not paid "each"."""
        return len(self.rows) > 1

    @property
    def each_recorded(self) -> bool:
        return self.each > 0

    @property
    def show_labels(self) -> bool:
        return not self.heading_is_the_prize


@dataclass(frozen=True)
class SurvivorPanel:
    """The survivor ladder, as its own column, the way the league's spreadsheet had it.

    **This column IS the Survivor prize, not a retelling of it.** The prize is shown here
    instead of in the season awards, so it carries the money and there is no Survivor block
    beside the other season prizes. A season with no ladder to show has no column either, and
    the prize stays a block there — the money appears exactly once either way, and the pot
    foots in both.
    """

    winners: tuple[Named, ...]
    amount: int
    recorded: bool
    eliminations: tuple[HighLine, ...]
    caption: str = SURVIVOR_RULE


@dataclass(frozen=True)
class LeaderLine:
    """A franchise's take for the season, with the bar width the template draws."""

    rank: int
    team: Named
    total: int


@dataclass(frozen=True)
class Home:
    """Everything the home page shows about the most recent season with results."""

    season: int
    heading: str
    """What the page calls itself. A settled season claims its result; an unfinished one says
    it is unfinished, so the heading alone never reads as a final answer."""
    status: str
    final: bool
    money_recorded: bool
    podium: tuple[PodiumSpot, ...]
    columns: tuple[tuple[BoardBlock, ...], ...]
    """The board, as columns of headed blocks. A column is a plain tuple — it needs no type of
    its own, because it carries nothing but its blocks."""
    survivor: SurvivorPanel | None
    leaders: tuple[LeaderLine, ...]
    pot: int
    unawarded: int
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class DuesLine:
    """One franchise, and whether it has paid into the league this season."""

    team: Named
    paid: bool


@dataclass(frozen=True)
class DuesBoard:
    """Who has paid their buy-in for the season being played.

    Counts, not money. What the buy-in costs is not recorded anywhere in ``data/`` and this is
    not the place to start — the panel says who still owes, which is the whole of what anyone
    reads it for.

    ``all_paid`` is the signal to stop showing it. A board of twelve ticks tells a reader
    nothing and would sit on the home page until January, so ``build_site`` drops the panel
    once this is true.
    """

    season: int
    rows: tuple[DuesLine, ...]
    paid_count: int
    total: int
    all_paid: bool


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")


def render_markdown(text: str) -> Markup:
    """Render the small Markdown subset ``docs/rules.md`` uses.

    **Escaping happens first, structure second.** Every line is HTML-escaped before a single
    tag is added, so the only characters still carrying meaning afterwards are ``*`` and
    backticks, which escaping leaves alone. Any ``<script>`` in the source comes out as text.

    That ordering is why this is forty lines of regex instead of a Markdown dependency: the
    usual libraries pass raw HTML through by design, which would mean handing the rules file
    the ability to inject markup and reaching for ``|safe`` to do it.

    Supported: ``#``/``##``/``###`` headings, ``-`` bullets, blank-line-separated paragraphs,
    ``**bold**``, ``*emphasis*`` and ``` `code` ```. Not supported, deliberately: raw HTML,
    links, images, tables. If the rules page needs one of those, add it here where the
    escaping is.
    """

    def inline(line: str) -> str:
        safe = str(escape(line))
        safe = _INLINE_CODE.sub(r"<code>\1</code>", safe)
        # Bold before emphasis: ``**x**`` must not be read as an empty emphasis either side
        # of ``*x*``.
        safe = _BOLD.sub(r"<strong>\1</strong>", safe)
        safe = _ITALIC.sub(r"<em>\1</em>", safe)
        return safe

    html: list[str] = []

    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        # Lines are joined back together before any inline formatting runs. The rules file is
        # hard-wrapped prose, so a bold run routinely opens on one line and closes on the
        # next; formatting line by line would leave both asterisks on the page.
        if lines[0].lstrip().startswith("- "):
            items: list[str] = []
            for line in lines:
                stripped = line.lstrip()
                if stripped.startswith("- "):
                    items.append(stripped[2:])
                else:
                    # A continuation of the bullet above, not a new one.
                    items[-1] += " " + stripped
            html.append("<ul>" + "".join(f"<li>{inline(item)}</li>" for item in items) + "</ul>")
            continue

        heading = re.match(r"^(#{1,3})\s+(.*)$", lines[0])
        if heading:
            level = len(heading.group(1)) + 1  # h1 is the page title, so ``#`` becomes h2
            html.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            if lines[1:]:
                html.append("<p>" + inline(" ".join(lines[1:])) + "</p>")
            continue

        html.append("<p>" + inline(" ".join(lines)) + "</p>")

    return Markup("\n".join(html))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(model: type, raw: Iterable[Any], notes: list[Note], where: str) -> list[Any]:
    """Load rows into their model, reporting a schema drift instead of raising.

    A season whose file has drifted must not take the whole site down — the other season
    still renders, and the page says this one could not be read.
    """
    try:
        return [model(**row) for row in raw or []]
    except (ValidationError, TypeError) as exc:
        notes.append(Note("error", f"{where} does not load into {model.__name__}: {exc}"))
        return []


def load_overrides(
    history_dir: Path = HISTORY, manual_dir: Path = MANUAL
) -> list[SalaryOverride]:
    """Every salary override the site should price with. Read-only, from **both** sources.

    An override lives in one of two places depending on who recorded it, and the site needs
    both or it prices with half of them:

    * ``data/history/{year}.json`` — frozen into a completed season by the backfill.
    * ``data/manual/overrides.json`` — recorded in the admin tool, which is where the
      commissioner enters a draft-cash trade *while it is still live*. This is the file that
      matters for the season being decided, and it is the one the site used to ignore.

    Reading only ``history/`` was a real hole: the admin tool would accept an override,
    ``validate`` would count it — it reads ``history.overrides + manual_overrides`` — and the
    public page would go on publishing ESPN's distorted figure with nothing to say so. The two
    readers must agree about what is on record.

    A malformed file is skipped rather than allowed to take the nightly build down;
    ``validate.py`` is the place that reports it.
    """
    overrides: list[SalaryOverride] = []
    if history_dir.exists():
        for path in sorted(history_dir.glob("*.json")):
            try:
                overrides += [
                    SalaryOverride(**row) for row in _load(path).get("overrides") or []
                ]
            except (ValidationError, TypeError, json.JSONDecodeError):
                continue

    live = manual_dir / "overrides.json"
    if live.exists():
        try:
            for rows in (_load(live).get("seasons") or {}).values():
                overrides += [SalaryOverride(**row) for row in rows]
        except (ValidationError, TypeError, json.JSONDecodeError):
            pass
    return overrides


def season_files(derived_dir: Path) -> tuple[list[int], list[int]]:
    """The seasons with a keeper file and the seasons with a stats file, as year numbers."""
    if not derived_dir.exists():
        return [], []
    keepers: list[int] = []
    stats: list[int] = []
    for path in sorted(derived_dir.glob("*.json")):
        stem = path.stem
        if stem.startswith("."):
            continue
        if stem.endswith("-stats"):
            stem = stem.removesuffix("-stats")
            target = stats
        else:
            target = keepers
        if stem.isdigit():
            target.append(int(stem))
    return sorted(keepers), sorted(stats)


def franchise_names(derived_dir: Path, season: int) -> dict[str, str]:
    """``manager_id`` to franchise name for one season, from that season's own file.

    Keyed per season on purpose. Names change yearly and one of them carries a double space,
    so borrowing another season's names would put the wrong name on a real result. When the
    season has no file the caller falls back to the ``manager_id`` and says so.
    """
    path = derived_dir / f"{season}.json"
    if not path.exists():
        return {}
    try:
        rows = [FranchiseName(**row) for row in _load(path).get("franchises") or []]
    except (ValidationError, TypeError, json.JSONDecodeError):
        return {}
    return {row.manager_id: row.name for row in rows}


def _named(manager_id: str, names: dict[str, str]) -> Named:
    found = names.get(manager_id)
    return Named(manager_id=manager_id, name=found or manager_id, known=found is not None)


# ---------------------------------------------------------------------------
# Keeper season
# ---------------------------------------------------------------------------


def load_claims(season: int, manual_dir: Path = MANUAL) -> list[KeeperClaim]:
    """Recorded keeper claims for one season, from ``data/manual/claims.json``. Read-only.

    The admin tool owns that file. The site reads it and never writes it, exactly as it reads
    ``payouts.json``. A malformed file is skipped rather than allowed to take the nightly build
    down — ``validate.py`` is the place that reports it.
    """
    path = manual_dir / "claims.json"
    if not path.exists():
        return []
    try:
        rows = (_load(path).get("seasons") or {}).get(str(season)) or []
        return [KeeperClaim(**row) for row in rows]
    except (ValidationError, TypeError, json.JSONDecodeError):
        return []


def load_season_settings(season: int, manual_dir: Path = MANUAL) -> Season | None:
    """One season's settings from ``data/manual/seasons.json``. Read-only.

    The admin tool owns that file. The site reads it for the pre-draft home page's Doodle link
    and never writes it. A malformed file is skipped rather than allowed to take the nightly
    build down; ``validate.py`` is the place that reports it.
    """
    path = manual_dir / "seasons.json"
    if not path.exists():
        return None
    try:
        row = (_load(path).get("seasons") or {}).get(str(season))
        return Season(**row) if row else None
    except (ValidationError, TypeError, json.JSONDecodeError):
        return None


def load_dues(season: int, manual_dir: Path = MANUAL) -> list[Dues]:
    """Recorded dues for one season, from ``data/manual/dues.json``. Read-only.

    The admin tool owns that file. The site reads it and never writes it, exactly as it reads
    ``claims.json``. A malformed file is skipped rather than allowed to take the nightly build
    down — ``validate.py`` is the place that reports it.
    """
    path = manual_dir / "dues.json"
    if not path.exists():
        return []
    try:
        rows = _load(path).get("dues") or []
        return [row for row in (Dues(**row) for row in rows) if row.season == season]
    except (ValidationError, TypeError, json.JSONDecodeError):
        return []


def build_dues_board(
    derived_dir: Path, season: int, manual_dir: Path = MANUAL
) -> DuesBoard | None:
    """Every franchise in the season, marked paid or not. ``None`` when the season has none.

    The franchise list comes from the season's own derived file rather than from the dues
    file, so a franchise that has paid nothing still appears — which is the only reason anyone
    reads this. Absence in ``dues.json`` is what unpaid means.
    """
    names = franchise_names(derived_dir, season)
    if not names:
        return None
    paid = {row.manager_id for row in load_dues(season, manual_dir) if row.paid}
    rows = tuple(
        DuesLine(team=_named(manager_id, names), paid=manager_id in paid)
        for manager_id in sorted(names, key=lambda mid: names[mid].strip().lower())
    )
    paid_count = sum(1 for row in rows if row.paid)
    return DuesBoard(
        season=season,
        rows=rows,
        paid_count=paid_count,
        total=len(rows),
        all_paid=paid_count == len(rows),
    )


@dataclass(frozen=True)
class PredraftInfo:
    """What the pre-draft home page shows.

    ``draft_date`` and ``keeper_deadline`` are read straight off the season's derived file —
    ESPN's own ``draftSettings``, synced nightly — so the page can never show a stale hand-typed
    guess (commissioner, 2026-08-26). ``draft_doodle_url`` has no ESPN equivalent and is the one
    piece still commissioner-entered, in ``data/manual/seasons.json``.
    """

    draft_date: datetime | None
    keeper_deadline: datetime | None
    draft_doodle_url: str | None


def _source_time(source: Mapping[str, Any], key: str) -> datetime | None:
    """One ISO datetime out of an already-loaded ``source`` block, or ``None`` if unset."""
    raw = source.get(key)
    return datetime.fromisoformat(raw) if raw else None


def _source_datetime(derived_dir: Path, season: int, key: str) -> datetime | None:
    """One ISO datetime out of ``{season}.json``'s ``source`` block, or ``None`` if unset."""
    path = derived_dir / f"{season}.json"
    if not path.exists():
        return None
    return _source_time(_load(path).get("source") or {}, key)


def _trade_deadline(derived_dir: Path, season: int) -> datetime | None:
    """``season``'s trade deadline, or ``None`` when that season is not on disk."""
    return _source_datetime(derived_dir, season, "trade_deadline")


def load_predraft_info(season: int, derived_dir: Path, manual_dir: Path = MANUAL) -> PredraftInfo:
    """The pre-draft home page's three facts, from their two separate sources."""
    settings = load_season_settings(season, manual_dir)
    return PredraftInfo(
        draft_date=_source_datetime(derived_dir, season, "draft_date"),
        keeper_deadline=_source_datetime(derived_dir, season, "keeper_deadline"),
        draft_doodle_url=settings.draft_doodle_url if settings else None,
    )


def _prospect_state(
    *,
    position: str,
    began: int | None,
    bound: int | None,
    qualifying_season: int,
    acquired_at: datetime,
    deadline: datetime | None,
    already_prospected: bool,
) -> str:
    """All three prospect rules for one player: ``eligible`` | ``ineligible`` | ``unknown``.

    Order matters, and it is "every definite no first". A player already kept as a prospect is
    ineligible whatever ESPN says about his draft class; one who arrived after the deadline
    likewise; a **D/ST is never a person and never prospectable at all**. Only a player nothing
    rules out and nothing confirms is genuinely unknown.

    ``began`` is an exact first season and decides either way. ``bound`` is the earliest season
    his career can have begun and may only rule him **out** — it comes from the statistics log,
    which runs a season late for anyone who was rostered without recording anything, so
    treating it as exact would eventually mark a second-year player eligible. It still settles
    most of what would otherwise be unknown: a bound before the qualifying season means he was
    provably already in the league.
    """
    if position == "DEF":
        # A team defence has no draft class, no rookie season and no way to be a prospect.
        # Asking ESPN about it returns 404 by construction — the question is the wrong one,
        # not the answer missing, so this is a settled "no" rather than an unknown.
        return "ineligible"
    if already_prospected:
        return "ineligible"
    if deadline is not None and acquired_at > deadline:
        return "ineligible"
    if began is not None:
        return "eligible" if began == qualifying_season else "ineligible"
    if bound is not None and bound < qualifying_season:
        return "ineligible"
    return "unknown"


def build_keeper_season(
    derived_dir: Path,
    season: int,
    *,
    overrides: Sequence[SalaryOverride] = (),
    waiver_manager_id: str | None = None,
    waiver_from_season: int | None = None,
    claims: Sequence[KeeperClaim] = (),
    first_nfl_season: Mapping[int, int] | None = None,
    first_season_bounds: Mapping[int, int] | None = None,
    prior_prospect_ids: Collection[int] = (),
    now: datetime | None = None,
) -> KeeperSeason:
    """What every rostered player would cost to keep, who has been declared, and who is eligible.

    Which season is this page about?
    ---------------------------------

    Not always ``season``, and the difference is the same one that decides ``base_salary_field``.
    Before the auction, ``{season}.json`` holds last season's roster carried forward and its
    bases are what a player costs to keep **into** ``season``. After the auction those bases are
    ``keeperValueFuture`` — what he would carry into ``season + 1`` — so the page is already
    about next season's decision whether it says so or not::

        decision_season   = season + 1 if drafted else season
        qualifying_season = decision_season - 1

    ``qualifying_season`` is the most recently completed season, and **every prospect rule keys
    off it**. Read it as a bare ``season - 1`` and the page marks the wrong draft class from the
    moment the auction ends: mid-2026 it is 2026's rookies who are eligible, not 2025's. The
    eligibility mark has to describe the same season as the money beside it, or the page has two
    columns quietly answering different questions.

    What "prospect eligible" means here
    -----------------------------------

    All three rules, because that is what the words say. Rookie (``first_nfl_season``), rostered
    before ``qualifying_season``'s trade deadline, and never kept as a prospect before. Checking
    only the first and still printing "eligible" is the silent-wrong-answer failure this repo is
    built around avoiding.

    Rule 2 is **provisional until that deadline has passed**: mid-season every rostered player
    trivially satisfies a deadline that has not happened yet. It settles on its own — the mark is
    re-derived nightly — but it is not a final answer in week 3.

    Two different numbers live in this function and they must not be confused.

    A **price** is ``base + $5 tax``, straight out of ``keeper_rules.keeper_salary`` with a zero
    fee, and every rostered player has one. The fee is deliberately absent: the tier depends on
    how many keepers a manager declares and the split is the manager's own choice, so there is no
    per-player fee to publish for a player nobody has claimed.

    **Except between the keeper deadline and the auction, when the price IS the base.** In that
    window ESPN's ``keeperValue`` has stopped meaning "carried in from last season" and holds
    the price the commissioner has entered for each keeper, fee and tax already inside it — so
    the tax goes on top of a number that already carries it, and the page publishes every kept
    player $5 dear. See ``charges_in_base`` below.

    A **declared keeper** is one the admin tool has recorded a ``KeeperClaim`` for. He carries the
    fee his manager allocated and the salary recorded at declaration — the figure that manager was
    actually told they owed. That salary is read off the claim rather than recomputed here, so this
    page shows what was agreed instead of what today's roster would price it at; a disagreement
    between the two is the admin tool's to surface, not something to paper over on a public page.
    """
    notes: list[Note] = []
    doc = _load(derived_dir / f"{season}.json")
    source = doc.get("source") or {}

    franchises = _rows(FranchiseName, doc.get("franchises"), notes, f"{season}.json franchises")
    players = _rows(Player, doc.get("players"), notes, f"{season}.json players")
    roster = _rows(RosterEntry, doc.get("roster"), notes, f"{season}.json roster")

    review = doc.get("review") or {}

    # The phase pivot, computed once. Everything below reads these rather than doing the
    # arithmetic again, so no rule can end up judging a different season from its neighbours.
    drafted = bool(source.get("drafted"))

    # Between the keeper deadline and the auction, ESPN's `keeperValue` is the price the
    # commissioner has entered for each keeper — allocated fee and $5 tax already inside it —
    # and NOT the value carried in from last season, which is what it holds the rest of the
    # year. Adding the tax on top of it charges the same $5 twice.
    #
    # This reads the deadline, it does not enforce it: nothing here gates an input or refuses
    # a claim. It decides which of two meanings ESPN's field currently carries. `utc_now` is
    # the only clock allowed to meet a stored deadline, and `keeper_deadline` is naive UTC off
    # ESPN, so both sides of the comparison are UTC.
    #
    # An unrecorded deadline is a distinct state from a past one: a season ESPN has set no
    # deadline for cannot place itself in this window, so the tax applies as usual.
    keeper_deadline = _source_time(source, "keeper_deadline")
    charges_in_base = _charges_in_base(
        drafted, keeper_deadline, now if now is not None else utc_now()
    )

    for warning in review.get("warnings") or []:
        # Transitional, and narrowly scoped: a season synced before the window gate landed still
        # carries the old sentence, and `data/derived/` belongs to the nightly Action. Dropping
        # the structured list below while publishing the prose would name nobody and still claim
        # three disagreements. **Delete once every season file has been re-synced.**
        if charges_in_base and STALE_WAIVER_WARNING in warning:
            continue
        notes.append(Note("review", warning, where=f"{season} keepers"))
    # `review.phase` is deliberately NOT published. Site notes are "unverified" by definition
    # — the class says so and there is no third kind — and a season that has not drafted yet is
    # not something nobody has checked. The page already states the phase where it matters:
    # `charges_in_base` puts a caption above the grid saying the charges are inside the base.
    # Nothing to report in that window: the field the check compared is not an acquisition
    # price there, so a waiver add carrying a fee differs from its own FAAB bid by design.
    mismatches = [] if charges_in_base else (review.get("waiver_base_mismatches") or [])
    if mismatches:
        notes.append(
            Note(
                "review",
                f"{len(mismatches)} waiver pickups have a base ESPN and the transaction log "
                f"disagree about, so their price below is unconfirmed "
                f"(player ids: {', '.join(str(i) for i in mismatches)})",
                where=f"{season} keepers",
            )
        )

    declared = {(claim.manager_id, claim.espn_player_id): claim for claim in claims}

    decision_season = season + 1 if drafted else season
    qualifying_season = decision_season - 1
    origins = first_nfl_season or {}
    bounds = first_season_bounds or {}

    # Rule 2 needs the trade deadline of the season the player was rostered through, which is
    # the qualifying season — never the coming season's, which every prospect would clear.
    deadline = _trade_deadline(derived_dir, qualifying_season)
    if deadline is None:
        notes.append(
            Note(
                "review",
                f"{qualifying_season}'s trade deadline is not on disk, so prospect eligibility "
                f"below reflects the rookie and repeat-claim rules only",
                where=f"{season} keepers",
            )
        )

    by_id = {player.espn_player_id: player for player in players}
    # Needed inside the loop now: a flat row carries its own franchise name.
    names = {row.manager_id: row.name for row in franchises}
    lines: dict[str, list[KeeperLine]] = {}
    for entry in roster:
        player = by_id.get(entry.espn_player_id)
        if player is None:
            # validate.py reports this as an ERROR against the file. Skipping keeps an
            # unnamed row off a public page rather than printing a bare player id.
            notes.append(
                Note(
                    "error",
                    f"roster entry for player {entry.espn_player_id} has no matching player "
                    f"record, so it is not listed",
                    where=f"{season} keepers",
                )
            )
            continue
        base = effective_base_salary(entry, overrides)
        price = (
            base
            if charges_in_base
            else keeper_salary(base, 0, entry.kept_prior_year, KeeperSlot.K1)
        )
        claim = declared.get((entry.manager_id, entry.espn_player_id))
        began = origins.get(entry.espn_player_id)
        lines.setdefault(entry.manager_id, []).append(
            KeeperLine(
                team_name=_named(entry.manager_id, names).name,
                overridden=base != entry.base_salary,
                player_name=player.name,
                position=str(player.position),
                nfl_team=player.nfl_team,
                base=base,
                tax=price - base,
                price=price,
                kept_prior_year=entry.kept_prior_year,
                source=str(entry.source),
                declared=claim is not None,
                slot=str(claim.slot) if claim else "",
                fee=claim.fee_allocated if claim else 0,
                salary=claim.computed_salary if claim else None,
                first_nfl_season=began,
                acquired_at=entry.acquired_at,
                after_deadline=deadline is not None and entry.acquired_at > deadline,
                prospect_state=_prospect_state(
                    position=str(player.position),
                    began=began,
                    bound=bounds.get(entry.espn_player_id),
                    qualifying_season=qualifying_season,
                    acquired_at=entry.acquired_at,
                    deadline=deadline,
                    already_prospected=entry.espn_player_id in prior_prospect_ids,
                ),
            )
        )

    teams: list[TeamKeepers] = []
    for manager_id in sorted(lines, key=lambda mid: (len(mid), mid)):
        # Declared keepers first, then the rest by price. Somebody reading their own team wants
        # what they have committed to above what they merely could commit to.
        team_lines = sorted(
            lines[manager_id],
            key=lambda line: (not line.declared, line.slot, line.price, line.player_name),
        )
        named = _named(manager_id, names)
        claimed = [line for line in team_lines if line.declared]
        teams.append(
            TeamKeepers(
                manager_id=manager_id,
                name=named.name,
                name_known=named.known,
                lines=tuple(team_lines),
                taxed=sum(line.kept_prior_year for line in team_lines),
                fees_waived=manager_id == waiver_manager_id,
                declared_count=len(claimed),
                # Added here, not in a template. A declared line always carries a recorded
                # salary, but `or 0` keeps a half-written claim from crashing the build.
                declared_salary=sum(line.salary or 0 for line in claimed),
                declared_fees=sum(line.fee for line in claimed),
            )
        )

    return KeeperSeason(
        season=season,
        drafted=drafted,
        decision_season=decision_season,
        qualifying_season=qualifying_season,
        qualifying_deadline=deadline,
        charges_in_base=charges_in_base,
        # Dearest first, then by name so equal salaries have a stable order rather than one
        # that depends on which franchise happened to be read first.
        rows=tuple(
            sorted(
                (line for team in teams for line in team.lines),
                key=lambda line: (-line.price, line.player_name),
            )
        ),
        any_eligible=any(
            line.prospect_state == "eligible" for team in teams for line in team.lines
        ),
        base_field=str(source.get("base_salary_field") or "unknown"),
        teams=tuple(teams),
        notes=tuple(notes),
        waiver_manager_id=waiver_manager_id,
        waiver_name=names.get(waiver_manager_id) if waiver_manager_id else None,
        waiver_from_season=waiver_from_season,
    )


# ---------------------------------------------------------------------------
# Stats season
# ---------------------------------------------------------------------------


def build_stats_season(derived_dir: Path, season: int) -> StatsSeason:
    """Standings, prize winners and payouts for one completed season."""
    notes: list[Note] = []
    doc = _load(derived_dir / f"{season}-stats.json")
    source = doc.get("source") or {}
    names = franchise_names(derived_dir, season)
    if not names:
        notes.append(
            Note(
                "review",
                f"data/derived/{season}.json has not been synced, so this season's franchise "
                f"names are unknown and teams are shown by their ESPN team id",
                where=f"{season} stats",
            )
        )

    review = doc.get("review") or {}
    for issue in _rows(ReviewIssue, review.get("issues"), notes, f"{season}-stats.json issues"):
        kind = "error" if issue.severity is Severity.ERROR else "review"
        notes.append(Note(kind, f"[{issue.code}] {issue.message}", where=f"{season} stats"))
    for warning in review.get("warnings") or []:
        notes.append(Note("review", warning, where=f"{season} stats"))

    standings = _rows(StandingRow, doc.get("standings"), notes, f"{season}-stats.json standings")
    points = _rows(SeasonPoints, doc.get("season_points"), notes, f"{season}-stats.json points")
    highs = _rows(WeeklyHigh, doc.get("weekly_high_scores"), notes, f"{season}-stats.json highs")
    studs = _rows(StudAward, doc.get("positional_studs"), notes, f"{season}-stats.json studs")
    survivor = doc.get("survivor") or {}
    eliminations = _rows(
        SurvivorElimination, survivor.get("eliminations"), notes, f"{season}-stats.json survivor"
    )
    payouts = _rows(Payout, doc.get("payouts"), notes, f"{season}-stats.json payouts")

    unlucky_row = doc.get("unlucky")
    unlucky = None
    if unlucky_row:
        loaded = _rows(UnluckyAward, [unlucky_row], notes, f"{season}-stats.json unlucky")
        if loaded:
            award: UnluckyAward = loaded[0]
            unlucky = HighLine(
                week=award.week,
                teams=tuple(_named(mid, names) for mid in award.manager_ids),
                points=award.points,
            )

    # Prizes are grouped by label because a tie splits one prize across several rows, and a
    # prize with no winner still carries its money so the pot reconciles.
    grouped: dict[str, list[PrizeRow]] = {}
    order: list[str] = []
    for payout in payouts:
        if payout.label not in grouped:
            grouped[payout.label] = []
            order.append(payout.label)
        grouped[payout.label].append(
            PrizeRow(
                winner=_named(payout.winner_manager_id, names)
                if payout.winner_manager_id
                else None,
                amount=payout.amount,
            )
        )

    # Money totals are added here rather than in a template. Integer dollars throughout.
    per_manager: dict[str, int] = {}
    for payout in payouts:
        if payout.winner_manager_id:
            per_manager[payout.winner_manager_id] = (
                per_manager.get(payout.winner_manager_id, 0) + payout.amount
            )
    earnings = tuple(
        Earning(team=_named(mid, names), total=total)
        for mid, total in sorted(per_manager.items(), key=lambda kv: (-kv[1], kv[0]))
    )
    unawarded = sum(p.amount for p in payouts if not p.winner_manager_id)

    # A season is final when the bracket has produced a rank, not when the weeks have run out.
    # Reading it off the calendar would call a season settled while its final was still being
    # played, and the placing prizes are exactly the ones that decides.
    weeks_with_results = [int(week) for week in source.get("weeks_with_results") or []]

    return StatsSeason(
        season=season,
        regular_season_weeks=int(source.get("regular_season_weeks") or 0),
        weeks_played=max(weeks_with_results) if weeks_with_results else 0,
        playoff_team_count=int(source.get("playoff_team_count") or 0),
        final=any(row.final_rank for row in standings),
        names_known=bool(names),
        standings=tuple(
            StandingLine(
                team=_named(row.manager_id, names),
                wins=row.wins,
                losses=row.losses,
                ties=row.ties,
                points_for=row.points_for,
                points_against=row.points_against,
                final_rank=row.final_rank,
                playoff_seed=row.playoff_seed,
            )
            for row in sorted(
                standings,
                key=lambda r: (r.final_rank is None, r.final_rank or 0, -r.wins, -r.points_for),
            )
        ),
        season_points=tuple(
            PointsLine(team=_named(row.manager_id, names), points=row.points) for row in points
        ),
        weekly_highs=tuple(
            HighLine(
                week=row.week,
                teams=tuple(_named(mid, names) for mid in row.manager_ids),
                points=row.points,
            )
            for row in highs
        ),
        studs=tuple(
            StudLine(
                position=str(row.position),
                player_name=row.player_name,
                week=row.week,
                points=row.points,
                teams=tuple(_named(mid, names) for mid in row.manager_ids),
            )
            for row in studs
        ),
        survivor_eliminations=tuple(
            HighLine(
                week=row.week,
                teams=tuple(_named(mid, names) for mid in row.manager_ids),
                points=row.points,
            )
            for row in eliminations
        ),
        survivor_winners=tuple(
            _named(mid, names) for mid in survivor.get("winner_manager_ids") or []
        ),
        unlucky=unlucky,
        prizes=tuple(PrizeGroup(label=label, rows=tuple(grouped[label])) for label in order),
        earnings=earnings,
        pot=sum(payout.amount for payout in payouts),
        unawarded=unawarded,
        consolation_winners=tuple(
            _named(mid, names) for mid in review.get("consolation_winner_manager_ids") or []
        ),
        waiver_season=season + 1,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------


def money(amount: int) -> str:
    """Integer dollars, written the way a person writes them.

    Display only, and the only place the site decides what a dollar looks like. Registered as
    a Jinja filter so a template can print money without doing arithmetic on it.
    """
    return f"${amount:,}"


def mdy(when: datetime | None) -> str:
    """A date the way the league writes one: ``8/5/2025``. Display only.

    **Converts to Eastern first, and that is the whole point of it being a function.** Every
    datetime that reaches a template is naive UTC, straight off ESPN. Printing the UTC calendar
    date published the 2026 draft as 9/4 when ESPN says 9/3 at 9pm ET — an evening ET instant
    has already crossed midnight in UTC, so every league date fell a day late. ``to_league_time``
    is the one border between how these are stored and how they are shown.

    Built from the parts rather than with ``strftime``. The no-pad directive that would do this
    in one go is ``%-m`` on Linux and macOS but ``%#m`` on Windows and absent from the C
    standard, so a format string here is a portability bug waiting for whoever next runs the
    site generator somewhere new.

    One filter, so the deadline banner, the Acquired column and the folded phone line cannot
    drift into three formats — which is exactly what they had done before this existed, and now
    so they cannot drift into three timezones either.
    """
    local = to_league_time(when)
    if local is None:
        return ""
    return f"{local.month}/{local.day}/{local.year}"


def _points(value: float) -> str:
    """A fantasy score. Points are the one number here that is not money, so they are the one
    number that may carry decimals."""
    return f"{value:,.2f}"


def _record(row: StandingLine) -> str:
    return f"{row.wins}-{row.losses}-{row.ties}" if row.ties else f"{row.wins}-{row.losses}"


def _playoff_round(week: int, regular_season_weeks: int, playoff_team_count: int) -> str:
    """What to call the playoff week being played.

    The bracket's size is what says how many rounds it takes — six teams take three, four
    take two — so the round cannot be named without it. A season synced before
    ``playoff_team_count`` was recorded, or one whose weeks run past the bracket, falls back
    to a plain ``Playoffs`` rather than naming a round it cannot work out.

    **This assumes one week per round.** ESPN can run a playoff round over two weeks; nothing
    in the derived stats says whether it did, and if it ever does every name after it shifts.
    """
    if playoff_team_count < 2:
        return "Playoffs"

    rounds = math.ceil(math.log2(playoff_team_count))
    index = week - regular_season_weeks
    if not 1 <= index <= rounds:
        return "Playoffs"

    # A bracket that is not a power of two cannot fill its first round, so the top seeds sit
    # it out and the opening week is a play-in — the wild card round, in the NFL's sense this
    # league borrows from. Six teams means two byes. A bracket that *is* a power of two has
    # no byes and no wild card: eight teams open at the quarterfinals.
    if index == 1 and playoff_team_count & (playoff_team_count - 1):
        return "Wild Card"

    # Otherwise the round is named by how far it is from the end, which is what decides how
    # many teams are left in it.
    return {0: "Championship", 1: "Semifinals", 2: "Quarterfinals"}.get(
        rounds - index, f"Round {index}"
    )


def build_home(season: StatsSeason | None) -> Home | None:
    """The prize board for the most recent season with results.

    **The board is driven by what was won, not by what was paid.** Every prize is assembled
    from the derived stats — the studs, the weekly highs, the survivor, the standings — and the
    money is then joined onto it by label. Building it the other way round would leave 2019
    through 2023 with a blank home page, because those seasons have no recorded prize amounts;
    the prizes were still won, the money is just not on record. Those rows show a dash rather
    than ``$0``, which would be a claim that the league paid nothing.

    Any payout label the stats do not explain still reaches the page, in a trailing section.
    A prize that quietly vanished between the engine and the page is exactly the kind of
    silence this project treats as a bug.
    """
    if season is None:
        return None

    groups = {group.label: group for group in season.prizes}
    claimed: set[str] = set()

    def resolve(label: str, fallback: Sequence[Named]) -> tuple[tuple[Named, ...], int, bool]:
        """Winners, money, and whether any money is on record, for one prize label."""
        claimed.add(label)
        group = groups.get(label)
        if group is None:
            return tuple(fallback), 0, False
        # A prize nobody won still has a group and still carries its money, so the winners
        # come back empty and the amount does not.
        return (
            tuple(row.winner for row in group.rows if row.winner),
            sum(row.amount for row in group.rows),
            True,
        )

    def line(
        label: str,
        fallback: Sequence[Named],
        detail: str,
        short_label: str = "",
        value: str = "",
    ) -> BoardRow:
        winners, amount, recorded = resolve(label, fallback)
        return BoardRow(
            label=label,
            winners=winners,
            amount=amount,
            recorded=recorded,
            split=len(winners) > 1,
            detail=detail,
            short_label=short_label,
            value=value,
        )

    standing = {row.team.manager_id: row for row in season.standings}
    by_rank = {row.final_rank: row for row in season.standings if row.final_rank}

    podium: list[PodiumSpot] = []
    for rank, place in ((1, "Champion"), (2, "2nd Place"), (3, "3rd Place")):
        placed = by_rank.get(rank)
        winners, amount, recorded = resolve(place, (placed.team,) if placed else ())
        # Described from the franchise actually shown as the winner, not from the rank — so a
        # payout and the standings disagreeing shows up as a missing record, not a wrong one.
        described = standing.get(winners[0].manager_id) if len(winners) == 1 else None
        podium.append(
            PodiumSpot(
                rank=rank,
                place=place,
                winners=winners,
                amount=amount,
                recorded=recorded,
                detail=f"{_record(described)} · {_points(described.points_for)} points"
                if described
                else "",
            )
        )

    # Most Points leads the season awards. It pays what third place pays, but it is not a
    # placing — the podium is what the playoff bracket decided, and a team can lead the league
    # in points and miss the playoffs entirely. Its size shows in its own amount.
    best = max((row.points for row in season.season_points), default=None)
    leaders_by_points = tuple(row.team for row in season.season_points if row.points == best)
    most_points = line(
        "Most Points (Season)",
        leaders_by_points,
        "",
        value=_points(best) if best is not None else "",
    )

    survived = len(season.survivor_eliminations)
    # Survivor is shown as its own column, so it is built here but not put in a season-awards
    # group. `line` still resolves the label, which is what keeps it out of "Other prizes".
    survivor_row = line(
        "Survivor",
        season.survivor_winners,
        f"last standing after {survived} eliminations" if survived else "last team standing",
    )
    # No ladder means no column to put it in, so the prize stays with the season awards.
    # Dropping it would take its money off the page and the pot would not foot.
    survivor_rows = [] if season.survivor_eliminations else [survivor_row]

    unlucky_rows = (
        [
            line(
                "Unlucky",
                season.unlucky.teams if season.unlucky else (),
                "",
                short_label=f"Week {season.unlucky.week}" if season.unlucky else "",
                value=_points(season.unlucky.points) if season.unlucky else "",
            )
        ]
        if season.unlucky is not None or "Unlucky" in groups
        else []
    )

    # One prize at four positions, so the position alone labels the row — "Stud" is the
    # group's subheading and does not need repeating four times under it.
    studs = [
        line(
            f"{stud.position} Stud",
            stud.teams,
            f"{stud.player_name} · Week {stud.week}",
            short_label=stud.position,
            value=_points(stud.points),
        )
        for stud in season.studs
    ]

    weekly = [
        line(
            f"Week {high.week} High Score",
            high.teams,
            "",
            short_label=f"Week {high.week}",
            value=_points(high.points),
        )
        for high in season.weekly_highs
    ]

    leftover = [
        BoardRow(
            label=label,
            winners=tuple(row.winner for row in group.rows if row.winner),
            amount=sum(row.amount for row in group.rows),
            recorded=True,
            split=len([row for row in group.rows if row.winner]) > 1,
            detail="",
        )
        for label, group in groups.items()
        if label not in claimed
    ]

    # The first column is three separate prizes stacked, each headed like any other block on
    # the board. Blocks with no rows are dropped rather than heading nothing: not every season
    # has every prize.
    columns = tuple(
        filled
        for filled in (
            tuple(block for block in column if block.rows)
            for column in (
                (
                    BoardBlock(
                        "Most Points",
                        (most_points,),
                        caption=f"The most points scored over the regular season, weeks 1–"
                        f"{season.regular_season_weeks}. The playoff weeks do not count "
                        "toward it.",
                        heading_is_the_prize=True,
                    ),
                    # "Stud" names the category; the rows name the positions under it.
                    BoardBlock(
                        "Stud",
                        tuple(studs),
                        caption="The best single week by a started player at that position, "
                        "across the whole season including the playoff weeks — not a season "
                        "total. A player on the bench or on IR does not count, and only QB, "
                        "RB, WR and TE pay a stud.",
                    ),
                    # The week is the row's key here, so the heading is not repeated on it.
                    BoardBlock(
                        "Unlucky",
                        tuple(unlucky_rows),
                        caption="The single highest score that still lost — once for the "
                        "whole season, not once a week. Regular season only, and a tie is "
                        "not a loss.",
                    ),
                    BoardBlock(
                        "Survivor",
                        tuple(survivor_rows),
                        caption=SURVIVOR_RULE,
                        heading_is_the_prize=True,
                    ),
                ),
                (
                    BoardBlock(
                        "Weekly top score",
                        tuple(weekly),
                        caption=f"The top score of the week, weeks 1–"
                        f"{season.regular_season_weeks}. The playoff weeks do not pay a "
                        "weekly high score.",
                    ),
                ),
                (
                    BoardBlock(
                        "Other prizes",
                        tuple(leftover),
                        caption="Recorded as paid, but not explained by this season's "
                        "derived stats.",
                    ),
                ),
            )
        )
        if filled
    )

    # Standard competition ranking: franchises on the same money share a place.
    leaders: list[LeaderLine] = []
    place_number = 0
    previous: int | None = None
    for index, earning in enumerate(season.earnings, start=1):
        if earning.total != previous:
            place_number, previous = index, earning.total
        leaders.append(
            LeaderLine(
                rank=place_number,
                team=earning.team,
                total=earning.total,
            )
        )

    # The same prize the "Survivor" row above carries, retold as its elimination order. It is
    # built from the ladder alone, so a season whose survivor could not be derived shows no
    # panel rather than an empty one.
    survivor_panel = (
        SurvivorPanel(
            winners=survivor_row.winners,
            amount=survivor_row.amount,
            recorded=survivor_row.recorded,
            eliminations=season.survivor_eliminations,
        )
        if season.survivor_eliminations
        else None
    )

    # The season's phase, read off the results themselves. ESPN fills in a final rank only
    # once the season completes, and the regular season's length is where the playoffs start.
    if season.final:
        heading, status = f"{season.season} Final Results", "Final"
    elif not season.weeks_played:
        heading, status = f"{season.season} Preseason", "No results yet"
    else:
        status = f"In progress — through week {season.weeks_played}"
        if not season.regular_season_weeks:
            # Without the regular season's length a playoff week cannot be told from a
            # regular one, so the heading claims neither rather than guessing.
            heading = f"{season.season} Season"
        elif season.weeks_played > season.regular_season_weeks:
            round_name = _playoff_round(
                season.weeks_played, season.regular_season_weeks, season.playoff_team_count
            )
            heading = f"{season.season} {round_name}"
        else:
            heading = f"{season.season} Week {season.weeks_played}"

    return Home(
        season=season.season,
        heading=heading,
        status=status,
        final=season.final,
        money_recorded=bool(season.prizes),
        podium=tuple(podium),
        columns=columns,
        survivor=survivor_panel,
        leaders=tuple(leaders),
        pot=season.pot,
        unawarded=season.unawarded,
        notes=season.notes,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def environment(templates: Path = TEMPLATES) -> Environment:
    """Jinja with autoescaping on. It stays on — the templates never call ``|safe``."""
    env = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    # Formatting money is Python's job. A template that could format it could also compute it.
    env.filters["money"] = money
    env.filters["mdy"] = mdy
    # A datetime on the league's own wall clock. ``mdy`` already converts; this is for the
    # one place a template needs another format of the same instant — the Acquired
    # column's ISO sort key, which has to agree with the date printed beside it.
    env.filters["et"] = to_league_time
    return env


FEE_TIER_ROWS = tuple(
    (count, FEE_TIERS[count]) for count in sorted(FEE_TIERS) if count >= 1
)
"""The published fee table, read off the engine's own tiers rather than retyped."""


def build_site(
    out_dir: Path,
    *,
    derived_dir: Path = DERIVED,
    history_dir: Path = HISTORY,
    manual_dir: Path = MANUAL,
    rules_path: Path = RULES_MD,
    templates: Path = TEMPLATES,
) -> list[Path]:
    """Render every page into ``out_dir``. Returns the files written."""
    env = environment(templates)
    keeper_years, stats_years = season_files(derived_dir)
    overrides = load_overrides(history_dir, manual_dir)

    # The most recent keeper file is the season a manager is deciding about.
    current_year = keeper_years[-1] if keeper_years else None

    # Last season's consolation winner has *this* season's fees waived — read it off by one
    # year and the waiver lands on the wrong team. Nothing records it yet, so it is derived
    # and rendered as such, never as settled.
    waiver_id: str | None = None
    waiver_from: int | None = None
    if current_year is not None and (current_year - 1) in stats_years:
        prior = build_stats_season(derived_dir, current_year - 1)
        if len(prior.consolation_winners) == 1:
            waiver_id = prior.consolation_winners[0].manager_id
            waiver_from = prior.season

    current = (
        build_keeper_season(
            derived_dir,
            current_year,
            overrides=overrides,
            waiver_manager_id=waiver_id,
            waiver_from_season=waiver_from,
            claims=load_claims(current_year, manual_dir),
            first_nfl_season=load_player_origins(derived_dir),
            first_season_bounds=load_first_season_bounds(derived_dir),
            # Both records of who has already been kept as a prospect: the frozen seasons and
            # the live claims file. A player in either is out, whatever his draft class says.
            prior_prospect_ids=(
                HistoryStore(data_dir=history_dir.parent).prospect_ids(current_year)
                | {
                    claim.espn_player_id
                    for year in range(current_year - 6, current_year)
                    for claim in load_claims(year, manual_dir)
                    if claim.slot is KeeperSlot.PROSPECT
                }
            ),
        )
        if current_year is not None
        else None
    )
    seasons = [build_stats_season(derived_dir, year) for year in sorted(stats_years, reverse=True)]

    # The home page is the most recent season with results — the one being played, or the one
    # just finished. Everything older is a link.
    home = build_home(
        seasons[0] if seasons else None,
    )

    # Before the auction, `current` is the season being drafted and there is nothing to award
    # yet — showing last season's finished board there would read as this season's result.
    # `predraft` carries the settings the pre-draft home page shows instead; it is None once
    # `current.drafted` flips, at which point the page above reverts to `home` on its own.
    predraft = (
        load_predraft_info(current.season, derived_dir, manual_dir)
        if current is not None and not current.drafted
        else None
    )

    # Dues are about the season being played, so this rides on `current` rather than on the
    # most recent *finished* season — it shows on the pre-draft home page and the results one
    # alike. It removes itself once every franchise has paid: a panel of twelve ticks tells a
    # reader nothing and would sit there until January. Decided here rather than in the
    # template so the board itself stays an honest record of who has paid.
    board = (
        build_dues_board(derived_dir, current.season, manual_dir)
        if current is not None
        else None
    )
    dues = board if board is not None and not board.all_paid else None

    shared = {
        "current": current,
        "seasons": seasons,
        "home": home,
        "fee_tiers": FEE_TIER_ROWS,
        "keeper_tax": KEEPER_TAX,
        "max_keepers": MAX_KEEPERS,
        "max_prospects": MAX_PROSPECTS,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, template: str, **context: Any) -> None:
        path = out_dir / name
        path.write_text(
            env.get_template(template).render(**{**shared, **context}), encoding="utf-8"
        )
        written.append(path)

    write("index.html", "index.html", page="home", predraft=predraft, dues=dues)
    write("keepers.html", "keepers.html", page="keepers")
    write("seasons.html", "seasons.html", page="seasons")
    # A past season's page is the home page, archived: same template, same prize board, just
    # that year's data instead of the most recent one. `home` above is `build_home(seasons[0])`;
    # this is that same function run over every other year.
    for season in seasons:
        write(f"season-{season.season}.html", "index.html", page="seasons", home=build_home(season))
    write(
        "rules.html",
        "rules.html",
        page="rules",
        rules=render_markdown(rules_path.read_text(encoding="utf-8"))
        if rules_path.exists()
        else Markup(""),
    )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the public site from data/.")
    parser.add_argument(
        "--preview",
        action="store_true",
        help="write .preview/ instead of site/. Use this locally — the Action owns site/",
    )
    parser.add_argument(
        "--derived",
        type=Path,
        help="read seasons from this directory instead of data/derived/ (requires --preview)",
    )
    parser.add_argument(
        "--clean", action="store_true", help="delete the output directory before rendering"
    )
    args = parser.parse_args(argv)

    if args.derived and not args.preview:
        # Reading from somewhere else and writing the committed site/ is how a laptop render
        # of half-synced data ends up published.
        print("--derived is for previews; pass --preview too", file=sys.stderr)
        return 2

    out_dir = PREVIEW if args.preview else SITE
    if args.clean and out_dir.exists():
        shutil.rmtree(out_dir)

    written = build_site(out_dir, derived_dir=args.derived or DERIVED)
    for path in written:
        print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    print(f"{len(written)} pages into {out_dir}")
    if not args.preview:
        print("site/ is the Action's to commit — do not commit a local render")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
