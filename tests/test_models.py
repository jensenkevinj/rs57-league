"""Models enforce types. Rules live in keeper_rules — see test_keeper_rules.py."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from conftest import claim, entry
from rs57.models import KeeperSlot, Manager, Payout, Season, dump_json, json_dumps


def test_unknown_key_is_rejected():
    """Schema drift should fail loudly at load, not silently produce a None."""
    with pytest.raises(ValidationError, match="Extra inputs"):
        Manager(id="m1", display_name="Kevin", espn_team_id=1, emial="typo@example.com")


def test_models_are_frozen():
    manager = Manager(id="m1", display_name="Kevin", espn_team_id=1)
    with pytest.raises(ValidationError):
        manager.display_name = "someone else"


def test_money_rejects_floats():
    """Without strict=True, Pydantic coerces 12.0 -> 12 and only chokes on 12.50, so whole
    dollars from a spreadsheet import would sneak through and cents would explode."""
    with pytest.raises(ValidationError):
        Payout(season=2025, label="champion", amount=425.0)
    with pytest.raises(ValidationError):
        Payout(season=2025, label="champion", amount=425.5)


def test_money_rejects_negative_amounts():
    with pytest.raises(ValidationError):
        Payout(season=2025, label="champion", amount=-425)


def test_fee_allocated_accepts_negative():
    """A negative fee is a rule violation, not a type error. The engine reports it as
    NEGATIVE_FEE alongside every other rule problem instead of raising from a JSON load."""
    assert claim(1, KeeperSlot.K1, fee=-5).fee_allocated == -5


def test_consolation_winner_is_that_seasons_winner():
    """Documented here because reading it off by one year silently misapplies every waiver."""
    season = Season(year=2024, consolation_winner_id="m10")
    assert season.consolation_winner_id == "m10"
    # The waiver from this record applies to 2025 keeper claims.


def test_json_dumps_is_deterministic():
    payout = Payout(season=2025, label="survivor", amount=100, winner_manager_id="m3")
    first = json_dumps(payout)
    second = json_dumps(payout)
    assert first == second
    assert first.endswith("\n")

    keys = list(json.loads(first).keys())
    assert keys == sorted(keys), "keys must be sorted or every nightly diff looks total"


def test_json_dumps_handles_collections_of_models():
    payouts = [
        Payout(season=2025, label="champion", amount=425),
        Payout(season=2025, label="runner up", amount=100),
    ]
    loaded = json.loads(json_dumps(payouts))
    assert [item["label"] for item in loaded] == ["champion", "runner up"]


def test_dump_json_round_trips_byte_identically(tmp_path):
    roster = [entry(1, 25), entry(2, 0, kept=True)]
    path = tmp_path / "roster.json"
    dump_json(roster, path)

    written = path.read_text(encoding="utf-8")
    assert written == json_dumps(roster)

    reloaded = json.loads(written)
    assert reloaded[1]["kept_prior_year"] is True
    assert datetime.fromisoformat(reloaded[0]["acquired_at"])
