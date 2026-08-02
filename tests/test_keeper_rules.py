"""One test per rule branch. The fixture table in test_fixtures.py is the ground truth;
these are the branch-level tests that say *why* each number comes out the way it does."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from conftest import SEASON, TRADE_DEADLINE, claim, entry, override
from rs57.keeper_rules import (
    KEEPER_TAX,
    IssueCode,
    Severity,
    TeamKeeperResult,
    ValidationIssue,
    check_base_continuity,
    check_override_balance,
    compute_team_keepers,
    derive_kept_prior_year,
    effective_base_salary,
    fee_total_for,
    keeper_salary,
    validate_team_claims,
)
from rs57.models import AcquisitionSource, KeeperSlot


def codes(issues) -> set[IssueCode]:
    return {issue.code for issue in issues}


# --------------------------------------------------------------------------- salary


def test_first_keep_is_untaxed():
    assert keeper_salary(25, 0, kept_prior_year=False) == 25


def test_repeat_keep_pays_the_tax():
    assert keeper_salary(24, 0, kept_prior_year=True) == 24 + KEEPER_TAX


def test_fee_and_tax_stack():
    assert keeper_salary(30, 15, kept_prior_year=True) == 50


def test_prospect_ignores_fee_and_tax():
    """Prospects are kept at acquisition value. A stray fee is reported as FEE_ON_PROSPECT,
    never quietly priced in."""
    assert keeper_salary(3, 5, kept_prior_year=True, slot=KeeperSlot.PROSPECT) == 3


def test_zero_dollar_players_stay_free():
    assert keeper_salary(0, 0, kept_prior_year=False) == 0


# ----------------------------------------------------------------------------- fees


@pytest.mark.parametrize(
    ("n_keepers", "expected"), [(0, 0), (1, 0), (2, 5), (3, 15)]
)
def test_fee_tiers(n_keepers, expected):
    assert fee_total_for(n_keepers) == expected


def test_consolation_winner_owes_no_fees():
    assert fee_total_for(3, fees_waived=True) == 0


def test_no_tier_above_the_keeper_cap():
    with pytest.raises(ValueError, match="no fee tier"):
        fee_total_for(4)


# -------------------------------------------------------------------- the tax flag


def test_prior_keep_sets_the_tax():
    assert derive_kept_prior_year(claim(1, KeeperSlot.K2)) is True


def test_prospect_keep_does_not_set_the_tax():
    """Quentin Johnston and Tyjae Spears both show Kept=FALSE the season after a prospect
    keep, and neither appears in the commissioner's kept_last_year list."""
    assert derive_kept_prior_year(claim(1, KeeperSlot.PROSPECT)) is False


def test_drop_clears_the_tax():
    assert derive_kept_prior_year(claim(1, KeeperSlot.K1), dropped_since=True) is False


def test_never_kept_is_untaxed():
    assert derive_kept_prior_year(None) is False


def test_trade_does_not_clear_the_tax():
    """There is deliberately no 'was traded' argument — the tax belongs to the player's
    history, not to whoever holds him now. Brian Thomas Jr. was acquired by trade on
    Oct 29 2025 and is still priced at $16 + $5."""
    traded = entry(1, 16, kept=True, manager="m2", source=AcquisitionSource.TRADE)
    assert keeper_salary(traded.base_salary, 0, traded.kept_prior_year) == 21


# ------------------------------------------------------------------------ overrides


def test_live_override_beats_espn():
    saquon = entry(1, 68, kept=True)
    assert effective_base_salary(saquon, [override(1, 71)]) == 71
    assert keeper_salary(71, 0, kept_prior_year=True) == 76


def test_reverted_override_falls_back_to_espn():
    saquon = entry(1, 68, kept=True)
    assert effective_base_salary(saquon, [override(1, 71, reverted=True)]) == 68


def test_override_for_another_season_is_ignored():
    player = entry(1, 68, season=SEASON)
    assert effective_base_salary(player, [override(1, 71, season=SEASON - 1)]) == 68


def test_latest_override_wins_when_several_apply():
    """Should not happen, but resolve it deterministically rather than by dict order."""
    player = entry(1, 68)
    early = override(1, 70, created=datetime(2025, 9, 1))
    late = override(1, 71, created=datetime(2025, 10, 1))
    assert effective_base_salary(player, [early, late]) == 71
    assert effective_base_salary(player, [late, early]) == 71


def test_balanced_cash_trade_is_quiet():
    roster = [entry(1, 18), entry(2, 33)]
    overrides = [override(1, 19), override(2, 32)]
    assert check_override_balance(roster, overrides) == []


def test_missing_cash_trade_leg_is_flagged():
    roster = [entry(1, 18), entry(2, 33)]
    issues = check_override_balance(roster, [override(1, 19)])
    assert codes(issues) == {IssueCode.OVERRIDES_UNBALANCED}
    assert issues[0].severity is Severity.REVIEW
    assert "+1" in issues[0].message


def test_known_orphan_stays_quiet():
    """Saquon's +$3 has no recoverable counterparty. Flagged once, then silent forever."""
    roster = [entry(1, 68)]
    assert check_override_balance(roster, [override(1, 71, unpaired_ok=True)]) == []


# ------------------------------------------------------------------- ratchet audit


def test_base_continuity_accepts_a_clean_ratchet():
    roster = [entry(1, 35, kept=True)]
    prior = [claim(1, KeeperSlot.K1, season=SEASON - 1, computed=35)]
    assert check_base_continuity(roster, prior) == []


def test_base_discontinuity_is_flagged_for_review():
    roster = [entry(1, 30, kept=True)]
    prior = [claim(1, KeeperSlot.K1, season=SEASON - 1, computed=35)]
    issues = check_base_continuity(roster, prior)
    assert codes(issues) == {IssueCode.BASE_DISCONTINUITY}
    assert issues[0].severity is Severity.REVIEW


def test_an_override_can_explain_a_discontinuity():
    roster = [entry(1, 30, kept=True)]
    prior = [claim(1, KeeperSlot.K1, season=SEASON - 1, computed=35)]
    assert check_base_continuity(roster, prior, [override(1, 35)]) == []


def test_no_prior_history_means_no_false_alarms():
    """Before the backfill there is nothing to compare against, and a wall of noise would
    teach everyone to ignore the check."""
    assert check_base_continuity([entry(1, 30, kept=True)], []) == []


# ---------------------------------------------------------------------- validation


def test_a_legal_three_keeper_team_is_clean():
    roster = [entry(1, 25), entry(2, 30), entry(3, 10)]
    claims = [
        claim(1, KeeperSlot.K1, fee=7),
        claim(2, KeeperSlot.K2, fee=8),
        claim(3, KeeperSlot.K3, fee=0),
    ]
    assert validate_team_claims(claims, roster) == []


def test_too_many_keepers():
    """With only three keeper slots this also trips DUPLICATE_SLOT — belt and braces."""
    roster = [entry(i, 10) for i in range(1, 5)]
    claims = [
        claim(1, KeeperSlot.K1),
        claim(2, KeeperSlot.K2),
        claim(3, KeeperSlot.K3),
        claim(4, KeeperSlot.K3),
    ]
    assert IssueCode.TOO_MANY_KEEPERS in codes(validate_team_claims(claims, roster))


def test_too_many_prospects():
    roster = [entry(1, 3), entry(2, 4)]
    claims = [claim(1, KeeperSlot.PROSPECT), claim(2, KeeperSlot.PROSPECT)]
    assert IssueCode.TOO_MANY_PROSPECTS in codes(validate_team_claims(claims, roster))


def test_fee_total_must_match_the_tier():
    roster = [entry(1, 25), entry(2, 30), entry(3, 10)]
    claims = [
        claim(1, KeeperSlot.K1, fee=5),
        claim(2, KeeperSlot.K2, fee=5),
        claim(3, KeeperSlot.K3, fee=0),
    ]
    issues = validate_team_claims(claims, roster)
    assert IssueCode.FEE_TOTAL_MISMATCH in codes(issues)


def test_consolation_winner_must_not_be_charged():
    roster = [entry(1, 25), entry(2, 30)]
    claims = [claim(1, KeeperSlot.K1, fee=5), claim(2, KeeperSlot.K2, fee=0)]
    assert validate_team_claims(claims, roster, fees_waived=True) != []
    zeroed = [claim(1, KeeperSlot.K1, fee=0), claim(2, KeeperSlot.K2, fee=0)]
    assert validate_team_claims(zeroed, roster, fees_waived=True) == []


def test_negative_fee():
    roster = [entry(1, 25), entry(2, 30)]
    claims = [claim(1, KeeperSlot.K1, fee=-5), claim(2, KeeperSlot.K2, fee=10)]
    assert IssueCode.NEGATIVE_FEE in codes(validate_team_claims(claims, roster))


def test_fee_on_prospect():
    roster = [entry(1, 25), entry(2, 3)]
    claims = [claim(1, KeeperSlot.K1, fee=0), claim(2, KeeperSlot.PROSPECT, fee=5)]
    assert IssueCode.FEE_ON_PROSPECT in codes(validate_team_claims(claims, roster))


def test_prospect_is_excluded_from_the_fee_tier():
    """Purdy Good at Fantasy, 2025: two keepers plus a prospect owes the $5 tier, not $15."""
    roster = [entry(1, 8), entry(2, 22), entry(3, 3)]
    claims = [
        claim(1, KeeperSlot.K1, fee=0),
        claim(2, KeeperSlot.K2, fee=5),
        claim(3, KeeperSlot.PROSPECT, fee=0),
    ]
    assert IssueCode.FEE_TOTAL_MISMATCH not in codes(validate_team_claims(claims, roster))


def test_duplicate_player():
    roster = [entry(1, 25)]
    claims = [claim(1, KeeperSlot.K1, fee=5), claim(1, KeeperSlot.K2, fee=0)]
    assert IssueCode.DUPLICATE_PLAYER in codes(validate_team_claims(claims, roster))


def test_duplicate_slot():
    roster = [entry(1, 25), entry(2, 30)]
    claims = [claim(1, KeeperSlot.K1, fee=5), claim(2, KeeperSlot.K1, fee=0)]
    assert IssueCode.DUPLICATE_SLOT in codes(validate_team_claims(claims, roster))


def test_player_not_on_roster():
    """The cross-file check a foreign key would have given us for free."""
    claims = [claim(99, KeeperSlot.K1, fee=0)]
    assert IssueCode.PLAYER_NOT_ON_ROSTER in codes(validate_team_claims(claims, []))


# --- prospect rule 1: must be a rookie -------------------------------------------------
#
# SEASON is 2026 throughout, so a player who began in 2025 was a rookie in the season the
# 2026 keep is made from, and one who began in 2024 was not.


def test_a_player_drafted_last_season_is_a_legal_prospect():
    """The Cam Skattebo shape, and the one the arithmetic is easiest to break on.

    He began in 2025 and was kept as a 2026 prospect — ``2026 - 2025 == 1``, legal. Write the
    count as ``claim.season - began + 1`` and it comes out 2, and the check rejects the single
    claim in the league's history we have the strongest evidence for.
    """
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], first_nfl_season={1: SEASON - 1}
    )
    assert codes(issues) == set(), f"a genuine rookie was rejected: {issues}"


def test_a_second_year_player_is_not_a_prospect():
    """The Jawhar Jordan shape: began 2024, so by 2026 he is a second-year player.

    ESPN reports ``experience.years == 1`` for him because he accrued only one season. Reading
    that field instead of the draft class is what would let him through.
    """
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], first_nfl_season={1: SEASON - 2}
    )
    found = [i for i in issues if i.code is IssueCode.PROSPECT_TOO_MANY_SEASONS]
    assert len(found) == 1
    assert str(SEASON - 2) in found[0].message
    assert "second-year" in found[0].message


def test_the_rookie_rule_flags_rather_than_blocks():
    """Commissioner's call, 2026-08-01: the draft class comes from outside the league, so the
    final word stays with a human. Contrast the repeat-claim check, which stays ERROR."""
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], first_nfl_season={1: SEASON - 2}
    )
    rookie = next(i for i in issues if i.code is IssueCode.PROSPECT_TOO_MANY_SEASONS)
    assert rookie.severity is Severity.REVIEW

    blocks = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], prior_prospect_ids={1}
    )
    repeat = next(i for i in blocks if i.code is IssueCode.PROSPECT_REPEAT_CLAIM)
    assert repeat.severity is Severity.ERROR, "the league's own record still blocks"


def test_no_mapping_means_the_rule_is_not_applied():
    """``None`` is how a pre-2026 season, and every older test, opts out of rule 1 entirely."""
    issues = validate_team_claims([claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)])
    assert IssueCode.PROSPECT_ROOKIE_UNVERIFIED not in codes(issues)
    assert IssueCode.PROSPECT_TOO_MANY_SEASONS not in codes(issues)


def test_an_empty_mapping_means_the_rule_is_applied():
    """An empty mapping is "checking, and nothing is known" — not "not checking".

    Collapse the two and a season with no origins file on disk passes every prospect in
    silence, which is the exact hole this replaced.
    """
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], first_nfl_season={}
    )
    assert IssueCode.PROSPECT_ROOKIE_UNVERIFIED in codes(issues)


def test_an_unknown_draft_class_is_reported_not_passed():
    """A prospect ESPN cannot answer for is unverified, never assumed fine."""
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], first_nfl_season={999: 2025}
    )
    found = [i for i in issues if i.code is IssueCode.PROSPECT_ROOKIE_UNVERIFIED]
    assert len(found) == 1
    assert found[0].severity is Severity.REVIEW
    assert "could not be checked" in found[0].message


def test_a_first_season_that_cannot_have_happened_is_reported():
    """A player cannot have been on last season's roster before his career began.

    Impossible data, not an illegal claim — reported as unverified rather than as the manager's
    mistake, and never passed over just because it is not the shape the check expected.
    """
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], first_nfl_season={1: SEASON}
    )
    assert IssueCode.PROSPECT_ROOKIE_UNVERIFIED in codes(issues)


def test_prospect_acquired_after_the_trade_deadline():
    late = entry(1, 3, acquired=TRADE_DEADLINE + timedelta(days=1))
    claims = [claim(1, KeeperSlot.PROSPECT)]
    issues = validate_team_claims(claims, [late], trade_deadline=TRADE_DEADLINE)
    assert IssueCode.PROSPECT_ACQUIRED_AFTER_DEADLINE in codes(issues)


def test_prospect_claimed_in_an_earlier_season():
    """Tyjae Spears was claimed as a prospect in 2024 and again in 2025. An oversight."""
    roster = [entry(1, 3)]
    claims = [claim(1, KeeperSlot.PROSPECT)]
    issues = validate_team_claims(claims, roster, prior_prospect_ids={1})
    assert IssueCode.PROSPECT_REPEAT_CLAIM in codes(issues)


def test_a_prospect_who_has_been_started_is_fine_now():
    """The never-started rule is retired (commissioner, 2026-07-30). Prospects may be started;
    the only remaining stipulation is that they are rookies."""
    issues = validate_team_claims([claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)])
    assert codes(issues) == set(), "a legal prospect raises nothing at all"


def test_a_repeat_prospect_claim_is_how_the_rookie_rule_is_enforced():
    """Rule 1 is "must be a rookie", and this is the form of it derivable from the league's
    OWN records: a player has exactly one rookie season, so a repeat claim is a violation with
    no outside source needed. It was the only detectable form until ESPN's draft class arrived;
    it is now the independent cross-check, and it is why this outlived rule 2.

    It stays ERROR while the draft-class check is REVIEW. The league's own record blocks; an
    outside data source flags."""
    issues = validate_team_claims(
        [claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)], prior_prospect_ids={1}
    )
    repeat = [i for i in issues if i.code is IssueCode.PROSPECT_REPEAT_CLAIM]
    assert len(repeat) == 1
    assert repeat[0].severity is Severity.ERROR
    assert "not a rookie" in repeat[0].message


def test_the_repeat_check_is_the_callers_to_apply():
    """Passing no prior prospects is how a season under the OLD rule is validated — second-year
    prospects were legal then, and the record holds one."""
    issues = validate_team_claims([claim(1, KeeperSlot.PROSPECT)], [entry(1, 3)])
    assert IssueCode.PROSPECT_REPEAT_CLAIM not in codes(issues)


# ------------------------------------------------------------------------- pricing


def test_compute_prices_a_whole_team():
    roster = [entry(1, 24, kept=True), entry(2, 30, kept=True), entry(3, 3)]
    claims = [
        claim(1, KeeperSlot.K1, fee=0),
        claim(2, KeeperSlot.K2, fee=5),
        claim(3, KeeperSlot.PROSPECT, fee=0),
    ]
    result = compute_team_keepers(claims, roster)

    assert [keeper.salary for keeper in result.keepers] == [29, 40, 3]
    assert result.total_salary == 72
    assert result.total_fees == 5


def test_review_issues_do_not_block():
    """``blocked`` is ERROR-only. Every REVIEW code now comes from the audits rather than from
    claim validation, so this exercises the property directly."""
    review = ValidationIssue(
        IssueCode.BASE_DISCONTINUITY, Severity.REVIEW, "unverified, not wrong"
    )
    assert TeamKeeperResult(manager_id="m1", issues=(review,)).blocked is False
    error = ValidationIssue(IssueCode.NEGATIVE_FEE, Severity.ERROR, "wrong")
    assert TeamKeeperResult(manager_id="m1", issues=(review, error)).blocked is True


def test_errors_block_but_salaries_still_compute():
    """An admin screen saying 'you owe $5 more in fees' is far more useful alongside the
    salaries than instead of them."""
    roster = [entry(1, 25), entry(2, 30)]
    claims = [claim(1, KeeperSlot.K1, fee=0), claim(2, KeeperSlot.K2, fee=0)]
    result = compute_team_keepers(claims, roster)
    assert result.blocked is True
    assert [keeper.salary for keeper in result.keepers] == [25, 30]


def test_pricing_uses_the_override():
    roster = [entry(1, 68, kept=True)]
    claims = [claim(1, KeeperSlot.K1, fee=0)]
    result = compute_team_keepers(claims, roster, [override(1, 71, unpaired_ok=True)])
    assert result.keepers[0].salary == 76


def test_there_is_no_salary_cap():
    """Keeper totals are unbounded. Nothing here should ever object to a $200 keeper."""
    roster = [entry(1, 195, kept=True)]
    claims = [claim(1, KeeperSlot.K1, fee=0)]
    result = compute_team_keepers(claims, roster)
    assert result.keepers[0].salary == 200
    assert result.issues == ()
