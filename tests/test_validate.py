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
        assert any("prospects.json is missing" in message for message in report.skipped)
        assert any("payouts.json is missing" in message for message in report.skipped)


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
                            "code": "consolation_winner_unconfirmed",
                            "severity": "review",
                            "message": "confirm the consolation bracket",
                        }
                    ],
                    "warnings": [],
                }
            ),
        )
        report = validate.run()
        assert any("confirm the consolation bracket" in m for m in report.reviews)
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
