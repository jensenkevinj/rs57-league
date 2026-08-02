"""``data/derived/player-origins.json`` — the file, the merge, and the ways it must not fail.

The invariant every test here defends, stated once: **no absence of data may make an
ineligible prospect look eligible, and no outage may make an eligible one look ineligible.**

The merge is where that lives. It only ever adds, so a run that reaches ESPN for half the
league leaves the other half exactly as it was; and it refuses to adopt a changed value,
because a draft class is immutable and a change means drift, not news.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rs57.espn import EspnError
from rs57.models import EXACT_SOURCES, PlayerOrigin, dump_json
from rs57.origins import (
    OriginsError,
    empty_document,
    load_first_season_bounds,
    load_player_origins,
    merge,
    origins_path,
    unresolved_ids,
    warnings,
)
from rs57.origins_sync import claimed_player_ids, roster_player_ids, sync_origins

SKATTEBO = 4696981
NACUA = 4426515
JAWHAR_JORDAN = 4429939
WARREN = 4569987
RAVENS_DST = -16033


class FakeCore:
    """A core client that answers from a dict. ``None`` is a 404; an id in ``down`` raises."""

    def __init__(self, answers: dict[int, dict], down: set[int] | None = None):
        self._answers = answers
        self._down = down or set()
        self.asked: list[int] = []

    def fetch_athlete(self, espn_player_id: int, season: int):
        self.asked.append(espn_player_id)
        if espn_player_id in self._down:
            raise EspnError(f"could not reach ESPN for athlete {espn_player_id}")
        return self._answers.get(espn_player_id)


def drafted(year: int) -> dict:
    return {"draft": {"year": year}}


def keeper_doc(season: int, player_ids: list[int]) -> dict:
    """A derived season file, cut down to what ``roster_player_ids`` reads."""
    return {
        "season": season,
        "source": {"drafted": False, "base_salary_field": "keeperValue"},
        "franchises": [],
        "players": [],
        "roster": [
            {
                "season": season,
                "manager_id": "t1",
                "espn_player_id": player_id,
                "acquired_at": "2025-08-05T12:00:00",
                "base_salary": 5,
                "kept_prior_year": False,
                "source": "draft",
            }
            for player_id in player_ids
        ],
        "review": {"waiver_bases_verified": 0, "waiver_base_mismatches": [], "warnings": []},
    }


@pytest.fixture
def derived(tmp_path: Path) -> Path:
    out = tmp_path / "derived"
    out.mkdir()
    (out / "2026.json").write_text(
        json.dumps(keeper_doc(2026, [SKATTEBO, NACUA, JAWHAR_JORDAN, RAVENS_DST])),
        encoding="utf-8",
    )
    return out


# ---------------------------------------------------------------------------
# The three states, expressed in the schema
# ---------------------------------------------------------------------------


def test_an_unknown_player_is_absent_rather_than_null():
    """``first_nfl_season`` is required, so the file cannot say "unknown" with a value.

    That is the whole three-state model: a resolved player is a row, an unresolved one is in
    ``unresolved``, and a never-fetched one is in neither. A nullable field would collapse the
    last two into one and no reader could tell "ESPN has nothing" from "nobody asked".
    """
    with pytest.raises(Exception):
        PlayerOrigin(espn_player_id=1, first_nfl_season=None, source="draft_year")

    origin = PlayerOrigin(espn_player_id=1, first_nfl_season=2025, source="draft_year")
    assert origin.first_nfl_season == 2025


def test_a_missing_file_reads_as_nothing_known_not_as_rule_off(tmp_path: Path):
    """An empty mapping means "the rule applies and nothing is known" — never "skip the rule"."""
    assert load_player_origins(tmp_path) == {}
    assert unresolved_ids(tmp_path) == set()


# ---------------------------------------------------------------------------
# The merge: it adds, and it never adopts a changed value
# ---------------------------------------------------------------------------


def test_merge_adds_without_disturbing_what_is_on_record():
    existing = merge(empty_document(), {NACUA: (2023, "draft_year")}, [])
    later = merge(existing, {SKATTEBO: (2025, "draft_year")}, [])

    seasons = {row["espn_player_id"]: row["first_nfl_season"] for row in later["players"]}
    assert seasons == {NACUA: 2023, SKATTEBO: 2025}
    assert later["review"]["warnings"] == []


def test_a_changed_draft_year_is_reported_and_not_adopted():
    """A draft class is immutable. A different answer is drift or a bug, never an update.

    Adopting it would reprice a keeper on the strength of whichever run happened to be last,
    silently. The recorded value stands and the disagreement goes where a human will read it.
    """
    existing = merge(empty_document(), {NACUA: (2023, "draft_year")}, [])
    later = merge(existing, {NACUA: (2019, "draft_year")}, [])

    seasons = {row["espn_player_id"]: row["first_nfl_season"] for row in later["players"]}
    assert seasons[NACUA] == 2023, "the changed value was adopted"
    assert len(later["review"]["warnings"]) == 1
    assert "2019" in later["review"]["warnings"][0]
    assert "does not change" in later["review"]["warnings"][0]


def test_a_merge_that_would_drop_a_player_refuses():
    """The guard behind "merge-only": the output is never smaller than the input.

    Reached here through a genuinely corrupt input — a file holding the same player twice,
    which the id-keyed merge would silently collapse. Whatever the cause, a merge that comes
    out smaller than it went in is a replace wearing a merge's name, and the run that noticed
    should stop rather than publish the shorter league.
    """
    existing = merge(empty_document(), {NACUA: (2023, "draft_year")}, [])
    duplicated = dict(existing)
    duplicated["players"] = existing["players"] + existing["players"]

    with pytest.raises(OriginsError, match="never removes"):
        merge(duplicated, {}, [])


def test_resolving_a_player_clears_him_from_unresolved():
    """ESPN backfills ``debutYear``, so an unresolved id is retried and can become known."""
    existing = merge(empty_document(), {}, [WARREN])
    assert [row["espn_player_id"] for row in existing["unresolved"]] == [WARREN]

    later = merge(existing, {WARREN: (2021, "debut_year")}, [])
    assert later["unresolved"] == []
    assert {row["espn_player_id"] for row in later["players"]} == {WARREN}


# ---------------------------------------------------------------------------
# The sync: what it asks for, and what it refuses to write
# ---------------------------------------------------------------------------


def test_def_ids_are_never_fetched(derived: Path):
    """D/ST ids are negative and 404 by construction, and a D/ST is never a rookie.

    Asking anyway would fill ``unresolved`` with a dozen meaningless rows every run and blunt
    the one reading that matters: ESPN knows him but not when he started.
    """
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=client, write=False)

    assert RAVENS_DST not in client.asked
    assert all(player_id > 0 for player_id in client.asked)


def test_a_player_already_on_record_is_never_asked_again(derived: Path):
    """A draft class is immutable, so steady state is a handful of waiver adds, not the league."""
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    document, *_ = sync_origins(2026, derived_dir=derived, client=client, write=True)
    first_pass = list(client.asked)
    assert len(first_pass) == 3

    again = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=again, write=True)
    assert again.asked == [], "a recorded player was fetched a second time"


def test_the_written_file_is_idempotent(derived: Path):
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=client, write=True)
    once = origins_path(derived).read_text()

    sync_origins(2026, derived_dir=derived, client=FakeCore({}), write=True)
    assert origins_path(derived).read_text() == once


def test_a_404_is_recorded_as_unresolved_not_as_an_outage(derived: Path):
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023)})  # Jordan answers None
    document, resolved, unresolved, _ = sync_origins(
        2026, derived_dir=derived, client=client, write=True
    )
    assert resolved == sorted([SKATTEBO, NACUA])
    assert unresolved == [JAWHAR_JORDAN]
    assert load_player_origins(derived) == {SKATTEBO: 2025, NACUA: 2023}
    assert JAWHAR_JORDAN not in load_player_origins(derived)


def test_an_unreachable_core_api_keeps_every_known_player(derived: Path):
    """The outage story. Nothing is written and everything already known stays known."""
    good = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=good, write=True)
    before = origins_path(derived).read_text()

    # A new player joins the roster and ESPN is unreachable when we go to ask about him. The
    # three already on record are not re-fetched, so the outage has to arrive through him.
    (derived / "2026.json").write_text(
        json.dumps(keeper_doc(2026, [SKATTEBO, NACUA, JAWHAR_JORDAN, 999999])), encoding="utf-8"
    )
    down = FakeCore({}, down={999999})
    with pytest.raises(EspnError):
        sync_origins(2026, derived_dir=derived, client=down, write=True)

    assert origins_path(derived).read_text() == before, "an outage rewrote the file"
    assert load_player_origins(derived) == {SKATTEBO: 2025, NACUA: 2023, JAWHAR_JORDAN: 2024}


def test_a_run_that_resolves_nothing_writes_nothing(derived: Path, tmp_path: Path):
    """Resolving zero of a whole league is a changed API, not a league without draft classes.

    Writing it would mark every prospect unverified in one quiet commit. Same reasoning as
    ``espn.MIN_ROSTER_SIZE``.
    """
    many = list(range(1000, 1000 + 15))
    (derived / "2026.json").write_text(json.dumps(keeper_doc(2026, many)), encoding="utf-8")

    with pytest.raises(EspnError, match="degraded"):
        sync_origins(2026, derived_dir=derived, client=FakeCore({}), write=True)
    assert not origins_path(derived).exists()


def test_retrying_known_unresolved_players_is_not_a_degraded_run(derived: Path):
    """The steady state, and the bug that only showed up on the second real run.

    After the first sync, most of what a run asks about is players ESPN has already said it
    has nothing for — they are retried because ESPN backfills ``debutYear``. They resolve
    nothing by definition. Count those toward the degraded-response guard and the nightly
    fails every single night from the second one onward.
    """
    many = list(range(1000, 1000 + 15))
    (derived / "2026.json").write_text(json.dumps(keeper_doc(2026, many)), encoding="utf-8")

    # First run: ESPN answers 404 for all fifteen. Below the guard only because none resolved
    # AND they were all new — so it must raise here.
    with pytest.raises(EspnError, match="degraded"):
        sync_origins(2026, derived_dir=derived, client=FakeCore({}), write=True)

    # Seed them as known-unresolved, the state the first successful run would have left.
    seeded = merge(empty_document(), {9: (2020, "draft_year")}, many)
    dump_json(seeded, origins_path(derived))

    # Now the same fruitless run is the expected steady state, not a symptom.
    document, resolved, unresolved, attempted = sync_origins(
        2026, derived_dir=derived, client=FakeCore({}), write=True
    )
    assert attempted == 15 and resolved == []
    assert sorted(unresolved) == many
    assert load_player_origins(derived) == {9: 2020}, "the retry run lost a recorded player"


def test_a_small_run_resolving_nothing_is_allowed(derived: Path):
    """Below the threshold, "resolved none" really can just mean "nobody had a draft class"."""
    (derived / "2026.json").write_text(json.dumps(keeper_doc(2026, [111, 222])), encoding="utf-8")
    document, resolved, unresolved, attempted = sync_origins(
        2026, derived_dir=derived, client=FakeCore({}), write=True
    )
    assert attempted == 2 and resolved == []
    assert unresolved == [111, 222]


def test_dry_run_writes_nothing(derived: Path):
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=client, write=False)
    assert not origins_path(derived).exists()
    assert client.asked, "a dry run should still fetch, so it can report what it found"


def test_warnings_are_readable_back_off_the_file(derived: Path):
    """A warning written into a derived file and never read is the same as no warning."""
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=client, write=True)
    assert warnings(derived) == []

    document = json.loads(origins_path(derived).read_text())
    document["review"]["warnings"] = ["something needs a human"]
    dump_json(document, origins_path(derived))
    assert warnings(derived) == ["something needs a human"]


def test_all_sweeps_claims_that_are_no_longer_rostered(tmp_path: Path):
    """A prospect claimed in a frozen season may be off every roster, and still needs auditing."""
    data = tmp_path / "data"
    (data / "derived").mkdir(parents=True)
    (data / "history").mkdir()
    (data / "manual").mkdir()
    (data / "derived" / "2026.json").write_text(json.dumps(keeper_doc(2026, [SKATTEBO])), encoding="utf-8")
    (data / "history" / "2024.json").write_text(
        json.dumps(
            {
                "season": 2024,
                "claims": [
                    {
                        "season": 2024,
                        "manager_id": "t1",
                        "espn_player_id": 4428557,
                        "slot": "PROSPECT",
                        "fee_allocated": 0,
                        "computed_salary": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert claimed_player_ids(data) == {4428557}
    assert roster_player_ids(data / "derived") == {SKATTEBO}

    client = FakeCore({SKATTEBO: drafted(2025), 4428557: drafted(2023)})
    sync_origins(
        2026, derived_dir=data / "derived", data_dir=data, client=client, every_season=True, write=True
    )
    assert load_player_origins(data / "derived") == {SKATTEBO: 2025, 4428557: 2023}


def test_the_origins_file_is_not_read_as_a_season(derived: Path):
    """``validate._season_files`` used to glob every ``*.json`` in ``data/derived/``."""
    from rs57.validate import _season_files

    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=client, write=True)
    assert origins_path(derived).exists()

    names = [path.name for path in _season_files(derived)]
    assert names == ["2026.json"], f"a non-season file was read as a season: {names}"


# ---------------------------------------------------------------------------
# The statistics-log bound: what it may and may not conclude
# ---------------------------------------------------------------------------


def test_a_bounded_source_is_kept_out_of_the_exact_mapping():
    """The whole point of the split. ``load_player_origins`` may say somebody IS a rookie.

    The statistics log runs a season late for a player who was rostered without recording
    anything — measured against the league it agreed with the draft class 159 times of 162,
    and all three misses were late by one. Letting a late value decide eligibility would make
    a second-year player prospect-eligible, which costs real money.
    """
    doc = merge(
        empty_document(),
        {SKATTEBO: (2025, "draft_year"), WARREN: (2022, "first_stats_season")},
        [],
    )
    origins = [PlayerOrigin.model_validate(row) for row in doc["players"]]
    exact = {o.espn_player_id for o in origins if o.exact}
    assert exact == {SKATTEBO}, "a bounded row leaked into the exact set"
    assert {o.espn_player_id for o in origins} == {SKATTEBO, WARREN}


def test_the_two_mappings_split_exact_from_bounded(derived: Path):
    client = FakeCore({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: drafted(2024)})
    sync_origins(2026, derived_dir=derived, client=client, write=True)

    document = json.loads(origins_path(derived).read_text())
    document["players"].append(
        {"espn_player_id": WARREN, "first_nfl_season": 2022, "source": "first_stats_season"}
    )
    dump_json(document, origins_path(derived))

    exact = load_player_origins(derived)
    bounds = load_first_season_bounds(derived)
    assert WARREN not in exact, "a bound must never be able to prove somebody is a rookie"
    assert bounds[WARREN] == 2022, "a bound must still be available to rule him out"
    assert set(exact) < set(bounds), "bounds are a superset of the exact answers"


def test_the_stats_log_is_only_asked_when_nothing_states_a_first_season(derived: Path):
    """It is a fallback, not a source of truth. A drafted player never reaches it."""

    class Counting(FakeCore):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.logs_asked: list[int] = []

        def fetch_first_stats_season(self, espn_player_id: int):
            self.logs_asked.append(espn_player_id)
            return 2022

    client = Counting({SKATTEBO: drafted(2025), NACUA: drafted(2023), JAWHAR_JORDAN: {}})
    sync_origins(2026, derived_dir=derived, client=client, write=True)

    assert client.logs_asked == [JAWHAR_JORDAN], (
        "the statistics log was fetched for a player who already had a draft class"
    )
    document = json.loads(origins_path(derived).read_text())
    sources = {row["espn_player_id"]: row["source"] for row in document["players"]}
    assert sources[SKATTEBO] == "draft_year"
    assert sources[JAWHAR_JORDAN] == "first_stats_season"
