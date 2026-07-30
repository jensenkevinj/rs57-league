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
import re
import shutil
import sys
from dataclasses import dataclass
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
    effective_base_salary,
    keeper_salary,
)
from rs57.models import (
    Base,
    FranchiseName,
    KeeperClaim,
    KeeperSlot,
    Payout,
    Player,
    RosterEntry,
    SalaryOverride,
    SeasonPoints,
    StandingRow,
    StudAward,
    SurvivorElimination,
    UnluckyAward,
    WeeklyHigh,
)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DERIVED = DATA / "derived"
HISTORY = DATA / "history"
MANUAL = DATA / "manual"
RULES_MD = ROOT / "docs" / "rules.md"
TEMPLATES = Path(__file__).resolve().parent / "templates"
SITE = ROOT / "site"
PREVIEW = ROOT / ".preview"


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
    any_declared: bool = False
    """Whether any team has recorded a claim. Until one has, the page says so in as many words."""


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


def load_overrides(history_dir: Path = HISTORY) -> list[SalaryOverride]:
    """Salary overrides recorded in ``data/history/``, read-only.

    Empty until the Phase 5 backfill writes it. Read anyway: an un-reverted draft-cash
    override is the difference between the published price and the true one, and the day
    those rows appear the site should already be using them.
    """
    overrides: list[SalaryOverride] = []
    if not history_dir.exists():
        return overrides
    for path in sorted(history_dir.glob("*.json")):
        try:
            overrides += [SalaryOverride(**row) for row in _load(path).get("overrides") or []]
        except (ValidationError, TypeError, json.JSONDecodeError):
            # validate.py is the place that reports a malformed history file. The site
            # renders without it rather than failing the nightly build.
            continue
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


def build_keeper_season(
    derived_dir: Path,
    season: int,
    *,
    overrides: Sequence[SalaryOverride] = (),
    waiver_manager_id: str | None = None,
    waiver_from_season: int | None = None,
    claims: Sequence[KeeperClaim] = (),
) -> KeeperSeason:
    """What every rostered player would cost to keep in ``season``, and who has been declared.

    Two different numbers live in this function and they must not be confused.

    A **price** is ``base + $5 tax``, straight out of ``keeper_rules.keeper_salary`` with a zero
    fee, and every rostered player has one. The fee is deliberately absent: the tier depends on
    how many keepers a manager declares and the split is the manager's own choice, so there is no
    per-player fee to publish for a player nobody has claimed.

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
    for warning in review.get("warnings") or []:
        notes.append(Note("review", warning, where=f"{season} keepers"))
    mismatches = review.get("waiver_base_mismatches") or []
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

    by_id = {player.espn_player_id: player for player in players}
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
        price = keeper_salary(base, 0, entry.kept_prior_year, KeeperSlot.K1)
        claim = declared.get((entry.manager_id, entry.espn_player_id))
        lines.setdefault(entry.manager_id, []).append(
            KeeperLine(
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
            )
        )

    names = {row.manager_id: row.name for row in franchises}
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
        drafted=bool(source.get("drafted")),
        base_field=str(source.get("base_salary_field") or "unknown"),
        teams=tuple(teams),
        notes=tuple(notes),
        waiver_manager_id=waiver_manager_id,
        waiver_name=names.get(waiver_manager_id) if waiver_manager_id else None,
        waiver_from_season=waiver_from_season,
        any_declared=any(team.declared_count for team in teams),
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

    return StatsSeason(
        season=season,
        regular_season_weeks=int(source.get("regular_season_weeks") or 0),
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
# Rendering
# ---------------------------------------------------------------------------


def environment(templates: Path = TEMPLATES) -> Environment:
    """Jinja with autoescaping on. It stays on — the templates never call ``|safe``."""
    return Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(default_for_string=True, default=True),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


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
    overrides = load_overrides(history_dir)

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
        )
        if current_year is not None
        else None
    )
    seasons = [build_stats_season(derived_dir, year) for year in sorted(stats_years, reverse=True)]

    shared = {
        "current": current,
        "seasons": seasons,
        "fee_tiers": FEE_TIER_ROWS,
        "keeper_tax": KEEPER_TAX,
        "max_keepers": MAX_KEEPERS,
        "max_prospects": MAX_PROSPECTS,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, template: str, **context: Any) -> None:
        path = out_dir / name
        path.write_text(env.get_template(template).render(**shared, **context), encoding="utf-8")
        written.append(path)

    write("index.html", "index.html", page="home")
    write("keepers.html", "keepers.html", page="keepers")
    write("seasons.html", "seasons.html", page="seasons")
    for season in seasons:
        write(f"season-{season.season}.html", "season.html", page="seasons", season=season)
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
