"""``validate.py`` — and above all, that it never reports a clean run it did not earn.

The rule under test throughout: **a check that could not run is reported as SKIPPED, and a
REVIEW item is reported as unverified.** Neither may read as a pass. ``data/history/`` is empty
today, so the two ``keeper_rules`` audits genuinely cannot run — silence there would be
indistinguishable from success.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rs57 import validate
from rs57.models import json_dumps

SEASON = {
    "season": 2025,
    "franchises": [{"manager_id": "t1", "season": 2025, "name": "Fake News"}],
    "players": [
        {"espn_player_id": 11, "name": "A Player", "position": "WR", "nfl_team": "BUF"}
    ],
    "roster": [
        {
            "season": 2025,
            "manager_id": "t1",
            "espn_player_id": 11,
            "acquired_at": "2025-08-05T12:00:00",
            "base_salary": 5,
            "kept_prior_year": False,
            "source": "draft",
        }
    ],
}


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    for name in ("derived", "manual", "history"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(validate, "DATA", tmp_path)
    return tmp_path


def write(path: Path, doc: object) -> None:
    path.write_text(json_dumps(doc), encoding="utf-8")


class TestReferenceChecks:
    def test_a_clean_season_passes(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        report = validate.run()
        assert report.errors == []
        assert any("all references resolve" in message for message in report.checked)

    def test_a_roster_entry_for_an_unknown_franchise_is_an_error(self, data_dir):
        doc = json.loads(json_dumps(SEASON))
        doc["roster"][0]["manager_id"] = "t99"
        write(data_dir / "derived" / "2025.json", doc)
        report = validate.run()
        assert any("has no franchise" in message for message in report.errors)

    def test_a_roster_entry_for_an_unknown_player_is_an_error(self, data_dir):
        doc = json.loads(json_dumps(SEASON))
        doc["roster"][0]["espn_player_id"] = 99
        write(data_dir / "derived" / "2025.json", doc)
        report = validate.run()
        assert any("not in this season's player list" in message for message in report.errors)

    def test_an_orphan_player_is_an_error(self, data_dir):
        doc = json.loads(json_dumps(SEASON))
        doc["players"].append(
            {"espn_player_id": 12, "name": "Orphan", "position": "RB", "nfl_team": "FA"}
        )
        write(data_dir / "derived" / "2025.json", doc)
        report = validate.run()
        assert any("on no roster" in message for message in report.errors)

    def test_schema_drift_fails_loudly_rather_than_loading_a_none(self, data_dir):
        doc = json.loads(json_dumps(SEASON))
        doc["players"][0]["unexpected_field"] = 1
        write(data_dir / "derived" / "2025.json", doc)
        report = validate.run()
        assert any("does not load into the models" in message for message in report.errors)


class TestSkippedChecksAreNeverSilent:
    def test_empty_history_is_reported_not_assumed(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        report = validate.run()
        assert any("data/history/ is empty" in message for message in report.skipped)
        assert any(
            "UNVERIFIED, not verified" in message for message in report.skipped
        ), "the ratchet audit cannot run without history and must say so"

    def test_missing_override_records_are_reported(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        report = validate.run()
        assert any("check_override_balance" in message for message in report.skipped)

    def test_an_empty_derived_directory_is_reported(self, data_dir):
        report = validate.run()
        assert any("holds no season files" in message for message in report.skipped)

    def test_missing_manual_files_are_reported(self, data_dir):
        report = validate.run()
        assert any("payouts.json is missing" in message for message in report.skipped)

    def test_a_leftover_prospects_file_is_an_error(self, data_dir):
        """Prospect keeps are derived from frozen claims now. A surviving copy of the retired
        hand-maintained file is a second record of the same fact, and two will drift."""
        write(data_dir / "manual" / "prospects.json", {"seasons": {"2025": [1]}})
        report = validate.run()
        assert any("prospects.json still exists" in message for message in report.errors)


class TestTheRatchetIsAuditedAgainstTheRightNumber:
    """The ratchet compares last season's claims against what each keeper **carried into**
    this one. Point it at a post-draft roster and it compares two numbers that are not
    supposed to be equal — and it does not fail loudly, it reports every keeper as off by
    exactly his own fee and tax. That is the whole reason a season records which ESPN field
    its bases came from.
    """

    def _claims(self, salary: int) -> dict:
        return {
            "season": 2025,
            "claims": [
                {
                    "season": 2025,
                    "manager_id": "t1",
                    "espn_player_id": 11,
                    "slot": "K1",
                    "fee_allocated": 0,
                    "computed_salary": salary,
                }
            ],
        }

    def _derived_2026(self, base_field: str, base_salary: int) -> dict:
        return {
            "season": 2026,
            "source": {"base_salary_field": base_field, "drafted": base_field != "keeperValue"},
            "franchises": [{"manager_id": "t1", "season": 2026, "name": "Fake News"}],
            "players": [
                {"espn_player_id": 11, "name": "A Player", "position": "WR", "nfl_team": "BUF"}
            ],
            "roster": [
                {
                    "season": 2026,
                    "manager_id": "t1",
                    "espn_player_id": 11,
                    "acquired_at": "2025-08-05T12:00:00",
                    "base_salary": base_salary,
                    "kept_prior_year": True,
                    "source": "draft",
                }
            ],
        }

    def test_a_pre_draft_season_is_audited_against_the_prior_claims(self, data_dir):
        write(data_dir / "history" / "2025.json", self._claims(10))
        write(data_dir / "derived" / "2026.json", self._derived_2026("keeperValue", 10))
        report = validate.run()
        assert any("check_base_continuity ran for 2026" in m for m in report.checked)
        assert not [m for m in report.reviews if "base continuity" in m]

    def test_a_real_discontinuity_is_reported(self, data_dir):
        write(data_dir / "history" / "2025.json", self._claims(10))
        write(data_dir / "derived" / "2026.json", self._derived_2026("keeperValue", 17))
        report = validate.run()
        assert any("base is $17" in m and "was $10" in m for m in report.reviews)

    def test_a_post_draft_roster_is_never_used_as_the_carried_in_base(self, data_dir):
        """The one that matters. 2026's base here is what its OWN auction charged, so it
        legitimately differs from 2025's salary by 2026's fee and tax. Auditing it would
        invent a discontinuity for every keeper in the league."""
        write(data_dir / "history" / "2025.json", self._claims(10))
        write(
            data_dir / "derived" / "2026.json",
            self._derived_2026("keeperValueFuture", 15),
        )
        report = validate.run()
        assert not [m for m in report.reviews if "base continuity" in m], (
            "a post-draft base was audited as though it were the carried-in one"
        )
        assert any(
            "UNVERIFIED" in m and "2026" in m for m in report.skipped
        ), "and the pairing it could not make must be reported, not dropped"

    def test_the_current_seasons_auction_does_not_silently_break_the_audit(self, data_dir):
        """The live hazard: derived/{current}.json flips from keeperValue to
        keeperValueFuture the day the auction runs. Nothing else about the repo changes."""
        write(data_dir / "history" / "2025.json", self._claims(10))
        write(data_dir / "derived" / "2026.json", self._derived_2026("keeperValue", 10))
        before = validate.run()
        write(
            data_dir / "derived" / "2026.json",
            self._derived_2026("keeperValueFuture", 15),
        )
        after = validate.run()
        assert any("check_base_continuity ran" in m for m in before.checked)
        assert not any("check_base_continuity ran" in m for m in after.checked)
        assert after.errors == before.errors == []


class TestFrozenSeasonsCrossCheck:
    def _frozen(self, year: int, *, charged: int | None = None, carried: int | None = None):
        doc: dict = {"season": year}
        if charged is not None:
            doc["roster"] = [
                {
                    "season": year,
                    "manager_id": "t1",
                    "espn_player_id": 11,
                    "acquired_at": "2024-08-05T12:00:00",
                    "base_salary": charged,
                    "kept_prior_year": False,
                    "source": "draft",
                }
            ]
        if carried is not None:
            doc["roster_carried_in"] = [
                {
                    "season": year,
                    "manager_id": "t1",
                    "espn_player_id": 11,
                    "acquired_at": "2024-08-05T12:00:00",
                    "base_salary": carried,
                    "kept_prior_year": True,
                    "source": "draft",
                }
            ]
        return doc

    def test_matching_prices_pass(self, data_dir):
        write(data_dir / "history" / "2024.json", self._frozen(2024, charged=10))
        write(data_dir / "history" / "2025.json", self._frozen(2025, charged=15, carried=10))
        report = validate.run()
        assert report.errors == []
        assert any("carried-in prices agree" in m for m in report.checked)

    def test_a_carried_in_price_that_contradicts_the_prior_season_is_an_error(self, data_dir):
        """The tripwire for reading a completed season's ``keeperValue``, which ESPN
        overwrites with ``keeperValueFuture`` the moment that season drafts. Here the
        carried-in price has picked up 2025's own charge instead of 2024's."""
        write(data_dir / "history" / "2024.json", self._frozen(2024, charged=10))
        write(data_dir / "history" / "2025.json", self._frozen(2025, charged=15, carried=15))
        report = validate.run()
        assert any("was read from the wrong ESPN field" in m for m in report.errors)


class TestClaimsAreNeverCheckedAgainstTheWrongRoster:
    def test_a_season_with_no_declaration_roster_is_skipped_not_substituted(self, data_dir):
        """A claim checked against another season's roster reports confident nonsense: every
        player who has changed hands since reads as never having been on the team."""
        write(
            data_dir / "history" / "2024.json",
            {
                "season": 2024,
                "roster_at_declaration": [
                    {
                        "season": 2024,
                        "manager_id": "t1",
                        "espn_player_id": 11,
                        "acquired_at": "2023-08-05T12:00:00",
                        "base_salary": 10,
                        "kept_prior_year": False,
                        "source": "draft",
                    }
                ],
                "claims": [
                    {
                        "season": 2024,
                        "manager_id": "t1",
                        "espn_player_id": 11,
                        "slot": "K1",
                        "fee_allocated": 0,
                        "computed_salary": 10,
                    }
                ],
            },
        )
        # 2025 has claims but no declaration roster of its own.
        write(
            data_dir / "history" / "2025.json",
            {
                "season": 2025,
                "claims": [
                    {
                        "season": 2025,
                        "manager_id": "t1",
                        "espn_player_id": 77,
                        "slot": "K1",
                        "fee_allocated": 0,
                        "computed_salary": 20,
                    }
                ],
            },
        )
        report = validate.run()
        assert not any("player_not_on_roster" in m for m in report.errors), (
            "2025's claim was checked against 2024's roster"
        )
        assert any(
            "2025's claims were NOT validated against a roster" in m for m in report.skipped
        )


def _declared(season: int, *players: int) -> list[dict]:
    return [
        {
            "season": season,
            "manager_id": "t1",
            "espn_player_id": pid,
            "acquired_at": "2023-08-05T12:00:00",
            "base_salary": 10,
            "kept_prior_year": False,
            "source": "draft",
        }
        for pid in players
    ]


def _claim(season: int, pid: int, slot: str = "K1", fee: int = 0) -> dict:
    return {
        "season": season,
        "manager_id": "t1",
        "espn_player_id": pid,
        "slot": slot,
        "fee_allocated": fee,
        "computed_salary": 10,
    }


class TestTheFeeWaiverIsApplied:
    def test_a_waived_team_owes_no_fee(self, data_dir):
        """The consolation winner's fees are waived for a year. Without that, their $0
        allocation reads as a $5 shortfall and a correct record looks broken."""
        write(
            data_dir / "history" / "2025.json",
            {
                "season": 2025,
                "fees_waived_manager_id": "t1",
                "roster_at_declaration": _declared(2025, 11, 12),
                "claims": [_claim(2025, 11), _claim(2025, 12, "K2")],
            },
        )
        report = validate.run()
        assert not any("fee_total_mismatch" in m for m in report.errors)

    def test_the_waiver_lands_the_year_AFTER_the_bracket_was_won(self, data_dir):
        """``consolation_winner_id`` names the winner of *that* season's bracket, and the
        waiver applies to the next one. Off by one and every waiver lands on the wrong team
        for a whole year — in both directions, since the team that did win still gets billed.
        """
        write(
            data_dir / "manual" / "seasons.json",
            {"seasons": {"2025": {"year": 2025, "consolation_winner_id": "t1"}}},
        )
        write(
            data_dir / "manual" / "claims.json",
            {"seasons": {"2026": [_claim(2026, 11), _claim(2026, 12, "K2")]}},
        )
        write(
            data_dir / "derived" / "2026.json",
            {
                "season": 2026,
                "source": {"base_salary_field": "keeperValue", "drafted": False},
                "franchises": [{"manager_id": "t1", "season": 2026, "name": "Fake News"}],
                "players": [
                    {"espn_player_id": pid, "name": f"P{pid}", "position": "WR",
                     "nfl_team": "BUF"}
                    for pid in (11, 12)
                ],
                "roster": _declared(2026, 11, 12),
            },
        )
        report = validate.run()
        assert not any("fee_total_mismatch" in m for m in report.errors), (
            "the 2025 consolation winner's fees are waived in 2026, not 2025"
        )

    def test_an_unwaived_team_still_owes_the_tier(self, data_dir):
        write(
            data_dir / "history" / "2025.json",
            {
                "season": 2025,
                "roster_at_declaration": _declared(2025, 11, 12),
                "claims": [_claim(2025, 11), _claim(2025, 12, "K2")],
            },
        )
        report = validate.run()
        assert any("fee_total_mismatch" in m for m in report.errors)


class TestTheRookieRuleIsEnforcedThroughRepeatClaims:
    """A prospect must be a rookie, and a repeat claim is the form of that rule derivable from
    the league's own records: a player has exactly one rookie season, so if he was a prospect
    before, he is not a rookie now.

    Since 2026-08-01 it is no longer the *only* form — ESPN's draft class checks rule 1
    directly — but it remains the independent cross-check, and the one that blocks.

    Both are applied only from the season the rule tightened. The league previously allowed
    second-year prospects, and the record holds one — Tyjae Spears, 2024 and 2025, legally.
    """

    def _season(self, year: int, claims: list[dict]) -> dict:
        return {
            "season": year,
            "roster_at_declaration": _declared(year, *[c["espn_player_id"] for c in claims]),
            "claims": claims,
        }

    def test_a_repeat_prospect_is_rejected_under_the_current_rule(self, data_dir):
        write(data_dir / "history" / "2025.json", self._season(2025, [_claim(2025, 11, "PROSPECT")]))
        write(data_dir / "history" / "2026.json", self._season(2026, [_claim(2026, 11, "PROSPECT")]))
        report = validate.run()
        assert any("prospect_repeat_claim" in m for m in report.errors)

    def test_the_old_rule_is_not_applied_retroactively(self, data_dir):
        """Spears' shape. Both claims predate the tightening, so neither is an error — turning
        a correct historical record into a wall of errors is its own kind of wrong answer."""
        write(data_dir / "history" / "2024.json", self._season(2024, [_claim(2024, 11, "PROSPECT")]))
        write(data_dir / "history" / "2025.json", self._season(2025, [_claim(2025, 11, "PROSPECT")]))
        report = validate.run()
        assert not any("prospect_repeat_claim" in m for m in report.errors)

    def test_being_started_is_no_longer_a_prospect_problem(self, data_dir):
        """The never-started rule is retired: prospects may be started."""
        write(
            data_dir / "history" / "2024.json",
            {**self._season(2024, [_claim(2024, 11, "PROSPECT")]), "started_player_ids": [11]},
        )
        report = validate.run()
        assert report.errors == []
        assert not any("prospect" in m for m in report.reviews)


class TestGraduatingASeasonIntoHistory:
    """A season graduates by being frozen; clearing it out of the admin tool is a second,
    manual step. Between the two, the same claims exist in both files."""

    def test_the_frozen_copy_wins_when_the_two_disagree(self, data_dir):
        """The hazard is not duplication, it is DIVERGENCE. Both copies feed the ratchet
        audit, so a manual row edited after the season was frozen would silently override the
        frozen record and change what the audit compares against."""
        frozen = dict(_claim(2025, 11), computed_salary=10)
        edited = dict(_claim(2025, 11), computed_salary=99)
        write(
            data_dir / "history" / "2025.json",
            {
                "season": 2025,
                "roster_at_declaration": _declared(2025, 11),
                "claims": [frozen],
            },
        )
        write(data_dir / "manual" / "claims.json", {"seasons": {"2025": [edited]}})
        write(
            data_dir / "derived" / "2026.json",
            {
                "season": 2026,
                "source": {"base_salary_field": "keeperValue", "drafted": False},
                "franchises": [{"manager_id": "t1", "season": 2026, "name": "Fake News"}],
                "players": [
                    {"espn_player_id": 11, "name": "P11", "position": "WR", "nfl_team": "BUF"}
                ],
                "roster": [dict(_declared(2026, 11)[0], kept_prior_year=True)],
            },
        )
        report = validate.run()
        assert not [m for m in report.reviews if "base continuity" in m], (
            "the edited manual copy overrode the frozen record in the audit"
        )
        assert any("already frozen" in m for m in report.reviews)

    def test_an_ungraduated_season_is_still_validated_from_manual(self, data_dir):
        write(
            data_dir / "manual" / "claims.json",
            {"seasons": {"2026": [_claim(2026, 11), _claim(2026, 12, "K2")]}},
        )
        # The current season is validated against its pre-draft derived file, which carries
        # last season's roster forward — the pool its keepers were declared from.
        write(
            data_dir / "derived" / "2026.json",
            {
                "season": 2026,
                "source": {"base_salary_field": "keeperValue", "drafted": False},
                "franchises": [{"manager_id": "t1", "season": 2026, "name": "Fake News"}],
                "players": [
                    {"espn_player_id": pid, "name": f"P{pid}", "position": "WR",
                     "nfl_team": "BUF"}
                    for pid in (11, 12)
                ],
                "roster": _declared(2026, 11, 12),
            },
        )
        report = validate.run()
        # Two keepers owe $5 and none was allocated, so the tier check must still fire.
        assert any("fee_total_mismatch" in m for m in report.errors)


class TestStatsChecks:
    def _stats_doc(self, **overrides):
        doc = {
            "season": 2025,
            "source": {"regular_season_weeks": 1, "weeks_with_results": [1]},
            "standings": [{"manager_id": "t1"}],
            "weekly_high_scores": [{"week": 1, "manager_ids": ["t1"]}],
            "positional_studs": [],
            "survivor": {"eliminations": [], "winner_manager_ids": ["t1"]},
            "payouts": [],
            "review": {"issues": [], "warnings": []},
        }
        doc.update(overrides)
        return doc

    def test_a_stats_file_naming_an_unknown_franchise_is_an_error(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        write(
            data_dir / "derived" / "2025-stats.json",
            self._stats_doc(weekly_high_scores=[{"week": 1, "manager_ids": ["t42"]}]),
        )
        report = validate.run()
        assert any("t42" in message for message in report.errors)

    def test_recorded_review_issues_are_resurfaced_not_left_in_the_file(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        write(
            data_dir / "derived" / "2025-stats.json",
            self._stats_doc(
                review={
                    "issues": [
                        {
                            "code": "consolation_seats_mismatch",
                            "severity": "review",
                            "message": "the consolation ladder is not the non-playoff teams",
                        }
                    ],
                    "warnings": [],
                }
            ),
        )
        report = validate.run()
        assert any("not the non-playoff teams" in m for m in report.reviews)
        assert report.errors == []

    def test_a_recorded_error_blocks(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        write(
            data_dir / "derived" / "2025-stats.json",
            self._stats_doc(
                review={
                    "issues": [
                        {"code": "missing_week", "severity": "error", "message": "week 3 gone"}
                    ],
                    "warnings": [],
                }
            ),
        )
        report = validate.run()
        assert any("week 3 gone" in message for message in report.errors)

    def test_payouts_are_checked_against_the_recorded_prize_structure(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        write(
            data_dir / "manual" / "payouts.json",
            {
                "seasons": {
                    "2025": {
                        "champion": 500,
                        "second": 200,
                        "third": 100,
                        "most_points": 100,
                        "survivor": 40,
                        "stud": 25,
                        "unlucky": 20,
                        "weekly_high": 10,
                    }
                }
            },
        )
        write(
            data_dir / "derived" / "2025-stats.json",
            self._stats_doc(
                payouts=[{"label": "Champion", "amount": 500, "winner_manager_id": "t1"}]
            ),
        )
        report = validate.run()
        assert any("payouts total $500" in message for message in report.errors)


class TestExitCodes:
    def test_errors_fail_the_run(self, data_dir, capsys):
        doc = json.loads(json_dumps(SEASON))
        doc["roster"][0]["manager_id"] = "t99"
        write(data_dir / "derived" / "2025.json", doc)
        assert validate.main([]) == 1

    def test_reviews_alone_do_not_block_but_are_announced(self, data_dir, capsys):
        write(data_dir / "derived" / "2025.json", SEASON)
        write(
            data_dir / "derived" / "2025-stats.json",
            {
                "season": 2025,
                "source": {"regular_season_weeks": 1},
                "payouts": [],
                "review": {
                    "issues": [
                        {"code": "tie_split", "severity": "review", "message": "a two-way tie"}
                    ],
                    "warnings": [],
                },
            },
        )
        assert validate.main([]) == 0
        printed = capsys.readouterr().out
        assert "have NOT been checked" in printed

    def test_strict_makes_reviews_blocking(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        write(
            data_dir / "derived" / "2025-stats.json",
            {
                "season": 2025,
                "source": {"regular_season_weeks": 1},
                "payouts": [],
                "review": {
                    "issues": [
                        {"code": "tie_split", "severity": "review", "message": "a two-way tie"}
                    ],
                    "warnings": [],
                },
            },
        )
        assert validate.main(["--strict"]) == 1

    def test_a_clean_run_passes(self, data_dir):
        write(data_dir / "derived" / "2025.json", SEASON)
        assert validate.main([]) == 0
