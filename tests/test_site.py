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

from rs57.keeper_rules import KEEPER_TAX, keeper_salary
from rs57.models import KeeperSlot
from rs57.site import (
    TEMPLATES,
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
