"""The site generator, and the rules it is not allowed to break.

Three of these tests exist because getting them wrong publishes something. The site is a
public GitHub Pages site off a public repo: a REVIEW item rendered as though it had been
checked, a manual text field rendered as markup, or a salary recomputed in a template are
each permanent once they ship.
"""

from __future__ import annotations

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
    (out / f"{PRIOR}.json").write_text(
        json.dumps(keeper_doc(season=PRIOR, roster=[], players=[])), encoding="utf-8"
    )
    (out / f"{PRIOR}-stats.json").write_text(json.dumps(stats_doc()), encoding="utf-8")
    return out


def render(tmp_path: Path, derived: Path) -> dict[str, str]:
    out = tmp_path / "out"
    build_site(out, derived_dir=derived, history_dir=tmp_path / "nohistory")
    return {path.name: path.read_text(encoding="utf-8") for path in out.iterdir()}


def text(html: str) -> str:
    """The page with its tags removed, for asserting on what a reader actually sees."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


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


def test_keeper_sync_warnings_reach_the_page(tmp_path: Path):
    derived = tmp_path / "derived"
    derived.mkdir()
    doc = keeper_doc()
    doc["review"]["warnings"] = ["2026 has not been drafted, so base_salary is keeperValue"]
    (derived / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    page = render(tmp_path, derived)["keepers.html"]
    assert "has not been drafted" in page
    assert "unverified" in text(page).lower()


def test_derived_consolation_winner_is_not_rendered_as_settled(tmp_path: Path, derived: Path):
    """Nothing records the consolation winner yet, so a fee waiver is derived, not decided."""
    page = render(tmp_path, derived)["keepers.html"]
    body = text(page).lower()
    assert "not been confirmed by the commissioner" in body
    assert "unverified" in body


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
