"""The site generator, and the rules it is not allowed to break.

Three of these tests exist because getting them wrong publishes something. The site is a
public GitHub Pages site off a public repo: a REVIEW item rendered as though it had been
checked, a manual text field rendered as markup, or a salary recomputed in a template are
each permanent once they ship.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

import pytest

from rs57.espn import SyncedScoring
from rs57.keeper_rules import KEEPER_TAX, keeper_salary
from rs57.models import KeeperSlot
from rs57.stats import SeasonStats
from rs57.stats_sync import stats_document
from rs57.site import (
    TEMPLATES,
    build_home,
    build_keeper_season,
    build_site,
    build_stats_season,
    environment,
    render_markdown,
    season_files,
)

SEASON = 2026
PRIOR = 2025


def keeper_doc(**overrides):
    """One derived keeper season, in the shape ``rs57.sync`` writes."""
    doc = {
        "season": SEASON,
        "source": {
            "drafted": False,
            "base_salary_field": "keeperValue",
            "trade_deadline": "2026-12-02T17:00:00",
        },
        "franchises": [
            {"manager_id": "t1", "season": SEASON, "name": "Fake News"},
            # The double space is real and has already leaked into the spreadsheets.
            {"manager_id": "t2", "season": SEASON, "name": "Belichick's  Spy"},
        ],
        "players": [
            {"espn_player_id": 1, "name": "Puka Nacua", "position": "WR", "nfl_team": "LAR"},
            {"espn_player_id": 2, "name": "James Cook III", "position": "RB", "nfl_team": "BUF"},
        ],
        "roster": [
            {
                "season": SEASON,
                "manager_id": "t1",
                "espn_player_id": 1,
                "acquired_at": "2025-08-05T12:00:00",
                "base_salary": 5,
                "kept_prior_year": True,
                "source": "draft",
            },
            {
                "season": SEASON,
                "manager_id": "t2",
                "espn_player_id": 2,
                "acquired_at": "2025-08-05T12:00:00",
                "base_salary": 42,
                "kept_prior_year": False,
                "source": "draft",
            },
        ],
        "review": {
            "waiver_bases_verified": 80,
            "waiver_base_mismatches": [],
            "warnings": [],
        },
    }
    doc.update(overrides)
    return doc


def stats_doc(**overrides):
    """One derived stats season, in the shape ``rs57.stats_sync`` writes."""
    doc = {
        "season": PRIOR,
        "source": {"regular_season_weeks": 14, "weeks_with_results": list(range(1, 18))},
        "standings": [
            {
                "season": PRIOR,
                "manager_id": "t1",
                "wins": 10,
                "losses": 4,
                "ties": 0,
                "points_for": 1737.5,
                "points_against": 1417.12,
                "final_rank": 1,
                "playoff_seed": 1,
            }
        ],
        "weekly_high_scores": [
            {"season": PRIOR, "week": 1, "manager_ids": ["t1"], "points": 129.62}
        ],
        "season_points": [{"season": PRIOR, "manager_id": "t1", "points": 1737.5}],
        "positional_studs": [
            {
                "season": PRIOR,
                "position": "QB",
                "espn_player_id": 9,
                "player_name": "Josh Allen",
                "week": 11,
                "points": 42.68,
                "manager_ids": ["t1"],
            }
        ],
        "survivor": {"eliminations": [], "winner_manager_ids": ["t1"]},
        "unlucky": {"season": PRIOR, "week": 14, "manager_ids": ["t2"], "points": 127.86},
        "payouts": [
            {
                "season": PRIOR,
                "label": "Champion",
                "amount": 500,
                "winner_manager_id": "t1",
                "paid": False,
            }
        ],
        "review": {
            "consolation_winner_manager_ids": ["t2"],
            "warnings": [],
            "issues": [],
        },
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def derived(tmp_path: Path) -> Path:
    """A ``data/derived/`` holding one keeper season and one stats season."""
    out = tmp_path / "derived"
    out.mkdir()
    (out / f"{SEASON}.json").write_text(json.dumps(keeper_doc()), encoding="utf-8")
    # The prior season carries its OWN deadline. Inheriting 2026's was unrealistic and made
    # every prospect deadline check pass for free — 2026's deadline is in December 2026, which
    # every player on a 2025 roster clears.
    prior = keeper_doc(season=PRIOR, roster=[], players=[])
    prior["source"]["trade_deadline"] = "2025-11-26T17:00:00"
    (out / f"{PRIOR}.json").write_text(json.dumps(prior), encoding="utf-8")
    (out / f"{PRIOR}-stats.json").write_text(json.dumps(stats_doc()), encoding="utf-8")
    return out


def render(tmp_path: Path, derived: Path) -> dict[str, str]:
    out = tmp_path / "out"
    build_site(out, derived_dir=derived, history_dir=tmp_path / "nohistory")
    return {path.name: path.read_text(encoding="utf-8") for path in out.iterdir()}


def text(html: str) -> str:
    """The page with its tags removed, for asserting on what a reader actually sees.

    ``<style>`` and ``<script>`` bodies are dropped first. Stripping tags alone leaves their
    *contents* behind, so a CSS comment or a line of JavaScript counted as page text — which
    fails an assertion for the wrong reason and, worse, would let one pass for the wrong
    reason. A reader sees neither.
    """
    stripped = re.sub(r"<(style|script)\b.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", stripped))


# ---------------------------------------------------------------------------
# Escaping — the site is public and manual text is an injection path
# ---------------------------------------------------------------------------


def test_no_template_uses_the_safe_filter():
    """``|safe`` on a manual text field is how commissioner-typed prose becomes markup."""
    for template in sorted(TEMPLATES.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b", source), f"{template.name} uses the safe filter"
        assert "autoescape" not in source.replace("Autoescaping", ""), (
            f"{template.name} mentions autoescape — it is on globally and stays on"
        )


def test_autoescaping_is_on():
    assert environment().autoescape is not False
    rendered = environment().from_string("{{ value }}").render(value="<script>x</script>")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_franchise_name_is_escaped_not_executed(tmp_path: Path):
    """A franchise name comes from ESPN. It is data, and it renders as data."""
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = keeper_doc()
    doc["franchises"][0]["name"] = "<script>alert(1)</script>"
    (derived / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    pages = render(tmp_path, derived)
    assert "<script>alert(1)</script>" not in pages["keepers.html"]
    assert "&lt;script&gt;" in pages["keepers.html"]


def test_markdown_escapes_before_it_adds_tags():
    """The rules page is built from Markdown, and the escaping happens first."""
    rendered = str(render_markdown("# Head\n\nA <script>alert(1)</script> line\n\n- **b** `c` *d*"))
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    # The supported subset still works.
    assert "<h2>Head</h2>" in rendered
    assert "<strong>b</strong>" in rendered
    assert "<code>c</code>" in rendered
    assert "<em>d</em>" in rendered


def test_markdown_bold_is_not_read_as_two_emphases():
    rendered = str(render_markdown("**bold** and *plain*"))
    assert "<strong>bold</strong>" in rendered
    assert "<em>plain</em>" in rendered
    assert "<em></em>" not in rendered


def test_markdown_handles_hard_wrapped_prose():
    """The rules file is hard-wrapped, so bullets and bold runs span lines."""
    rendered = str(
        render_markdown(
            "- **Unlucky** — the highest score that still\n"
            "  lost its matchup, once a season.\n"
            "- **Survivor** — last team standing."
        )
    )
    assert rendered.count("<li>") == 2, "a wrapped bullet started a second list item"
    assert "still lost its matchup" in rendered

    wrapped_bold = str(render_markdown("the **single highest score that\nstill lost**, once."))
    assert "<strong>single highest score that still lost</strong>" in wrapped_bold
    assert "*" not in wrapped_bold


def test_markdown_leaves_no_stray_asterisks_in_the_rules_page():
    """The rules file is prose someone will keep editing; unrendered syntax is a visible bug."""
    from rs57.site import RULES_MD

    rendered = str(render_markdown(RULES_MD.read_text(encoding="utf-8")))
    assert "*" not in rendered


def test_markdown_does_not_pass_raw_html_through():
    assert "<img" not in str(render_markdown('<img src=x onerror="alert(1)">'))


# ---------------------------------------------------------------------------
# REVIEW must never render as though it had been checked
# ---------------------------------------------------------------------------


def test_review_issue_renders_as_unverified(tmp_path: Path):
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = stats_doc()
    doc["review"]["issues"] = [
        {
            "code": "tie_split",
            "severity": "review",
            "message": "Week 6 High Score is a 2-way tie",
            "manager_id": None,
            "week": 6,
            "position": None,
        }
    ]
    (derived / f"{PRIOR}-stats.json").write_text(json.dumps(doc), encoding="utf-8")
    (derived / f"{PRIOR}.json").write_text(
        json.dumps(keeper_doc(season=PRIOR)), encoding="utf-8"
    )

    page = render(tmp_path, derived)[f"season-{PRIOR}.html"]
    assert "Week 6 High Score is a 2-way tie" in page
    body = text(page).lower()
    assert "unverified" in body
    assert "nobody has checked this" in body


def test_error_issue_renders_too_and_is_not_silently_dropped(tmp_path: Path):
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = stats_doc()
    doc["review"]["issues"] = [
        {
            "code": "missing_week",
            "severity": "error",
            "message": "no scores recorded for week 3",
            "manager_id": None,
            "week": 3,
            "position": None,
        }
    ]
    (derived / f"{PRIOR}-stats.json").write_text(json.dumps(doc), encoding="utf-8")
    page = render(tmp_path, derived)[f"season-{PRIOR}.html"]
    assert "no scores recorded for week 3" in page


def test_a_sync_warning_stays_off_the_public_page(tmp_path: Path):
    """A REVIEW is the commissioner's to clear, and the admin tool is where he clears it.

    Publishing it asks twelve managers to check something none of them can act on. It is not
    dropped: ``admin.screens`` raises every ``review.warnings`` entry on the team screen, and
    ``test_admin.test_a_sync_warning_reaches_the_commissioner`` is the assertion that it does.
    Delete that test and this one becomes a warning that reaches nobody.
    """
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = keeper_doc()
    doc["review"]["warnings"] = ["2026 has not been drafted, so base_salary is keeperValue"]
    (derived / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    page = render(tmp_path, derived)["keepers.html"]
    assert "has not been drafted" not in page
    assert "unverified" not in text(page).lower()


def test_an_error_still_reaches_the_public_page(tmp_path: Path):
    """An ERROR is a row missing from the grid, and only this flag says the grid is short.

    The page filters its notes down to errors. Filtering them down to nothing would look
    identical on today's data, which has no errors — so this drops a roster entry whose player
    record is absent and insists the reader is told.
    """
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = keeper_doc()
    doc["players"] = [row for row in doc["players"] if row["espn_player_id"] != 2]
    (derived / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    body = text(render(tmp_path, derived)["keepers.html"]).lower()
    assert "no matching player record" in body
    assert "error" in body


def test_the_derived_consolation_waiver_is_not_published_at_all(tmp_path: Path, derived: Path):
    """Nothing records the consolation winner, so the page must not hint that anything does.

    It used to publish the derivation under an "unverified" flag. Half the league read the flag
    as the answer, so the whole question moved to the admin tool's settings screen, where the
    commissioner records the winner and the waiver becomes a fact rather than a guess.
    """
    body = text(render(tmp_path, derived)["keepers.html"]).lower()
    for hint in ("waive", "waived", "consolation", "commissioner", "unverified"):
        assert hint not in body, f"the public keeper page still mentions {hint!r}"


# ---------------------------------------------------------------------------
# The grid: one flat table, sorted and filtered in the browser
# ---------------------------------------------------------------------------


def grid_rows(page: str) -> list[list[str]]:
    """Every ``<tbody>`` row of the keeper grid, as a list of stripped cell texts."""
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    assert body, "the keeper page has no table body"
    return [
        [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
         for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S)
    ]


def test_the_grid_is_one_row_per_rostered_player(tmp_path: Path, derived: Path):
    """Twelve tables became one, so a franchise is a column and never a heading row.

    A heading row carries no franchise for the row below it once the table is re-sorted, which
    is what the browser does the moment anyone clicks a column.
    """
    season = build_keeper_season(derived, SEASON)
    rows = grid_rows(render(tmp_path, derived)["keepers.html"])

    assert len(rows) == sum(len(team.lines) for team in season.teams)
    assert all(len(row) == 9 for row in rows), f"a row is not nine cells: {rows}"
    # ``Belichick's  Spy`` carries a real double space. It survives into the HTML; only this
    # test's own whitespace-collapsing needs undoing, so collapse the expected names too.
    named = {re.sub(r"\s+", " ", team.name) for team in season.teams}
    for row in rows:
        assert row[2] in named, f"row {row} carries no franchise of its own"


def test_the_grid_does_not_publish_declarations(tmp_path: Path, derived: Path):
    page = render(tmp_path, derived)["keepers.html"]
    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    assert [re.sub(r"<[^>]+>", "", cell).strip()
            for cell in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)] == [
        "Player", "Pos", "Team", "NFL", "Prospect", "Acquired", "Base", "Tax", "Salary"
    ]


def test_the_whole_grid_is_served_without_javascript(tmp_path: Path, derived: Path):
    """Sorting and filtering are an enhancement. The rows are HTML the server wrote.

    If the script ever became the thing that builds the table, a browser that blocks it — or a
    syntax error in one line of it — would serve a league of empty rosters and look fine doing it.
    """
    page = render(tmp_path, derived)["keepers.html"]
    served = page[: page.index("<script>")]
    assert "Puka Nacua" in served and "James Cook III" in served
    assert len(grid_rows(served)) == len(grid_rows(page))


def test_the_sort_reads_money_from_an_attribute_not_from_the_dollars(
    tmp_path: Path, derived: Path
):
    """``data-v`` is why the script never parses "$12" back into a number.

    A parser in the script would be a second implementation of what a dollar is, and it would
    sort $100 below $9 the first time somebody wrote one that split on the wrong character.
    """
    season = build_keeper_season(derived, SEASON)
    lines = {line.player_name: line for line in
             [line for team in season.teams for line in team.lines]}
    page = render(tmp_path, derived)["keepers.html"]

    row = next(row for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)
               if "Puka Nacua" in row)
    nacua = lines["Puka Nacua"]
    for amount in (nacua.base, nacua.tax, nacua.price):
        assert f'data-v="{amount}"' in row, f"${amount} is rendered with no sortable value"


def test_the_player_cell_keeps_a_sort_key_apart_from_what_it_displays(
    tmp_path: Path, derived: Path
):
    """Position, NFL club and franchise fold under the name, inside the same cell.

    The cell then reads "Puka Nacua WR LAR Fake News", and a sort that took the cell's text
    would order the league by name-then-position-then-franchise while still calling itself
    Player. ``data-k`` is the name on its own, and the script prefers it where a cell has one.
    """
    page = render(tmp_path, derived)["keepers.html"]
    row = next(row for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)
               if "Puka Nacua" in row)
    cell = re.search(r"<td[^>]*data-k=\"([^\"]*)\"[^>]*>(.*?)</td>", row, re.S)

    assert cell, "the player cell carries no sort key"
    assert cell.group(1) == "Puka Nacua", "the sort key is not the name on its own"
    folded = cell.group(2)
    for detail in ("WR", "LAR", "Fake News"):
        assert detail in folded, (
            f"{detail!r} is not folded into the player cell, and its own column is hidden — "
            f"so it appears nowhere on the page"
        )


def test_a_franchise_name_is_escaped_inside_the_title_attribute(tmp_path: Path):
    """The franchise name is now in an attribute as well as in text — a second escape path."""
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = keeper_doc()
    doc["franchises"][0]["name"] = '" onmouseover="alert(1)'
    (derived / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    page = render(tmp_path, derived)["keepers.html"]
    assert 'onmouseover="alert(1)"' not in page
    assert "&#34;" in page or "&quot;" in page


# ---------------------------------------------------------------------------
# Money comes from the engine, not from a template
# ---------------------------------------------------------------------------


def test_keeper_price_matches_the_engine(tmp_path: Path, derived: Path):
    season = build_keeper_season(derived, SEASON)
    lines = {line.player_name: line for line in
             [line for team in season.teams for line in team.lines]}

    nacua = lines["Puka Nacua"]
    assert nacua.price == keeper_salary(5, 0, True, KeeperSlot.K1) == 5 + KEEPER_TAX
    cook = lines["James Cook III"]
    assert cook.price == keeper_salary(42, 0, False, KeeperSlot.K1) == 42

    page = render(tmp_path, derived)["keepers.html"]
    assert f"${nacua.price}" in page


def test_the_fee_is_not_baked_into_a_published_price(tmp_path: Path, derived: Path):
    """No claim exists, so no fee is owed by anyone in particular. The page prices base+tax."""
    season = build_keeper_season(derived, SEASON)
    for team in season.teams:
        for line in team.lines:
            assert line.price == line.base + line.tax
            assert line.tax in (0, KEEPER_TAX)


def test_no_template_does_arithmetic_on_money():
    """A Jinja expression computing a salary is a second, untested copy of the rules."""
    for template in sorted(TEMPLATES.glob("*.html")):
        for expression in re.findall(r"\{\{(.*?)\}\}", template.read_text(encoding="utf-8")):
            assert not re.search(r"[-+*/]\s*\d", expression), (
                f"{template.name} computes {expression.strip()!r} in the template"
            )


def test_page_never_implies_a_keeper_has_been_declared(tmp_path: Path, derived: Path):
    page = text(render(tmp_path, derived)["keepers.html"]).lower()
    assert "nobody has declared a keeper yet" in page


# ---------------------------------------------------------------------------
# Franchise names, and the fact that nothing else identifies a manager
# ---------------------------------------------------------------------------


def test_names_are_read_per_season_and_never_borrowed(tmp_path: Path):
    """A season with no keeper file shows team ids, not another season's names."""
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / f"{PRIOR}-stats.json").write_text(json.dumps(stats_doc()), encoding="utf-8")
    (derived / f"{SEASON}.json").write_text(json.dumps(keeper_doc()), encoding="utf-8")

    season = build_stats_season(derived, PRIOR)
    assert not season.names_known
    assert all(not row.team.known for row in season.standings)

    page = render(tmp_path, derived)[f"season-{PRIOR}.html"]
    # 2026's names must not appear on a 2025 page.
    assert "Fake News" not in page
    assert "t1" in page
    assert "name unknown" in page


def test_franchise_names_come_from_the_matching_season(tmp_path: Path, derived: Path):
    season = build_stats_season(derived, PRIOR)
    assert season.names_known
    assert season.standings[0].team.name == "Fake News"


def test_double_spaced_franchise_name_is_not_used_as_a_key(tmp_path: Path, derived: Path):
    """Names are display only. The keys are ``t{espn_team_id}``."""
    season = build_keeper_season(derived, SEASON)
    assert {team.manager_id for team in season.teams} == {"t1", "t2"}
    assert any(team.name == "Belichick's  Spy" for team in season.teams)


def test_nothing_published_looks_like_a_person_or_an_email(tmp_path: Path, derived: Path):
    for name, page in render(tmp_path, derived).items():
        assert not re.search(r"[\w.%+-]+@[\w.-]+\.\w{2,}", page), f"{name} holds an email"
        for forbidden in ("firstName", "lastName", "data/private", "owners"):
            assert forbidden not in page, f"{name} mentions {forbidden}"


# ---------------------------------------------------------------------------
# Prizes: ties, unawarded money, and seasons with no payouts
# ---------------------------------------------------------------------------


def test_a_tie_renders_one_row_per_winner(tmp_path: Path):
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = stats_doc()
    doc["payouts"] = [
        {"season": PRIOR, "label": "Week 6 High Score", "amount": 5,
         "winner_manager_id": "t1", "paid": False},
        {"season": PRIOR, "label": "Week 6 High Score", "amount": 5,
         "winner_manager_id": "t2", "paid": False},
    ]
    (derived / f"{PRIOR}-stats.json").write_text(json.dumps(doc), encoding="utf-8")
    (derived / f"{PRIOR}.json").write_text(json.dumps(keeper_doc(season=PRIOR)), encoding="utf-8")

    season = build_stats_season(derived, PRIOR)
    group = next(g for g in season.prizes if g.label == "Week 6 High Score")
    assert len(group.rows) == 2
    assert season.pot == 10
    assert {e.total for e in season.earnings} == {5}


def test_an_unawarded_prize_keeps_its_money_and_says_so(tmp_path: Path):
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = stats_doc()
    doc["payouts"] = [
        {"season": PRIOR, "label": "Survivor", "amount": 40,
         "winner_manager_id": None, "paid": False}
    ]
    (derived / f"{PRIOR}-stats.json").write_text(json.dumps(doc), encoding="utf-8")
    (derived / f"{PRIOR}.json").write_text(json.dumps(keeper_doc(season=PRIOR)), encoding="utf-8")

    season = build_stats_season(derived, PRIOR)
    assert season.pot == 40
    assert season.unawarded == 40
    assert season.earnings == ()

    page = render(tmp_path, derived)[f"season-{PRIOR}.html"]
    assert "unawarded" in text(page).lower()


def test_a_season_with_stats_and_no_payouts_still_renders(tmp_path: Path):
    """2023 is deliberately absent from payouts.json; its stats still compute."""
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = stats_doc(season=2023, payouts=[])
    (derived / "2023-stats.json").write_text(json.dumps(doc), encoding="utf-8")

    pages = render(tmp_path, derived)
    assert "season-2023.html" in pages
    assert "No prize amounts recorded" in pages["season-2023.html"]


def test_franchise_earnings_add_up_to_the_pot(tmp_path: Path, derived: Path):
    season = build_stats_season(derived, PRIOR)
    assert sum(e.total for e in season.earnings) + season.unawarded == season.pot


# ---------------------------------------------------------------------------
# The home page — the prize board
# ---------------------------------------------------------------------------


def one_stats_season(tmp_path: Path, doc: dict) -> Path:
    """A ``derived/`` holding one stats season and the keeper file its names come from."""
    out = tmp_path / "derived"
    out.mkdir(exist_ok=True)
    (out / f"{PRIOR}-stats.json").write_text(json.dumps(doc), encoding="utf-8")
    (out / f"{PRIOR}.json").write_text(json.dumps(keeper_doc(season=PRIOR)), encoding="utf-8")
    return out


def board_labels(home) -> set[str]:
    """Every prize the home page actually shows, placings included."""
    return {spot.place for spot in home.podium} | {
        row.label for column in home.columns for block in column for row in block.rows
    }


def test_home_is_the_most_recent_season_with_results(tmp_path: Path, derived: Path):
    page = render(tmp_path, derived)["index.html"]
    assert f"{PRIOR} Final Results" in text(page)


def test_no_prize_disappears_between_the_payouts_and_the_board(tmp_path: Path):
    """A payout the derived stats cannot explain still reaches the page.

    The board is assembled from what was won and the money is joined on by label, so a label
    the stats do not produce has nowhere to land unless the trailing section catches it.
    Dropping that section must fail here — a prize that silently vanished is the failure this
    project keeps guarding against.
    """
    doc = stats_doc()
    doc["payouts"] = doc["payouts"] + [
        {
            "season": PRIOR,
            "label": "Toilet Bowl",
            "amount": 15,
            "winner_manager_id": "t2",
            "paid": False,
        }
    ]
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert {payout["label"] for payout in doc["payouts"]} <= board_labels(home)
    assert "Toilet Bowl" in board_labels(home)
    assert home.pot == 515

    page = render(tmp_path, derived)["index.html"]
    assert "Toilet Bowl" in page


def test_a_season_with_no_recorded_money_shows_no_amount_rather_than_zero(tmp_path: Path):
    """2019 through 2023 have no recorded prize amounts. The prizes were still won.

    ``$0`` would be a claim that the league paid nothing that season, which is a different
    statement from having no record of what it paid.
    """
    doc = stats_doc(payouts=[])
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert not home.money_recorded
    assert home.pot == 0
    rows = [row for column in home.columns for block in column for row in block.rows]
    assert rows, "the board went blank when the money did"
    assert not any(row.recorded for row in rows)
    assert any(row.winners for row in rows), "the prizes were still won"

    page = render(tmp_path, derived)["index.html"]
    assert "$0" not in page


def test_an_unfinished_season_is_not_presented_as_settled(tmp_path: Path):
    """Mid-season the nightly still derives a stats file. Nothing in it is decided yet."""
    doc = stats_doc()
    doc["source"]["weeks_with_results"] = list(range(1, 10))
    doc["standings"][0]["final_rank"] = None
    doc["standings"][0]["playoff_seed"] = None
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert not home.final
    assert home.status == "In progress — through week 9"
    assert home.heading == f"{PRIOR} Week 9"

    body = text(render(tmp_path, derived)["index.html"])
    assert "In progress — through week 9" in body
    # Not in the heading, not in a pill, not anywhere: nothing here is settled.
    assert "Final" not in body


@pytest.mark.parametrize(
    ("weeks", "final", "expected"),
    [
        ([], False, f"{PRIOR} Preseason"),
        ([1], False, f"{PRIOR} Week 1"),
        # Week 14 is the last regular-season week, so it is not yet the playoffs.
        (list(range(1, 15)), False, f"{PRIOR} Week 14"),
        (list(range(1, 16)), False, f"{PRIOR} Playoffs"),
        (list(range(1, 18)), True, f"{PRIOR} Final Results"),
    ],
)
def test_the_heading_names_the_phase_of_the_season(
    tmp_path: Path, weeks: list[int], final: bool, expected: str
):
    """The header walks preseason → regular season → playoffs → final across the year.

    The boundary is the one worth pinning: ``regular_season_weeks`` is 14 here, so week 14 is
    still the regular season and week 15 is the playoffs. Off by one and the page announces
    the playoffs while the regular season is still being played.
    """
    doc = stats_doc()
    doc["source"]["weeks_with_results"] = weeks
    if not final:
        doc["standings"][0]["final_rank"] = None
        doc["standings"][0]["playoff_seed"] = None
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert home.heading == expected
    assert expected in text(render(tmp_path, derived)["index.html"])


@pytest.mark.parametrize(
    ("teams", "week", "expected"),
    [
        # Six teams take three rounds: two byes, then a semifinal, then the final. Six is not
        # a power of two, so the opening week is a play-in.
        (6, 15, f"{PRIOR} Wild Card"),
        (6, 16, f"{PRIOR} Semifinals"),
        (6, 17, f"{PRIOR} Championship"),
        # Eight fills its bracket, so there are no byes and no wild card round at all.
        (8, 15, f"{PRIOR} Quarterfinals"),
        (8, 16, f"{PRIOR} Semifinals"),
        (8, 17, f"{PRIOR} Championship"),
        # Four teams take two, so the first playoff week is already the semifinal.
        (4, 15, f"{PRIOR} Semifinals"),
        (4, 16, f"{PRIOR} Championship"),
        # A week past the bracket names no round rather than inventing one.
        (4, 17, f"{PRIOR} Playoffs"),
        # Seasons synced before playoff_team_count was recorded carry 0.
        (0, 16, f"{PRIOR} Playoffs"),
    ],
)
def test_the_playoff_round_is_named_from_the_bracket(
    tmp_path: Path, teams: int, week: int, expected: str
):
    """The bracket's size is what says how many rounds it takes.

    Hardcoding three rounds would be right today and wrong the moment the league changes its
    playoff team count — the same shape of mistake as hardcoding a 14-week regular season.
    """
    doc = stats_doc()
    doc["source"]["playoff_team_count"] = teams
    doc["source"]["weeks_with_results"] = list(range(1, week + 1))
    doc["standings"][0]["final_rank"] = None
    doc["standings"][0]["playoff_seed"] = None
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert home.heading == expected
    assert expected in text(render(tmp_path, derived)["index.html"])


def test_the_sync_writes_the_bracket_size_the_site_reads(tmp_path: Path):
    """The two ends of the same field, checked against each other.

    ``build_home`` can only name a playoff round if ``stats_sync`` writes the bracket size
    out. If the writer drops it the site does not break — it quietly says "Playoffs" forever,
    which is the kind of silence this project treats as a bug, so it is asserted here.
    """
    scoring = SyncedScoring(
        season=PRIOR,
        regular_season_weeks=14,
        playoff_team_count=6,
        scores=(),
        matchups=(),
        player_weeks=(),
    )
    stats = SeasonStats(
        season=PRIOR,
        regular_season_weeks=tuple(range(1, 15)),
        standings=(),
        weekly_highs=(),
        season_points=(),
        studs=(),
        survivor_eliminations=(),
        survivor_winner_ids=(),
        unlucky=None,
        consolation_winner_ids=(),
        issues=(),
    )

    doc = json.loads(json.dumps(stats_document(stats, scoring, payouts=[], issues=[]), default=str))
    assert doc["source"]["playoff_team_count"] == 6

    # And the site reads it back off exactly that key.
    derived = one_stats_season(tmp_path, doc)
    assert build_stats_season(derived, PRIOR).playoff_team_count == 6


def test_a_phase_is_not_guessed_when_the_season_length_is_unknown(tmp_path: Path):
    """Without ``regular_season_weeks`` a playoff week cannot be told from a regular one.

    Guessing here would announce the playoffs on the strength of a missing field.
    """
    doc = stats_doc()
    doc["source"]["regular_season_weeks"] = 0
    doc["source"]["weeks_with_results"] = list(range(1, 16))
    doc["standings"][0]["final_rank"] = None
    doc["standings"][0]["playoff_seed"] = None
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert home.heading == f"{PRIOR} Season"
    assert "Playoffs" not in text(render(tmp_path, derived)["index.html"])


def test_every_season_prize_explains_itself(tmp_path: Path, derived: Path):
    """The rules moved off the page and behind an "i", so nothing shows them by default.

    A prize whose note went empty would render an unexplained row and look completely
    normal — the rule would simply have stopped being told anywhere.
    """
    home = build_home(build_stats_season(derived, PRIOR))

    page = render(tmp_path, derived)["index.html"]
    for column in home.columns:
        for block in column:
            assert block.caption, f"{block.title} has no rule behind its i"
            assert block.caption in page, f"{block.title}'s rule never reached the page"


def test_what_won_each_prize_survives_the_column_layout(tmp_path: Path, derived: Path):
    """Every row carries the number that won it and the context for that number.

    They render in two different cells — the number on the right, its context under the
    winner — and either could be dropped from the row shape while the page still looked
    finished. The prize would just no longer say what won it.
    """
    home = build_home(build_stats_season(derived, PRIOR))
    rows = [row for column in home.columns for block in column for row in block.rows]
    assert any(row.value for row in rows), "no prize has a number, so this checks nothing"
    assert any(row.detail for row in rows), "no prize has context, so this checks nothing"

    page = render(tmp_path, derived)["index.html"]
    for row in rows:
        if row.value:
            assert row.value in page, f"{row.label} no longer shows the number that won it"
        if row.detail:
            assert row.detail in page, f"{row.label} lost the context for its number"


def _block(home, title: str):
    """The board block with this heading, wherever on the board it sits."""
    return next(b for column in home.columns for b in column if b.title == title)


def _blocks(home) -> list[str]:
    return [b.title for column in home.columns for b in column]


def _money_shown(home) -> int:
    """Every dollar the home page puts on screen, wherever it puts it."""
    return (
        sum(row.amount for column in home.columns for block in column for row in block.rows)
        + sum(spot.amount for spot in home.podium)
        + (home.survivor.amount if home.survivor else 0)
    )


def test_most_points_leads_the_season_awards_and_is_not_a_placing(tmp_path: Path, derived: Path):
    """It pays what third place pays, but the podium is what the playoff bracket decided.

    A team can lead the league in points and miss the playoffs entirely, so putting it in the
    top row would make it read as a fourth place. Like Survivor it has two possible homes and
    must occupy exactly one: in both, the pot is over by its own amount; in neither, the money
    leaves the page.
    """
    home = build_home(build_stats_season(derived, PRIOR))

    assert [spot.rank for spot in home.podium] == [1, 2, 3], "the podium is placings only"
    assert not [spot for spot in home.podium if "Points" in spot.place]

    points = _block(home, "Most Points")
    assert _blocks(home)[0] == "Most Points", "it leads the first column"
    rows = [row for column in home.columns for block in column for row in block.rows]
    assert len([row for row in rows if row.label == "Most Points (Season)"]) == 1
    assert _money_shown(home) == home.pot

    # Its regular-season window has to stay stated somewhere, and that is its heading's "i".
    page = render(tmp_path, derived)["index.html"]
    assert points.caption in page
    assert "weeks 1–14" in points.caption


def test_a_prize_a_season_never_awarded_leaves_no_empty_subheading(tmp_path: Path):
    """A group with nothing in it would render its subheading over nothing at all.

    Not every season has every prize — 2019 through 2022 have no recorded money, and a season
    can finish with no Unlucky award. The subheading has to go with the rows.
    """
    doc = stats_doc()
    doc["unlucky"] = None
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert all(
        block.rows for column in home.columns for block in column
    ), "a heading with no prizes under it"
    assert "Unlucky" not in _blocks(home)

    assert "Unlucky" not in text(render(tmp_path, derived)["index.html"])


def test_a_column_only_says_each_when_its_prizes_really_are_equal(tmp_path: Path):
    """"$10 each" is a claim about every row under it, so it has to be true of every row.

    A column that says it wrongly also stops printing the individual amounts, so the figures
    it is misreporting are no longer on the page to contradict it.
    """
    doc = stats_doc()
    doc["weekly_high_scores"] = [
        {"season": PRIOR, "week": 1, "manager_ids": ["t1"], "points": 129.62},
        {"season": PRIOR, "week": 2, "manager_ids": ["t2"], "points": 126.00},
    ]
    # Survivor moves to its own column, so the season awards are exactly the three below —
    # every one of them recorded, so it is the amounts differing that has to do the work here.
    doc["survivor"] = {
        "eliminations": [{"season": PRIOR, "week": 1, "manager_ids": ["t2"], "points": 43.62}],
        "winner_manager_ids": ["t1"],
    }
    # Two studs paying different amounts: the group is recorded throughout, so it is the
    # amounts differing — not a missing one — that has to stop it claiming a shared figure.
    doc["positional_studs"] = [
        dict(doc["positional_studs"][0]),
        {
            "season": PRIOR,
            "position": "RB",
            "espn_player_id": 10,
            "player_name": "Jahmyr Gibbs",
            "week": 12,
            "points": 49.90,
            "manager_ids": ["t2"],
        },
    ]
    doc["payouts"] += [
        {"season": PRIOR, "label": "Week 1 High Score", "amount": 10, "winner_manager_id": "t1", "paid": False},
        {"season": PRIOR, "label": "Week 2 High Score", "amount": 10, "winner_manager_id": "t2", "paid": False},
        {"season": PRIOR, "label": "Most Points (Season)", "amount": 100, "winner_manager_id": "t1", "paid": False},
        {"season": PRIOR, "label": "Unlucky", "amount": 20, "winner_manager_id": "t2", "paid": False},
        {"season": PRIOR, "label": "QB Stud", "amount": 25, "winner_manager_id": "t1", "paid": False},
        {"season": PRIOR, "label": "RB Stud", "amount": 30, "winner_manager_id": "t2", "paid": False},
        {"season": PRIOR, "label": "Survivor", "amount": 40, "winner_manager_id": "t1", "paid": False},
    ]
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))

    # Every weekly prize pays $10, so that block says it once.
    weekly = _block(home, "Weekly top score")
    assert weekly.each_recorded and weekly.each == 10

    # The two studs pay $25 and $30, so theirs says nothing.
    studs = _block(home, "Stud")
    assert all(row.recorded for row in studs.rows), "the recorded check must not be what fires"
    assert {row.amount for row in studs.rows} == {25, 30}
    assert not studs.each_recorded and studs.each == 0

    # So each of those rows still prints its own amount.
    page = render(tmp_path, derived)["index.html"]
    assert "$25" in page and "$30" in page
    assert _money_shown(home) == home.pot


def test_survivor_is_shown_once_and_paid_once(tmp_path: Path):
    """Survivor is its own column, so it is NOT also a row in the season awards.

    It has two possible homes and has to occupy exactly one. Shown in both, the pot would be
    over by $40; shown in neither, the money would silently leave the page. Both failures look
    completely normal on screen, so the arithmetic is what catches them.
    """
    doc = stats_doc()
    doc["survivor"] = {
        "eliminations": [
            {"season": PRIOR, "week": 1, "manager_ids": ["t2"], "points": 43.62},
            {"season": PRIOR, "week": 2, "manager_ids": ["t3"], "points": 61.10},
        ],
        "winner_manager_ids": ["t1"],
    }
    doc["payouts"].append(
        {
            "season": PRIOR,
            "label": "Survivor",
            "amount": 40,
            "winner_manager_id": "t1",
            "paid": False,
        }
    )
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert home.survivor is not None
    assert len(home.survivor.eliminations) == 2

    # The column is the prize, so it carries the money and there is no row beside the others.
    assert home.survivor.amount == 40 and home.survivor.recorded
    rows = [row for column in home.columns for block in column for row in block.rows]
    assert not [row for row in rows if row.label == "Survivor"]

    assert _money_shown(home) == home.pot, "the page shows money the pot does not account for"

    body = text(render(tmp_path, derived)["index.html"])
    assert "Winner" in body
    assert "$40" in body
    for elimination in home.survivor.eliminations:
        assert f"Week {elimination.week}" in body


def test_a_season_with_no_survivor_ladder_keeps_the_prize_in_the_season_awards(
    tmp_path: Path, derived: Path
):
    """With no ladder there is no column to hold the prize, so it stays a row.

    The fixture has a Survivor winner but no eliminations. Dropping the row here because the
    column normally takes it would take the prize off the page entirely.
    """
    home = build_home(build_stats_season(derived, PRIOR))
    assert home.survivor is None

    rows = [row for column in home.columns for block in column for row in block.rows]
    assert len([row for row in rows if row.label == "Survivor"]) == 1
    assert _money_shown(home) == home.pot

    body = text(render(tmp_path, derived)["index.html"])
    assert "Survivor" in body
    assert "Winner" not in body


def test_a_finished_season_says_so(tmp_path: Path, derived: Path):
    home = build_home(build_stats_season(derived, PRIOR))
    assert home.final and home.status == "Final"
    assert home.heading == f"{PRIOR} Final Results"
    body = text(render(tmp_path, derived)["index.html"])
    # The heading carries this now — a finished season shows no pill at all.
    assert f"{PRIOR} Final Results" in body
    assert "In progress" not in body


def test_a_tie_on_the_board_shows_both_winners_and_the_whole_prize(tmp_path: Path):
    """The split is what each winner took; the board's ``Pays`` column is the prize itself."""
    doc = stats_doc()
    doc["weekly_high_scores"] = [
        {"season": PRIOR, "week": 1, "manager_ids": ["t1", "t2"], "points": 129.62}
    ]
    doc["payouts"] = [
        {"season": PRIOR, "label": "Week 1 High Score", "amount": 5,
         "winner_manager_id": "t1", "paid": False},
        {"season": PRIOR, "label": "Week 1 High Score", "amount": 5,
         "winner_manager_id": "t2", "paid": False},
    ]
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    row = next(
        row
        for column in home.columns
        for block in column
        for row in block.rows
        if row.label == "Week 1 High Score"
    )
    assert row.split and row.amount == 10 and len(row.winners) == 2

    page = render(tmp_path, derived)["index.html"]
    assert "split" in page
    # Both winners are named, and the double space in the second one survives verbatim.
    assert "Fake News" in page
    assert "Belichick&#39;s  Spy" in page


def test_the_home_leaderboard_and_the_unawarded_money_add_up_to_the_pot(
    tmp_path: Path, derived: Path
):
    home = build_home(build_stats_season(derived, PRIOR))
    assert sum(line.total for line in home.leaders) + home.unawarded == home.pot


def test_franchises_on_the_same_money_share_a_place(tmp_path: Path):
    doc = stats_doc()
    doc["payouts"] = [
        {"season": PRIOR, "label": "Champion", "amount": 100,
         "winner_manager_id": "t1", "paid": False},
        {"season": PRIOR, "label": "Survivor", "amount": 100,
         "winner_manager_id": "t2", "paid": False},
    ]
    derived = one_stats_season(tmp_path, doc)

    home = build_home(build_stats_season(derived, PRIOR))
    assert [line.rank for line in home.leaders] == [1, 1]
    assert {line.total for line in home.leaders} == {100}

    # Neither is shown as second on a board whose ordering is the only thing saying who led.
    body = text(render(tmp_path, derived)["index.html"])
    assert " 2 " not in body.split("Moneylist")[1][:200]


def test_the_home_page_survives_a_season_with_no_stats_at_all(tmp_path: Path):
    """Before the first stats sync there is no board. The page still renders and says so."""
    derived = tmp_path / "derived"
    derived.mkdir()
    (derived / f"{SEASON}.json").write_text(json.dumps(keeper_doc()), encoding="utf-8")

    assert build_home(None) is None
    body = text(render(tmp_path, derived)["index.html"])
    assert "no prizes to show" in body


# ---------------------------------------------------------------------------
# Writers, inputs, and the empty case
# ---------------------------------------------------------------------------


def test_preview_writes_the_scratch_dir_and_never_site(monkeypatch, tmp_path: Path, derived: Path):
    """``site/`` belongs to the Action. A laptop render goes to the gitignored ``.preview/``."""
    from rs57 import site as site_module

    site_dir = tmp_path / "site"
    preview_dir = tmp_path / ".preview"
    monkeypatch.setattr(site_module, "SITE", site_dir)
    monkeypatch.setattr(site_module, "PREVIEW", preview_dir)

    assert site_module.main(["--preview", "--derived", str(derived)]) == 0
    assert preview_dir.exists()
    assert not site_dir.exists()


def test_derived_without_preview_is_refused(tmp_path: Path, derived: Path):
    """Rendering the committed site/ from a hand-picked input directory is how half-synced
    data gets published."""
    from rs57 import site as site_module

    assert site_module.main(["--derived", str(derived)]) == 2


def test_generator_imports_no_espn_and_no_network():
    source = (Path(__file__).resolve().parent.parent / "rs57" / "site.py").read_text()
    for forbidden in ("rs57.espn", "urllib", "requests", "http.client", "socket"):
        assert forbidden not in source, f"site.py reaches for {forbidden}"


def test_empty_derived_directory_still_produces_a_site(tmp_path: Path):
    """The repo ships an empty data/derived/. The build must not crash before the first sync."""
    derived = tmp_path / "derived"
    derived.mkdir()
    pages = render(tmp_path, derived)
    assert "index.html" in pages and "keepers.html" in pages and "rules.html" in pages
    assert "No season has been synced yet" in pages["keepers.html"]


def test_season_files_ignores_stats_suffix_correctly(tmp_path: Path, derived: Path):
    keepers, stats = season_files(derived)
    assert keepers == [PRIOR, SEASON]
    assert stats == [PRIOR]


def test_the_rules_page_is_built_from_the_repo_markdown(tmp_path: Path, derived: Path):
    page = render(tmp_path, derived)["rules.html"]
    body = text(page)
    assert "keeper" in body.lower()
    assert f"${KEEPER_TAX}" in body


# ---------------------------------------------------------------------------
# Prospect eligibility, and the season the page is actually about
# ---------------------------------------------------------------------------


def origins_doc(seasons: dict[int, int]) -> str:
    return json.dumps(
        {
            "players": [
                {"espn_player_id": pid, "first_nfl_season": began, "source": "draft_year"}
                for pid, began in seasons.items()
            ],
            "unresolved": [],
            "review": {"warnings": []},
        }
    )


def test_a_rookie_from_last_season_is_prospect_eligible(tmp_path: Path, derived: Path):
    """Player 1 began in 2025 and the 2026 file has not drafted, so 2025 is the qualifying season."""
    (derived / "player-origins.json").write_text(origins_doc({1: PRIOR}), encoding="utf-8")
    season = build_keeper_season(derived, SEASON, first_nfl_season={1: PRIOR})

    lines = {line.player_name: line for team in season.teams for line in team.lines}
    assert lines["Puka Nacua"].prospect_state == "eligible"
    assert season.any_eligible

    body = text(render(tmp_path, derived)["keepers.html"])
    assert "eligible" in body.lower()


def test_a_veteran_is_marked_not_eligible_rather_than_left_blank(derived: Path):
    """"Not eligible" is a checked answer. It must be distinguishable from "we do not know"."""
    season = build_keeper_season(derived, SEASON, first_nfl_season={1: PRIOR - 4, 2: PRIOR})
    lines = {line.player_name: line for team in season.teams for line in team.lines}
    assert lines["Puka Nacua"].prospect_state == "ineligible"
    assert lines["James Cook III"].prospect_state == "eligible"


def test_an_unknown_draft_class_says_unknown_on_the_page(tmp_path: Path, derived: Path):
    """Absence of a mark would read as a quiet no. It gets its own words instead."""
    season = build_keeper_season(derived, SEASON, first_nfl_season={})
    assert all(
        line.prospect_state == "unknown" for team in season.teams for line in team.lines
    )
    assert not season.any_eligible

    (derived / "player-origins.json").write_text(origins_doc({}), encoding="utf-8")
    page = render(tmp_path, derived)["keepers.html"]
    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    assert "Unknown" in row, "an unresolved player must say so in his own row"


def test_a_player_kept_as_a_prospect_before_is_never_eligible_again(derived: Path):
    """Rule 3 beats the draft class: a definite no outranks anything ESPN says."""
    season = build_keeper_season(
        derived, SEASON, first_nfl_season={1: PRIOR}, prior_prospect_ids={1}
    )
    lines = {line.player_name: line for team in season.teams for line in team.lines}
    assert lines["Puka Nacua"].prospect_state == "ineligible"


def test_a_player_acquired_after_the_deadline_is_not_eligible(tmp_path: Path):
    """Rule 2, against the QUALIFYING season's deadline — not the coming season's.

    The 2026 file's own deadline is in December 2026, which every player on a 2025 roster
    clears. Reading that one would mark the whole league eligible.
    """
    out = tmp_path / "derived"
    out.mkdir()
    doc = keeper_doc()
    doc["roster"][0]["acquired_at"] = "2025-12-19T12:00:00"  # after 2025's deadline
    (out / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")
    prior = keeper_doc(season=PRIOR, roster=[], players=[])
    prior["source"]["trade_deadline"] = "2025-11-26T17:00:00"
    (out / f"{PRIOR}.json").write_text(json.dumps(prior), encoding="utf-8")

    season = build_keeper_season(out, SEASON, first_nfl_season={1: PRIOR, 2: PRIOR})
    lines = {line.player_name: line for team in season.teams for line in team.lines}
    assert lines["Puka Nacua"].prospect_state == "ineligible", "acquired after the deadline"
    assert lines["James Cook III"].prospect_state == "eligible"


def test_a_drafted_season_marks_next_years_rookies(tmp_path: Path):
    """The phase pivot. Once a season has drafted, the page is about the NEXT keep decision.

    Before the auction the bases are what a player costs to keep into ``season``; after it they
    are ``keeperValueFuture``, which is what he carries into ``season + 1``. The eligibility
    mark has to move with the money, or the page marks the wrong draft class from the day the
    auction ends — mid-2026 it is 2026's rookies who are eligible, not 2025's.
    """
    out = tmp_path / "derived"
    out.mkdir()
    doc = keeper_doc(season=PRIOR)
    doc["source"]["drafted"] = True
    doc["source"]["base_salary_field"] = "keeperValueFuture"
    doc["source"]["trade_deadline"] = "2025-11-26T17:00:00"
    for row in doc["roster"]:
        row["season"] = PRIOR
    (out / f"{PRIOR}.json").write_text(json.dumps(doc), encoding="utf-8")

    season = build_keeper_season(out, PRIOR, first_nfl_season={1: PRIOR, 2: PRIOR - 1})

    assert season.drafted
    assert season.decision_season == PRIOR + 1, "a drafted season decides the following year"
    lines = {line.player_name: line for team in season.teams for line in team.lines}
    # Player 1 began in 2025 — the qualifying season for a 2026 decision made mid-2025.
    assert lines["Puka Nacua"].prospect_state == "eligible"
    # Player 2 began in 2024, a rookie for the 2025 decision but not this one.
    assert lines["James Cook III"].prospect_state == "ineligible"


def test_both_phases_agree_about_the_same_decision(tmp_path: Path, derived: Path):
    """2025 (drafted) and 2026 (not) both decide the 2026 keeps, so both name the same season.

    Two routes to one answer is what shows the pivot is right rather than merely present.
    """
    undrafted = build_keeper_season(derived, SEASON, first_nfl_season={})
    assert undrafted.decision_season == SEASON

    out = tmp_path / "d2"
    out.mkdir()
    doc = keeper_doc(season=PRIOR)
    doc["source"]["drafted"] = True
    for row in doc["roster"]:
        row["season"] = PRIOR
    (out / f"{PRIOR}.json").write_text(json.dumps(doc), encoding="utf-8")
    drafted = build_keeper_season(out, PRIOR, first_nfl_season={})

    assert drafted.decision_season == undrafted.decision_season == SEASON


def test_the_title_names_the_season_being_decided(tmp_path: Path):
    """Post-auction the file is named 2025 but the page is about 2026. The tab must say so."""
    out = tmp_path / "derived"
    out.mkdir()
    doc = keeper_doc(season=PRIOR)
    doc["source"]["drafted"] = True
    for row in doc["roster"]:
        row["season"] = PRIOR
    (out / f"{PRIOR}.json").write_text(json.dumps(doc), encoding="utf-8")

    page = render(tmp_path, out)["keepers.html"]
    assert f"<title>RS57 — {SEASON} Keepers</title>" in page
    assert f"<title>RS57 — {PRIOR} Keepers</title>" not in page


def test_the_prospect_mark_does_not_pollute_the_player_sort_key(tmp_path: Path, derived: Path):
    """The tag rides in the Player cell, and ``data-k`` is still the bare name."""
    (derived / "player-origins.json").write_text(origins_doc({1: PRIOR}), encoding="utf-8")
    page = render(tmp_path, derived)["keepers.html"]
    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    cell = re.search(r"<td[^>]*data-k=\"([^\"]*)\"", row)
    assert cell.group(1) == "Puka Nacua"


def test_no_review_note_reaches_the_public_page_from_eligibility(tmp_path: Path, derived: Path):
    """The unknown state is carried per row, never as a page-level unverified banner."""
    body = text(render(tmp_path, derived)["keepers.html"]).lower()
    assert "unverified" not in body
    assert "nobody has checked" not in body


def test_a_defence_is_never_marked_unknown(tmp_path: Path):
    """A D/ST has no draft class because the question does not apply, not because it is missing.

    ESPN 404s on every negative id by construction, so before this they all rendered "draft
    class unknown" — twelve rows telling the reader to go and check something uncheckable, and
    unprospectable anyway. A settled no, not an open question.
    """
    out = tmp_path / "derived"
    out.mkdir()
    doc = keeper_doc()
    doc["players"].append(
        {"espn_player_id": -16033, "name": "Ravens D/ST", "position": "DEF", "nfl_team": "BAL"}
    )
    doc["roster"].append({**doc["roster"][0], "espn_player_id": -16033})
    (out / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    season = build_keeper_season(out, SEASON, first_nfl_season={})
    lines = {line.player_name: line for team in season.teams for line in team.lines}
    assert lines["Ravens D/ST"].prospect_state == "ineligible"

    # Scoped to the D/ST's own row — the other fixture players genuinely are unknown here,
    # and asserting over the whole page would pass or fail for the wrong reason.
    page = render(tmp_path, out)["keepers.html"]
    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Ravens D/ST" in r)
    assert "Unknown" not in row
    assert "Eligible" not in row


def test_a_bound_before_the_qualifying_season_rules_a_player_out(derived: Path):
    """An undrafted veteran ESPN states nothing about is still provably not a rookie.

    The statistics log says he was recording numbers in 2022. It can only run late, so his
    true first season is 2022 or earlier — either way, not 2025.
    """
    season = build_keeper_season(
        derived, SEASON, first_nfl_season={}, first_season_bounds={1: 2022, 2: 2022}
    )
    assert all(
        line.prospect_state == "ineligible" for team in season.teams for line in team.lines
    )


def test_a_bound_can_never_prove_a_player_is_a_rookie(derived: Path):
    """The asymmetry, and the reason the two mappings are kept apart.

    A bound equal to the qualifying season is exactly the ambiguous case: he may be a genuine
    rookie, or he may have spent the prior season on a roster recording nothing, which is how
    the statistics log comes out a season late. Unknown is the honest answer; "eligible" would
    eventually hand somebody a second-year player at a prospect price.
    """
    season = build_keeper_season(
        derived, SEASON, first_nfl_season={}, first_season_bounds={1: PRIOR, 2: PRIOR}
    )
    states = {line.prospect_state for team in season.teams for line in team.lines}
    assert states == {"unknown"}, "a bound was allowed to confer eligibility"

    # The same year from an exact source does confer it.
    exact = build_keeper_season(derived, SEASON, first_nfl_season={1: PRIOR, 2: PRIOR})
    assert {line.prospect_state for team in exact.teams for line in team.lines} == {"eligible"}


# ---------------------------------------------------------------------------
# Acquisition dates, the deadline reference, and the late marking
# ---------------------------------------------------------------------------


def test_the_deadline_is_stated_above_the_grid(tmp_path: Path, derived: Path):
    """The Acquired column is measured against a date, so the page says which date.

    Asking a reader to hold it in their head down 188 rows is how a marked row becomes
    a mystery rather than a warning.
    """
    body = text(render(tmp_path, derived)["keepers.html"])
    assert "11/26/2025" in body, "the qualifying season's keeper deadline is not on the page"
    assert "keeper deadline" in body.lower()


def test_a_missing_deadline_says_so_rather_than_printing_nothing(tmp_path: Path):
    """No line at all would read as "there is no deadline", which is a different claim."""
    out = tmp_path / "derived"
    out.mkdir()
    (out / f"{SEASON}.json").write_text(json.dumps(keeper_doc()), encoding="utf-8")

    body = text(render(tmp_path, out)["keepers.html"])
    assert "not on file" in body
    assert "no row is marked against it" in body


def test_a_player_acquired_after_the_deadline_is_marked_on_the_row(tmp_path: Path):
    """The fact lives on the date it is a fact about, and the row carries an edge to find it."""
    out = tmp_path / "derived"
    out.mkdir()
    doc = keeper_doc()
    doc["roster"][0]["acquired_at"] = "2025-12-19T12:00:00"  # after 2025's deadline
    (out / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")
    prior = keeper_doc(season=PRIOR, roster=[], players=[])
    prior["source"]["trade_deadline"] = "2025-11-26T17:00:00"
    (out / f"{PRIOR}.json").write_text(json.dumps(prior), encoding="utf-8")

    season = build_keeper_season(out, SEASON)
    lines = {line.player_name: line for team in season.teams for line in team.lines}
    assert lines["Puka Nacua"].after_deadline is True
    assert lines["James Cook III"].after_deadline is False

    page = render(tmp_path, out)["keepers.html"]
    late = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    ontime = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "James Cook" in r)
    assert 'class="late' in late or ' late"' in late or "late" in late.split(">")[0]
    assert "late-date" in late, "the date itself is not marked"
    assert "late-date" not in ontime, "an on-time acquisition was marked"


def test_the_acquired_column_sorts_chronologically(tmp_path: Path, derived: Path):
    """ISO in ``data-k`` so a text sort is a date sort, and the script parses no dates.

    Same reasoning as money sorting off ``data-v``: a date parser in the script would be a
    second implementation of what a date is, and "02 Sep 2025" sorts before "08 Oct 2025"
    alphabetically only by luck.
    """
    page = render(tmp_path, derived)["keepers.html"]
    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    cell = re.search(r'<td class="acq" data-k="([^"]+)"', row)
    assert cell, "the acquired cell carries no sort key"
    assert cell.group(1) == "2025-08-05", "the sort key is not an ISO date"
    assert "8/5/2025" in row, "the displayed date is not the page's format"


def test_prospect_status_is_a_column_of_its_own(tmp_path: Path, derived: Path):
    """Hidden, and read only by the checkbox — but still a column, with clean words in it.

    Both players get a first season, so the two states under test are *eligible* and *not
    eligible*. Leave player 2 out and he is **unknown**, which is a third state and would make
    this test pass for the wrong reason.
    """
    (derived / "player-origins.json").write_text(
        origins_doc({1: PRIOR, 2: PRIOR - 3}), encoding="utf-8"
    )
    page = render(tmp_path, derived)["keepers.html"]

    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    headers = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)]
    assert headers.index("Prospect") == 4, "the prospect state is not where the filter reads it"
    assert headers.index("Acquired") == 5

    eligible = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    veteran = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "James Cook" in r)
    assert '<td class="pstate">Eligible</td>' in eligible
    # The badge is what a reader sees; the hidden cell still carries the word the filter matches.
    assert '<td class="pstate">Not eligible</td>' in veteran


def test_the_folded_row_details_are_present_for_the_narrow_layout(tmp_path: Path, derived: Path):
    """A phone drops the Acquired, Prospect and Tax columns and folds the first two into the
    row. They have to be *in* the row for CSS to reveal them — six columns do not fit 375px."""
    page = render(tmp_path, derived)["keepers.html"]
    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    assert "acq-sub" in row, "the date is not folded into the row for the narrow layout"
    assert "franchise" in row, "the franchise is not the element that gives way when clipped"


def test_the_search_box_matches_player_names_and_nothing_else(tmp_path: Path, derived: Path):
    """It says "player name", so that is what it searches.

    Franchise and position have dropdowns of their own; leaving them in the box meant typing a
    franchise silently did a different job from picking it, and searching the whole row would
    make "5" match every price on the page.
    """
    page = render(tmp_path, derived)["keepers.html"]
    script = page[page.index("<script>"):]

    assert 'placeholder="Filter by player name"' in page
    assert "cellText(row, PLAYER).toLowerCase()" in script, "the haystack is not the name alone"
    assert "cellText(row, TEAM)" not in script.split("function haystack")[1].split("}")[0]


def test_every_displayed_date_uses_one_format(tmp_path: Path, derived: Path):
    """mm/dd/yyyy on the deadline, in the Acquired column and on the folded phone line.

    Three places that must never disagree, so the template sets the format once. The ISO sort
    key is deliberately not this: it is for ordering, and a text sort over mm/dd/yyyy would put
    every January before every February regardless of year.
    """
    page = render(tmp_path, derived)["keepers.html"]
    body = text(page)

    assert "11/26/2025" in body, "the deadline is not in the page's date format"
    assert "8/5/2025" in body, "an acquisition date is not in the page's date format"
    for old in ("26 Nov 2025", "05 Aug 2025", "08/05/2025", "2025-08-05 "):
        assert old not in body, f"{old!r} is a second date format on the page"

    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    assert 'data-k="2025-08-05"' in row, "the sort key stopped being ISO"


def test_prospect_eligibility_renders_as_a_badge_not_a_word(tmp_path: Path, derived: Path):
    """A one-character column, because the header sets the width and this table has none spare.

    The ordinary case is an empty cell on purpose: 165 dashes down a column hide the 23 rows
    somebody is looking for. The words stay in ``data-k``, so the filter is unaffected.
    """
    # Player 2 needs a first season of his own, or he is *unknown* rather than not eligible —
    # and those are the two states this test is here to keep apart.
    (derived / "player-origins.json").write_text(
        origins_doc({1: PRIOR, 2: PRIOR - 3}), encoding="utf-8"
    )
    page = render(tmp_path, derived)["keepers.html"]

    eligible = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    veteran = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "James Cook" in r)

    assert 'class="pbadge is-eligible"' in eligible
    assert "pbadge" not in veteran, "a checked-and-not-eligible row should carry no badge"
    assert '<td class="pstate">Not eligible</td>' in veteran, "the filter key went with the badge"
    # The badge says "P"; the tooltip says what it means.
    assert "Prospect eligible" in eligible


def test_the_grid_arrives_sorted_dearest_first(tmp_path: Path, derived: Path):
    """The server emits the default order, not the script.

    A browser running no JavaScript gets the same grid everyone else starts on, and the
    script's initial sort state describes that order rather than producing it — if the two
    disagreed, the first click on Salary would appear to do nothing.
    """
    season = build_keeper_season(derived, SEASON)
    prices = [line.price for line in season.rows]
    assert prices == sorted(prices, reverse=True), "rows are not dearest first"
    assert len(season.rows) == sum(len(t.lines) for t in season.teams)

    page = render(tmp_path, derived)["keepers.html"]
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S).group(1)
    served = [int(v) for v in re.findall(r'class="num money sal s\d" data-v="(\d+)"', body)]
    assert served == sorted(served, reverse=True), "the served HTML is not in the default order"

    script = page[page.index("<script>"):]
    assert "var sortColumn = SALARY;" in script
    assert "var ascending = false;" in script


def test_the_salary_column_carries_no_colour_scale(tmp_path: Path, derived: Path):
    """Deliberately plain, and this is the test that keeps it that way.

    Three encodings were tried and dropped — a tinted cell, a bar under the figure, and the
    figure coloured green to red. The reasons are worth defending rather than rediscovering:
    **red already means "acquired after the keeper deadline"** on this page, and a second
    unrelated meaning on the same colour weakens the one that carries a rule; and the grid
    arrives sorted dearest first, so magnitude is already encoded by position.
    """
    page = render(tmp_path, derived)["keepers.html"]

    for gone in ("sal-bar", "price_band", 'class="num money sal'):
        assert gone not in page, f"a salary encoding came back: {gone!r}"

    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "James Cook" in r)
    assert '<td class="num money" data-v="42">$42</td>' in row

    # `.money` — tabular figures and one weight — and nothing at all keyed to the amount.
    style = page[page.index("<style>"):page.index("</style>")]
    assert not re.search(r"\.sal[.\s{]", style), "a salary-specific style survived"


def test_prospects_only_is_a_checkbox_not_a_dropdown(tmp_path: Path, derived: Path):
    page = render(tmp_path, derived)["keepers.html"]
    assert '<input type="checkbox" id="f-prospect">' in page
    assert '<select id="f-prospect"' not in page

    script = page[page.index("<script>"):]
    assert "byProspect.checked" in script
    assert 'cellText(row, PROSPECT) === "Eligible"' in script, (
        "the checkbox does not filter to eligible players"
    )


def test_the_prospect_badge_sits_with_the_player_not_in_a_column(tmp_path: Path, derived: Path):
    """A column costs its header's width; a badge beside the name costs almost nothing.

    That width is what the phone layout was short of — it is the whole reason this moved.
    """
    (derived / "player-origins.json").write_text(
        origins_doc({1: PRIOR, 2: PRIOR - 3}), encoding="utf-8"
    )
    page = render(tmp_path, derived)["keepers.html"]

    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    assert ">P</th>" not in head, "the badge is back in a column of its own"

    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    name_cell = row[row.index("<td data-k="):row.index("</td>")]
    assert "pbadge" in name_cell, "the badge is not inside the player cell"


def test_a_declared_keeper_is_not_highlighted(tmp_path: Path, derived: Path):
    """The yellow wash explained a "Declared" column that no longer exists.

    Once the column went, four rows were tinted with nothing on the page saying why — a marker
    that raises a question it cannot answer is worse than no marker. Declarations live in the
    admin console now.
    """
    claims = tmp_path / "manual"
    claims.mkdir()
    (claims / "claims.json").write_text(
        json.dumps({"seasons": {str(SEASON): [
            {"season": SEASON, "manager_id": "t1", "espn_player_id": 1,
             "slot": "K1", "fee_allocated": 0, "computed_salary": 10}
        ]}}),
        encoding="utf-8",
    )
    out = tmp_path / "out"
    build_site(out, derived_dir=derived, history_dir=tmp_path / "nohistory", manual_dir=claims)
    page = (out / "keepers.html").read_text(encoding="utf-8")

    row = next(r for r in re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S) if "Puka Nacua" in r)
    assert "kept" not in row.split(">")[0], "a declared keeper is still being highlighted"
    assert ".kept" not in page, "the declared-row style outlived the column it explained"


def test_the_deadline_banner_is_sized_to_its_content(tmp_path: Path, derived: Path):
    """One short fact. A full-width bar promises more than it says, and it carries the mark."""
    page = render(tmp_path, derived)["keepers.html"]
    assert 'class="deadline-i"' in page, "the banner has no information mark"
    assert "display: inline-flex" in page.split(".deadline {")[1].split("}")[0]


def test_the_player_count_only_appears_while_filtering(tmp_path: Path, derived: Path):
    """Feedback while something is being narrowed, not a label sitting there permanently."""
    page = render(tmp_path, derived)["keepers.html"]
    assert '<span class="count" id="count" role="status" hidden></span>' in page
    script = page[page.index("<script>"):]
    assert "count.hidden = shown === rows.length;" in script


def test_an_ineligible_row_is_greyed_as_well_as_tinted(tmp_path: Path, derived: Path):
    """Colour alone leaves these reading as ordinary rows for anyone who cannot see it."""
    page = render(tmp_path, derived)["keepers.html"]
    style = page[page.index("<style>"):page.index("</style>")]
    assert ".grid tbody tr.late td { color: var(--muted); }" in style
    assert ".grid tbody tr.late { background: var(--bad-bg); }" in style, (
        "the tint was dropped when the grey went in — the ask was to keep both"
    )
