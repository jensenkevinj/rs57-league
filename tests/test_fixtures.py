"""The engine against the reviewed fixture table.

Every expected value in keeper_cases.json is a literal the commissioner signed off on. If a
case here fails, the rule changed or the engine is wrong — per plan §10, chase it down rather
than adjusting the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import entry, override
from rs57.keeper_rules import (
    IssueCode,
    Severity,
    effective_base_salary,
    fee_total_for,
    keeper_salary,
    validate_team_claims,
)
from conftest import claim
from rs57.models import KeeperSlot

FIXTURES = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "keeper_cases.json").read_text(
        encoding="utf-8"
    )
)

KEEPER_SLOT_ORDER = (KeeperSlot.K1, KeeperSlot.K2, KeeperSlot.K3)


def _chain_links():
    for chain in FIXTURES["chains"]:
        for index, link in enumerate(chain["links"]):
            yield pytest.param(chain, link, id=f"{chain['case']}[{link['season']}]")


@pytest.mark.parametrize(("chain", "link"), list(_chain_links()))
def test_chain_link_salary(chain, link):
    assert (
        keeper_salary(
            link["base"], link["fee"], link["kept_prior"], KeeperSlot(link["slot"])
        )
        == link["expected"]
    ), chain["why"]


@pytest.mark.parametrize(
    "chain",
    [pytest.param(c, id=c["case"]) for c in FIXTURES["chains"] if c["expects_ratchet"]],
)
def test_chain_ratchets(chain):
    """Next season's base is this season's salary. That is the whole ratchet, and it is the
    part the spreadsheet never did — it re-read the base from ESPN every year."""
    links = chain["links"]
    for current, following in zip(links, links[1:]):
        assert following["base"] == current["expected"], chain["why"]


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c["case"]) for c in FIXTURES["salaries"]]
)
def test_salary_case(case):
    assert (
        keeper_salary(
            case["base"], case["fee"], case["kept_prior"], KeeperSlot(case["slot"])
        )
        == case["expected"]
    ), case.get("why", "")


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c["player"] + "/" + c["case"]) for c in FIXTURES["overrides"]]
)
def test_override_case(case):
    player = entry(1, case["espn_base"], kept=case["kept_prior"])
    live = override(
        1,
        case["override"],
        reverted=case["reverted"],
        unpaired_ok=case["unpaired_ok"],
    )
    base = effective_base_salary(player, [live])
    assert (
        keeper_salary(base, case["fee"], case["kept_prior"]) == case["expected"]
    ), case["why"]


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c["case"]) for c in FIXTURES["fee_tiers"]]
)
def test_fee_tier_case(case):
    assert (
        fee_total_for(case["n_keepers"], case["fees_waived"]) == case["expected_total"]
    )


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c["case"]) for c in FIXTURES["fee_splits"]]
)
def test_fee_split_case(case):
    fees = case["fees"]
    roster = [entry(index, 10) for index, _ in enumerate(fees, start=1)]
    claims = [
        claim(index, KEEPER_SLOT_ORDER[index - 1], fee=fee)
        for index, fee in enumerate(fees, start=1)
    ]
    issues = validate_team_claims(claims, roster, fees_waived=case["fees_waived"])
    mismatched = IssueCode.FEE_TOTAL_MISMATCH in {issue.code for issue in issues}
    assert mismatched is not case["valid"], case.get("why", "")


def test_fixture_codes_match_the_engine():
    """Guards against fixture rot in both directions: a code the engine can emit but the
    table never mentions, or a table entry naming a code that no longer exists."""
    declared = {case["expect_code"] for case in FIXTURES["validation"]}
    implemented = {code.value for code in IssueCode}
    assert declared == implemented


@pytest.mark.parametrize(
    "case", [pytest.param(c, id=c["case"]) for c in FIXTURES["validation"]]
)
def test_fixture_severities_are_declared_correctly(case):
    assert case["severity"] in {Severity.ERROR.value, Severity.REVIEW.value}


# ---------------------------------------------------------------------------
# The prospect rule against every claim the league has ever made
# ---------------------------------------------------------------------------

PROSPECTS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "prospect_cases.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize(
    "case",
    [pytest.param(c, id=f"{c['player']}-{c['kept_in_season']}") for c in PROSPECTS["claims"]],
)
def test_every_recorded_prospect_claim(case):
    """Ten claims, ten verdicts, checked against ESPN's draft class.

    This is the table that says the feature is right rather than merely wired up. Nine claims
    are rookies by ESPN's own record. The tenth — Tyjae Spears kept in 2025 — was legal under
    the second-year rule then in force, and ``rule_one_applies`` is False for exactly that
    reason. An outside source reproducing the league's record, including the one exception, is
    the evidence ``PROSPECT_RULES_TIGHTENED`` is set where it is.
    """
    season = case["kept_in_season"]
    player_id = case["espn_player_id"]
    claims = [claim(player_id, KeeperSlot.PROSPECT, season=season)]
    roster = [entry(player_id, 3, season=season)]

    # Exactly what validate.py does: the mapping is passed only for governed seasons.
    origins = {player_id: case["first_nfl_season"]} if case["rule_one_applies"] else None
    issues = validate_team_claims(claims, roster, first_nfl_season=origins)
    flagged = IssueCode.PROSPECT_TOO_MANY_SEASONS in {issue.code for issue in issues}

    assert flagged is case["expect_flagged"], (
        f"{case['player']} kept in {season}, began {case['first_nfl_season']}: "
        f"expected flagged={case['expect_flagged']}, got {flagged}"
    )


def test_the_prospect_table_covers_the_whole_recorded_history():
    """A fixture that quietly loses rows stops being ground truth.

    Ten claims: seven prospects in 2024, two in 2025, one in 2026. If this count changes,
    either a season was frozen or a claim was recorded — both fine, but the table has to be
    rebuilt against ESPN rather than left behind.
    """
    by_season = {}
    for case in PROSPECTS["claims"]:
        by_season[case["kept_in_season"]] = by_season.get(case["kept_in_season"], 0) + 1
    assert by_season == {2024: 7, 2025: 2, 2026: 1}
    assert all(case["first_nfl_season"] for case in PROSPECTS["claims"]), (
        "a recorded prospect has no draft class — the table cannot judge him"
    )


def test_the_one_historical_exception_is_the_one_we_know_about():
    """Spears in 2025 is the single claim today's rule would reject. Named, not incidental."""
    would_fail_today = [
        case
        for case in PROSPECTS["claims"]
        if case["elapsed"] is not None and case["elapsed"] > 1
    ]
    assert [(c["player"], c["kept_in_season"]) for c in would_fail_today] == [
        ("Tyjae Spears", 2025)
    ]
    assert not would_fail_today[0]["rule_one_applies"], (
        "the pre-2026 exception is being judged by the post-2026 rule"
    )
