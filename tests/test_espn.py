"""The ESPN mapping, and above all the ``keeperValue`` / ``keeperValueFuture`` question.

Everything here replays payloads recorded from the live league into ``tests/data/`` — real
ESPN responses, trimmed to the fields the mapper reads. CI never touches the network, and the
recordings are what stop the field semantics from drifting back.

**That trimming cost the project a wrong conclusion, so read this before adding a fixture.**
``espn_2025.json``'s player objects were trimmed from ESPN's eighteen keys down to the four
the mapper reads. A later session read the fixture as if it were the API and concluded, in six
places, that ESPN carries no rookie year — and built the prospect rules around the gap. It
does carry one, on a different host. A trimmed fixture is evidence about the mapper, never
about the API. ``espn_athletes.json`` is therefore recorded **whole**.

The reasoning behind the field choice is in ``docs/espn-field-semantics.md``. These tests are
the executable half of it.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from rs57.espn import (
    CORE_HOST,
    AthleteNotFound,
    HOST,
    LEAGUE_SIZE,
    AcquisitionSource,
    EspnCoreClient,
    EspnError,
    Position,
    acquisition_source,
    base_salary_field,
    bid_season_for,
    build_season,
    first_nfl_season,
    keeper_pick_ids,
    winning_bids,
)
from rs57.models import dump_json, json_dumps
from rs57.sync import season_document

DATA = Path(__file__).parent / "data"

# The handoff note's three known-history players. All three read the same under either field,
# which is exactly why they could not settle the question on their own — see
# test_wrong_field_is_caught for the players that do settle it.
KNOWN_SALARIES = {4426515: 5, 4426502: 24, 4596448: 7}  # Nacua, London, Irving


class ReplayClient:
    """Stands in for ``EspnClient`` against a recorded season. Same surface, no network."""

    def __init__(self, year: int, doc: dict | None = None):
        self.year = year
        self._doc = doc if doc is not None else json.loads((DATA / f"espn_{year}.json").read_text())

    def fetch_league(self):
        return self._doc["league"]

    def fetch_roster(self, team_id: int):
        return self._doc["rosters"][str(team_id)]

    def fetch_draft_detail(self):
        return self._doc["draft"]

    def fetch_pro_teams(self):
        return {int(k): v for k, v in self._doc["pro_teams"].items()}


@pytest.fixture
def doc_2025():
    return json.loads((DATA / "espn_2025.json").read_text())


@pytest.fixture
def doc_2026():
    return json.loads((DATA / "espn_2026.json").read_text())


@pytest.fixture
def faab_2025():
    """Winning FAAB bids from the real 2025 transaction log."""
    return winning_bids(json.loads((DATA / "espn_transactions_2025.json").read_text()))


@pytest.fixture
def season_2026(doc_2025):
    """2026 as the pipeline actually builds it: undrafted, taxed off 2025's keeper picks."""
    return build_season(
        ReplayClient(2026), prior_keeper_ids=keeper_pick_ids(doc_2025["draft"])
    )


@pytest.fixture
def season_2025():
    return build_season(ReplayClient(2025))


def bases(season) -> dict[int, int]:
    return {entry.espn_player_id: entry.base_salary for entry in season.roster}


# --------------------------------------------------------------------------------------
# The field question
# --------------------------------------------------------------------------------------


def test_field_choice_follows_the_draft_not_the_calendar():
    """Before a season's auction the live number is what carried in; after, it is what was paid."""
    assert base_salary_field(drafted=False) == "keeperValue"
    assert base_salary_field(drafted=True) == "keeperValueFuture"


def test_recordings_are_the_two_states(doc_2025, doc_2026):
    """The recordings cover both sides of the switch, or the tests below prove nothing."""
    assert doc_2025["draft"]["drafted"] is True
    assert doc_2026["draft"]["drafted"] is False


def test_undrafted_season_reads_keeper_value(season_2026):
    assert season_2026.drafted is False
    assert season_2026.base_field == "keeperValue"
    for player_id, expected in KNOWN_SALARIES.items():
        assert bases(season_2026)[player_id] == expected


def test_drafted_season_reads_keeper_value_future(season_2025):
    assert season_2025.drafted is True
    assert season_2025.base_field == "keeperValueFuture"
    for player_id, expected in KNOWN_SALARIES.items():
        assert bases(season_2025)[player_id] == expected


def test_wrong_field_is_caught(doc_2025, doc_2026):
    """The regression that matters: picking the wrong field must not look plausible.

    Read an undrafted season through ``keeperValueFuture`` and every base collapses to $0 —
    nothing has been paid in that season yet. Read a drafted season through ``keeperValue``
    and the bases silently become *last* season's, which is the failure that compounds for
    years instead of failing loudly. Tyreek Hill is the witness: $60 carried in, $30 paid.
    """
    entries_2026 = [
        e
        for roster in doc_2026["rosters"].values()
        for e in roster["teams"][0]["roster"]["entries"]
    ]
    assert all(e["playerPoolEntry"]["keeperValueFuture"] == 0 for e in entries_2026)
    assert any(e["playerPoolEntry"]["keeperValue"] > 0 for e in entries_2026)

    hill = next(
        e
        for roster in doc_2025["rosters"].values()
        for e in roster["teams"][0]["roster"]["entries"]
        if e["playerPoolEntry"]["player"]["fullName"] == "Tyreek Hill"
    )
    assert hill["playerPoolEntry"]["keeperValueFuture"] == 30  # paid in 2025 — the base
    assert hill["playerPoolEntry"]["keeperValue"] == 60  # carried in from 2024 — not the base


def test_the_two_fields_are_one_number_across_adjacent_seasons(doc_2025, doc_2026):
    """Season Y's ``keeperValue`` is season Y-1's ``keeperValueFuture``, player for player.

    This identity is what lets the ratchet work at all: reading 2026 today yields each
    player's 2025 salary, which is precisely the base next season's price is built on. If it
    ever stops holding, ESPN has changed the semantics again and the sync must be re-derived.
    """

    def by_id(doc, field):
        return {
            e["playerPoolEntry"]["player"]["id"]: e["playerPoolEntry"][field]
            for roster in doc["rosters"].values()
            for e in roster["teams"][0]["roster"]["entries"]
        }

    carried_in_2026 = by_id(doc_2026, "keeperValue")
    paid_in_2025 = by_id(doc_2025, "keeperValueFuture")
    assert carried_in_2026 == paid_in_2025
    # Not a coincidence of equal values: the fields genuinely disagree for most of the league.
    disagree = sum(1 for k, v in by_id(doc_2025, "keeperValue").items() if paid_in_2025[k] != v)
    assert disagree > 100


def test_keeper_value_future_is_what_the_auction_charged(doc_2025):
    """Ground truth: an auction league records the actual price on every pick.

    ``keeperValueFuture`` matches ``bidAmount`` for the overwhelming majority; ``keeperValue``
    matches only where it happens to equal it. The rows that miss are drafted players later
    dropped and re-added, whose base correctly reset to the new waiver value.
    """
    bid = {p["playerId"]: p["bidAmount"] for p in doc_2025["draft"]["picks"]}
    pairs = [
        (bid[e["playerPoolEntry"]["player"]["id"]], e["playerPoolEntry"], e["acquisitionType"])
        for roster in doc_2025["rosters"].values()
        for e in roster["teams"][0]["roster"]["entries"]
        if e["playerPoolEntry"]["player"]["id"] in bid
    ]
    future_hits = sum(1 for b, ppe, _ in pairs if b == ppe["keeperValueFuture"])
    value_hits = sum(1 for b, ppe, _ in pairs if b == ppe["keeperValue"])
    assert future_hits > value_hits * 3

    # Restricted to rows where the fields disagree, keeperValue never matches the real price.
    disagreeing = [(b, ppe) for b, ppe, _ in pairs if ppe["keeperValue"] != ppe["keeperValueFuture"]]
    assert sum(1 for b, ppe in disagreeing if b == ppe["keeperValue"]) == 0
    assert sum(1 for b, ppe in disagreeing if b == ppe["keeperValueFuture"]) > 50

    # Every miss is a player who came back through the wire, not a bad field.
    for b, ppe, acq in pairs:
        if b != ppe["keeperValueFuture"]:
            assert acq == "ADD"


def test_keeper_value_future_is_not_a_projection(doc_2025):
    """Rules out the 2022-era reading, where ``keeperValueFuture`` looked like a $1-floored
    market projection. A projection cannot be $0, and cannot be $0 for exactly the players
    who were picked up free."""
    by_acq: dict[str, list[int]] = {}
    for roster in doc_2025["rosters"].values():
        for e in roster["teams"][0]["roster"]["entries"]:
            by_acq.setdefault(e["acquisitionType"], []).append(
                e["playerPoolEntry"]["keeperValueFuture"]
            )
    assert min(by_acq["DRAFT"]) >= 1  # nobody wins an auction player for $0
    assert by_acq["ADD"].count(0) > 50  # free waiver claims, priced honestly at $0


# --------------------------------------------------------------------------------------
# The tax, derived from data rather than a list of names
# --------------------------------------------------------------------------------------


def test_kept_prior_year_comes_from_last_seasons_keeper_picks(season_2026, doc_2025):
    kept = {e.espn_player_id for e in season_2026.roster if e.kept_prior_year}
    picks = keeper_pick_ids(doc_2025["draft"])
    assert kept
    assert kept <= picks


def test_a_trade_does_not_clear_the_tax(season_2026, doc_2025):
    """``CLAUDE.md``: the tax is a property of the player's history, not of who holds him.

    Three of last season's keepers changed hands. All three stay taxed.
    """
    picks = keeper_pick_ids(doc_2025["draft"])
    traded = [
        e
        for e in season_2026.roster
        if e.espn_player_id in picks and e.source is AcquisitionSource.TRADE
    ]
    assert len(traded) == 3
    assert all(e.kept_prior_year for e in traded)


def test_a_drop_clears_the_tax(doc_2025, doc_2026):
    """A drop kills the tax, and the re-add sets a fresh base.

    No keeper was dropped and re-added between 2025 and 2026, so the case is constructed:
    take a player who *was* kept and put him back on the roster through the wire. The branch
    is worth holding onto — it is the asymmetry against trades, and the recording happening
    not to exercise it this year is luck, not proof.
    """
    picks = keeper_pick_ids(doc_2025["draft"])
    entry = next(
        e
        for roster in doc_2026["rosters"].values()
        for e in roster["teams"][0]["roster"]["entries"]
        if e["playerPoolEntry"]["player"]["id"] in picks
    )
    player_id = entry["playerPoolEntry"]["player"]["id"]

    taxed = build_season(ReplayClient(2026, doc_2026), prior_keeper_ids=picks)
    assert next(e for e in taxed.roster if e.espn_player_id == player_id).kept_prior_year

    entry["acquisitionType"] = "ADD"
    entry["playerPoolEntry"]["keeperValue"] = 0  # re-added off waivers, so the base resets too
    after = build_season(ReplayClient(2026, doc_2026), prior_keeper_ids=picks)
    re_added = next(e for e in after.roster if e.espn_player_id == player_id)
    assert re_added.kept_prior_year is False
    assert re_added.base_salary == 0


TYJAE_SPEARS = 4428557
"""Kept in the PROSPECT slot in 2025 (`Purdy Good at Fantasy`, P_Sal $3), per the workbook's
Fee Allocations tab. ESPN's draft record flags him `keeper: True` exactly like a K1/K2/K3."""


def test_a_prospect_keep_is_not_taxed(doc_2025):
    """`CLAUDE.md`: a prospect keep never sets the tax flag.

    ESPN's `keeperCount` is 4 — three keepers plus a prospect — and the draft pick carries no
    slot, so the keeper flag alone over-taxes every prospect. This was a real $5 error on
    Tyjae Spears, caught by the workbook diff, and the ratchet would have carried it forward
    for good.
    """
    picks = keeper_pick_ids(doc_2025["draft"])
    assert TYJAE_SPEARS in picks, "ESPN does flag the prospect as a keeper"

    taxed = build_season(ReplayClient(2026), prior_keeper_ids=picks)
    assert next(e for e in taxed.roster if e.espn_player_id == TYJAE_SPEARS).kept_prior_year

    fixed = build_season(
        ReplayClient(2026), prior_keeper_ids=picks, prior_prospect_ids={TYJAE_SPEARS}
    )
    entry = next(e for e in fixed.roster if e.espn_player_id == TYJAE_SPEARS)
    assert entry.kept_prior_year is False
    assert entry.base_salary == 3  # keeps his base; only the tax goes away


def test_unknown_prospects_warn_rather_than_silently_taxing(doc_2025):
    """Never let a REVIEW item pass silently as if it had been checked."""
    picks = keeper_pick_ids(doc_2025["draft"])
    unknown = build_season(ReplayClient(2026), prior_keeper_ids=picks)
    assert any("prospect keeps were not supplied" in w for w in unknown.warnings)

    known = build_season(
        ReplayClient(2026), prior_keeper_ids=picks, prior_prospect_ids={TYJAE_SPEARS}
    )
    assert not any("prospect" in w for w in known.warnings)


def test_matches_the_workbook_keeper_column(doc_2025):
    """The 30 players the `Keepers` tab marks Kept=TRUE, with the prospect excluded.

    This is the Phase 1 acceptance check. It found the prospect bug above, which is why the
    workbook diff was not the redundant formality it looked like.
    """
    season = build_season(
        ReplayClient(2026),
        prior_keeper_ids=keeper_pick_ids(doc_2025["draft"]),
        prior_prospect_ids={TYJAE_SPEARS},
    )
    names = {p.espn_player_id: p.name for p in season.players}
    taxed = {names[e.espn_player_id] for e in season.roster if e.kept_prior_year}
    workbook = {
        "Bucky Irving", "Puka Nacua", "Saquon Barkley", "Nico Collins", "Justin Jefferson",
        "Jaxon Smith-Njigba", "James Cook III", "Rashee Rice", "Xavier Worthy", "Malik Nabers",
        "Ja'Marr Chase", "Drake London", "Michael Pittman Jr.", "CeeDee Lamb", "Tee Higgins",
        "Brock Bowers", "Brian Thomas Jr.", "Jahmyr Gibbs", "Mike Evans", "Chuba Hubbard",
        "Trey McBride", "Chase Brown", "Ladd McConkey", "Jonathan Taylor", "Terry McLaurin",
        "Sam LaPorta", "Amon-Ra St. Brown", "A.J. Brown", "De'Von Achane",
    }
    # Jayden Daniels is Kept=TRUE in the workbook but was dropped after the sheet was last
    # written, so he is on nobody's roster now. A stale snapshot, not a disagreement.
    assert taxed == workbook


def test_no_prior_keepers_warns_rather_than_silently_untaxing():
    season = build_season(ReplayClient(2026), prior_keeper_ids=())
    assert not any(e.kept_prior_year for e in season.roster)
    assert any("kept_prior_year is False" in w for w in season.warnings)


def test_players_are_matched_on_id_not_name(season_2026):
    """The James Cook bug: ESPN renamed him ``James Cook III`` and a name-keyed list
    under-charged him $5. Ids are the join key, so a rename is inert."""
    cooks = [p for p in season_2026.players if p.name.startswith("James Cook")]
    assert len(cooks) == 1
    assert cooks[0].espn_player_id == 4379399
    assert cooks[0].espn_player_id in bases(season_2026)


# --------------------------------------------------------------------------------------
# Mapping and shape
# --------------------------------------------------------------------------------------


def test_acquisition_types_seen_live_all_map():
    assert acquisition_source("DRAFT") is AcquisitionSource.DRAFT
    assert acquisition_source("ADD") is AcquisitionSource.WAIVER
    assert acquisition_source("TRADE") is AcquisitionSource.TRADE
    with pytest.raises(EspnError, match="schema drift"):
        acquisition_source("SOMETHING_NEW")


def test_season_maps_onto_models(season_2026):
    assert len(season_2026.franchises) == LEAGUE_SIZE
    assert len(season_2026.roster) == 188
    assert {p.position for p in season_2026.players} <= set(Position)
    assert all(isinstance(e.base_salary, int) for e in season_2026.roster)
    assert all(e.season == 2026 for e in season_2026.roster)
    assert isinstance(season_2026.trade_deadline, datetime)


def test_franchise_names_are_whitespace_normalised(season_2026):
    """One team name carries a double space and has already leaked into the spreadsheets."""
    names = [f.name for f in season_2026.franchises]
    assert "Belichick's Spy" in names
    assert not any("  " in name for name in names)


def test_managers_are_keyed_on_espn_team_id(season_2026):
    assert {f.manager_id for f in season_2026.franchises} == {f"t{i}" for i in range(1, 13)}


def test_waiver_bases_are_verified_against_the_faab_actually_bid(doc_2025, faab_2025):
    """A waiver base has no other witness — the auction record only covers drafted players.

    Every one of the 80 waiver adds matches its winning FAAB bid, Tyrone Tracy Jr.'s $79
    included. That is the independent confirmation that the field is money paid.
    """
    season = build_season(
        ReplayClient(2026), prior_keeper_ids=keeper_pick_ids(doc_2025["draft"]), faab_bids=faab_2025
    )
    waivers = [e for e in season.roster if e.source is AcquisitionSource.WAIVER]
    assert len(waivers) == 80
    assert season.waiver_bases_verified == 80
    assert season.waiver_base_mismatches == ()
    assert not any("waiver" in w for w in season.warnings)
    assert faab_2025[4360516] == 79  # Tyrone Tracy Jr., a real bid


def test_a_waiver_base_that_contradicts_the_faab_record_is_caught(doc_2025, doc_2026, faab_2025):
    """The check has to be able to fail, or it is decoration.

    A wrong waiver base is the quiet kind of error: nothing rejects it, and the ratchet
    carries it into every season after.
    """
    entry = next(
        e
        for roster in doc_2026["rosters"].values()
        for e in roster["teams"][0]["roster"]["entries"]
        if e["acquisitionType"] == "ADD"
    )
    entry["playerPoolEntry"]["keeperValue"] += 7

    season = build_season(
        ReplayClient(2026, doc_2026),
        prior_keeper_ids=keeper_pick_ids(doc_2025["draft"]),
        faab_bids=faab_2025,
    )
    assert entry["playerPoolEntry"]["player"]["id"] in season.waiver_base_mismatches
    assert any("FAAB" in w for w in season.warnings)


def test_unchecked_waiver_bases_say_so(season_2026):
    """Without the FAAB record the bases are unverified, and must not read as checked."""
    assert season_2026.waiver_bases_verified == 0
    assert any("witness" in w for w in season_2026.warnings)


def test_only_executed_transactions_count():
    """A losing waiver claim is recorded too; counting it would invent money nobody spent."""
    losing = {
        "id": "x",
        "status": "FAILED_INVALID_PLAYER_SOURCE",
        "bidAmount": 99,
        "proposedDate": 2,
        "items": [{"type": "ADD", "playerId": 1}],
    }
    winning = {
        "id": "y",
        "status": "EXECUTED",
        "bidAmount": 4,
        "proposedDate": 1,
        "items": [{"type": "ADD", "playerId": 1}],
    }
    assert winning_bids([losing, winning]) == {1: 4}


def test_the_latest_add_sets_the_base():
    """Re-added later in the season? That is the add that priced him now."""
    first = {"id": "a", "status": "EXECUTED", "bidAmount": 3, "proposedDate": 100,
             "items": [{"type": "ADD", "playerId": 7}]}
    second = {"id": "b", "status": "EXECUTED", "bidAmount": 12, "proposedDate": 200,
              "items": [{"type": "ADD", "playerId": 7}]}
    assert winning_bids([first, second]) == {7: 12}
    assert winning_bids([second, first]) == {7: 12}


def test_bids_come_from_the_season_that_set_the_bases():
    """Same asymmetry as the field choice, for the same reason."""
    assert bid_season_for(2026, drafted=False) == 2025
    assert bid_season_for(2026, drafted=True) == 2026


# --------------------------------------------------------------------------------------
# Degraded responses must not write a season
# --------------------------------------------------------------------------------------


def test_short_roster_raises(doc_2026):
    doc_2026["rosters"]["3"]["teams"][0]["roster"]["entries"] = []
    with pytest.raises(EspnError, match="degraded"):
        build_season(ReplayClient(2026, doc_2026))


def test_missing_team_raises(doc_2026):
    doc_2026["league"]["teams"] = doc_2026["league"]["teams"][:11]
    with pytest.raises(EspnError, match="expected 12 teams"):
        build_season(ReplayClient(2026, doc_2026))


def test_unknown_position_raises(doc_2026):
    entry = doc_2026["rosters"]["1"]["teams"][0]["roster"]["entries"][0]
    entry["playerPoolEntry"]["player"]["defaultPositionId"] = 5  # a kicker, never rostered here
    with pytest.raises(EspnError, match="defaultPositionId"):
        build_season(ReplayClient(2026, doc_2026))


def test_missing_base_field_raises(doc_2026):
    entry = doc_2026["rosters"]["1"]["teams"][0]["roster"]["entries"][0]
    del entry["playerPoolEntry"]["keeperValue"]
    with pytest.raises(EspnError, match="keeperValue"):
        build_season(ReplayClient(2026, doc_2026))


# --------------------------------------------------------------------------------------
# The derived document
# --------------------------------------------------------------------------------------


def test_document_records_which_field_it_used(season_2026):
    doc = season_document(season_2026)
    assert doc["source"]["base_salary_field"] == "keeperValue"
    assert doc["source"]["drafted"] is False
    assert doc["season"] == 2026


def test_document_serialises_deterministically_and_without_a_timestamp(season_2026):
    doc = season_document(season_2026)
    once, twice = json_dumps(doc), json_dumps(doc)
    assert once == twice
    assert once.endswith("\n")
    assert "generated_at" not in once
    assert json.loads(once)["roster"][0]["base_salary"] is not None


def test_written_file_round_trips(season_2026, tmp_path):
    path = tmp_path / "2026.json"
    dump_json(season_document(season_2026), path)
    reloaded = json.loads(path.read_text())
    assert len(reloaded["roster"]) == len(season_2026.roster)
    assert reloaded["source"]["base_salary_field"] == "keeperValue"


# ---------------------------------------------------------------------------
# The core API: the draft class, and the field that must never be read
# ---------------------------------------------------------------------------

ATHLETES = json.loads((DATA / "espn_athletes.json").read_text())

SKATTEBO = 4696981  # drafted 2025 — the recorded 2026 prospect claim
NACUA = 4426515  # drafted 2023
TAYSOM_HILL = 2468609  # undrafted, debutYear 2017
JAWHAR_JORDAN = 4429939  # drafted 2024, experience.years == 1 — the trap
JAYLEN_WARREN = 4569987  # undrafted, no debutYear
RAVENS_DST = -16033  # negative id, 404


class ReplayCoreClient:
    """Stands in for ``EspnCoreClient`` against recorded athlete records. No network."""

    def __init__(self, doc: dict | None = None):
        self._doc = doc if doc is not None else ATHLETES
        self.asked: list[int] = []

    def fetch_athlete(self, espn_player_id: int, season: int):
        self.asked.append(espn_player_id)
        if espn_player_id in self._doc["not_found"]:
            return None
        record = self._doc["athletes"].get(str(espn_player_id))
        if record is None:
            raise KeyError(f"no recorded athlete {espn_player_id} — record it first")
        return record


def athlete(espn_player_id: int) -> dict:
    return ATHLETES["athletes"][str(espn_player_id)]


def _code_of(obj) -> str:
    """The executable source of ``obj``, with every docstring removed.

    A test that greps source for a forbidden word otherwise fails on the docstring that
    explains why the word is forbidden — which teaches the next reader to delete the
    explanation rather than keep the guard.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(obj)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            body.pop(0)
    return ast.unparse(tree)


def test_the_draft_class_is_read_from_draft_year():
    assert first_nfl_season(athlete(SKATTEBO)) == (2025, "draft_year")
    assert first_nfl_season(athlete(NACUA)) == (2023, "draft_year")


def test_an_undrafted_player_falls_back_to_debut_year():
    """No draft class exists for an undrafted player, so ``debutYear`` is the first season."""
    assert first_nfl_season(athlete(TAYSOM_HILL)) == (2017, "debut_year")


def test_a_player_with_neither_field_has_no_first_season():
    """Where a number cannot be known, record no number — the caller marks him unresolved."""
    assert first_nfl_season(athlete(JAYLEN_WARREN)) is None


def test_experience_years_is_never_read():
    """The trap, replayed from the real payload.

    Jawhar Jordan was drafted in 2024 and ESPN reports ``experience.years == 1``, because that
    field counts *accrued* seasons rather than a draft class. A parser that reached for it
    would call a third-year player a rookie and make him prospect-eligible.
    """
    record = athlete(JAWHAR_JORDAN)
    assert record["experience"]["years"] == 1, "fixture no longer holds the trap"
    assert record["draft"]["year"] == 2024

    assert first_nfl_season(record) == (2024, "draft_year")
    assert first_nfl_season(record) != (2026, "experience")


def test_the_recorded_athlete_payload_is_not_trimmed():
    """The fixture is whole. Trimming one is what produced the wrong conclusion before.

    If this fails because ESPN dropped a field, that is worth knowing. If it fails because
    somebody pruned the file to what the parser reads, read this module's docstring.
    """
    record = athlete(JAWHAR_JORDAN)
    assert len(record) > 20, f"the recorded payload has been trimmed to {len(record)} keys"
    for untouched in ("college", "displayHeight", "jersey", "status", "experience"):
        assert untouched in record, f"{untouched} is gone — the payload was trimmed"


def test_a_404_is_an_answer_not_an_outage(monkeypatch):
    """``None`` means ESPN has no such athlete. An outage must never look like that.

    Conflating the two would record "ESPN has no draft class for anyone" the first time the
    core API returned 503, and every prospect in the league would silently go unverified.

    Exercised through the **real** ``fetch_athlete``, with the transport stubbed. Asserting
    this against ``ReplayCoreClient`` proves only that the test double behaves — the first
    version of this test did exactly that and survived a mutation that made 404 raise.
    """
    import rs57.espn as espn_module

    def transport(url, *, timeout, cookies=None):
        if "/athletes/-16033" in url:
            raise AthleteNotFound(url)
        if "/athletes/999" in url:
            raise EspnError(f"could not reach ESPN for {url}: timed out")
        return {"draft": {"year": 2025}}

    monkeypatch.setattr(espn_module, "_get_json", transport)
    client = EspnCoreClient()

    assert client.fetch_athlete(RAVENS_DST, 2026) is None, "a 404 must be an answer"
    assert client.fetch_athlete(SKATTEBO, 2026) == {"draft": {"year": 2025}}
    with pytest.raises(EspnError):
        client.fetch_athlete(999, 2026)  # an outage must stop the run, never return None


def test_the_core_client_never_sends_credentials():
    """``espn_s2``/``SWID`` belong to the fantasy host. This is a different, public host.

    The guard is that the client has nowhere to put a cookie: no ``from_env``, no cookie
    field, and nothing passed to ``_get_json``. Asserted on the surface so that adding one
    back has to break a test.
    """
    client = EspnCoreClient()
    assert not hasattr(client, "from_env")
    assert not hasattr(client, "_cookies")
    assert not hasattr(client, "authenticated")

    # Scanned with docstrings stripped, so the prose explaining the absence cannot trip its
    # own test. ``cookies=`` is the mechanism: it is the keyword ``_get_json`` takes, and the
    # only route a credential has to the wire.
    code = _code_of(EspnCoreClient)
    assert "cookies=" not in code, "the core client passes cookies to _get_json"
    assert "ESPN_S2" not in code and "SWID" not in code
    assert "Cookie" not in code

    url = client.athlete_url(SKATTEBO, 2026)
    assert url.startswith(CORE_HOST), "the core client is pointed at the fantasy host"
    for secret in ("espn_s2", "SWID", "Cookie"):
        assert secret not in url


def test_the_core_host_is_not_the_fantasy_host():
    """Two hosts, two APIs. The keeper sync must never depend on the core one."""
    assert CORE_HOST != HOST
    assert "sports.core.api.espn.com" in CORE_HOST
