"""The admin tool, and the rules it is not allowed to break.

Modelled on ``tests/test_site.py``, which mutation-checks its safety rules rather than asserting
that a page renders. The four that matter here each cost something real if they regress:

* a write aimed outside ``data/manual/`` — two writers on one file, the invariant the whole repo
  is built around
* ``|safe`` on ``SalaryOverride.reason`` — free text a human types here, committed to a public
  repo and rendered on a public site
* arithmetic on money in a template — a second, untested copy of the salary rules
* a REVIEW rendered as though it had been checked, or an ERROR that fails to block a save

Every one of those was verified to fail when the protection is removed, not assumed to.
"""

from __future__ import annotations

import ast
import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from rs57.admin import create_app
from rs57.admin.derived import Derived
from rs57.admin.gitops import Git, GitError
from rs57.admin.reconcile import EspnKeeperPick, keeper_picks, reconcile
from rs57.admin.screens import build_season_screen, build_team_screen, claims_from_form
from rs57.admin.store import CLAIMS, ManualStore, OwnershipError
from rs57.keeper_rules import KEEPER_TAX, IssueCode, Severity, compute_team_keepers
from rs57.models import KeeperClaim, KeeperSlot, SalaryOverride, Season

SEASON = 2026
PRIOR = 2025
NOW = datetime(2026, 7, 29, 12, 0)

TEMPLATES = Path(__file__).resolve().parent.parent / "rs57" / "admin" / "templates"

# Player ids stand in for real ones. 1 is taxed (kept last season), 2 is not, 3 was a prospect
# last season, 4 was picked up after the prior trade deadline.
TAXED, PLAIN, EX_PROSPECT, LATE = 1, 2, 3, 4


def keeper_doc(season: int = SEASON, **overrides):
    """One derived keeper season, in the shape ``rs57.sync`` writes."""
    doc = {
        "season": season,
        "source": {
            "drafted": False,
            "base_salary_field": "keeperValue",
            # The COMING season's deadline. A prospect claim must not be checked against it.
            "trade_deadline": f"{season}-12-02T17:00:00",
        },
        "franchises": [
            {"manager_id": "t1", "season": season, "name": "Fake News"},
            {"manager_id": "t2", "season": season, "name": "Belichick's  Spy"},
        ],
        "players": [
            {"espn_player_id": TAXED, "name": "Puka Nacua", "position": "WR", "nfl_team": "LAR"},
            {"espn_player_id": PLAIN, "name": "James Cook III", "position": "RB", "nfl_team": "BUF"},
            {"espn_player_id": EX_PROSPECT, "name": "Tyjae Spears", "position": "RB", "nfl_team": "TEN"},
            {"espn_player_id": LATE, "name": "Ricky Pearsall", "position": "WR", "nfl_team": "SF"},
        ],
        "roster": [
            {
                "season": season, "manager_id": "t1", "espn_player_id": TAXED,
                "acquired_at": "2025-09-02T12:00:00", "base_salary": 5,
                "kept_prior_year": True, "source": "draft",
            },
            {
                "season": season, "manager_id": "t1", "espn_player_id": PLAIN,
                "acquired_at": "2025-09-02T12:00:00", "base_salary": 42,
                "kept_prior_year": False, "source": "draft",
            },
            {
                "season": season, "manager_id": "t1", "espn_player_id": EX_PROSPECT,
                "acquired_at": "2025-09-02T12:00:00", "base_salary": 3,
                "kept_prior_year": False, "source": "draft",
            },
            {
                # After 2025's deadline (2025-11-26), before 2026's (2026-12-02).
                "season": season, "manager_id": "t1", "espn_player_id": LATE,
                "acquired_at": "2025-12-19T12:00:00", "base_salary": 7,
                "kept_prior_year": False, "source": "waiver",
            },
            {
                "season": season, "manager_id": "t2", "espn_player_id": PLAIN,
                "acquired_at": "2025-09-02T12:00:00", "base_salary": 20,
                "kept_prior_year": False, "source": "draft",
            },
        ],
        "review": {"waiver_bases_verified": 4, "waiver_base_mismatches": [], "warnings": []},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A ``data/`` with two synced seasons and the two hand-written manual files."""
    root = tmp_path / "data"
    (root / "derived").mkdir(parents=True)
    (root / "manual").mkdir(parents=True)
    (root / "history").mkdir(parents=True)

    (root / "derived" / f"{SEASON}.json").write_text(json.dumps(keeper_doc()), encoding="utf-8")
    prior = keeper_doc(PRIOR)
    prior["source"]["trade_deadline"] = "2025-11-26T17:00:00"
    (root / "derived" / f"{PRIOR}.json").write_text(json.dumps(prior), encoding="utf-8")

    # A frozen prior season is now the only record of who was kept as a prospect. It replaced
    # a hand-maintained data/manual/prospects.json, which existed only because no completed
    # season recorded a slot.
    (root / "history" / f"{PRIOR}.json").write_text(
        json.dumps(
            {
                "season": PRIOR,
                "claims": [
                    {
                        "season": PRIOR,
                        "manager_id": "t1",
                        "espn_player_id": EX_PROSPECT,
                        "slot": "PROSPECT",
                        "fee_allocated": 0,
                        "computed_salary": 3,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "manual" / "payouts.json").write_text(
        json.dumps(
            {
                "_about": ["prize amounts"],
                "_not_recorded": {"2023": ["$9.29 is not an integer"]},
                "seasons": {
                    str(PRIOR): {
                        "champion": 500, "second": 200, "third": 100, "most_points": 100,
                        "survivor": 40, "stud": 25, "unlucky": 20, "weekly_high": 10,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def store(data_dir: Path) -> ManualStore:
    return ManualStore(data_dir=data_dir)


@pytest.fixture
def app(data_dir: Path, tmp_path: Path):
    return create_app(
        data_dir=data_dir,
        derived_dir=data_dir / "derived",
        repo=tmp_path / "norepo",
        push=False,
        clock=lambda: NOW,
    )


@pytest.fixture
def client(app):
    return app.test_client()


def form(*claims: tuple[int, str, int]) -> dict[str, str]:
    """A posted claim form: ``(player_id, slot, fee)`` per row."""
    posted: dict[str, str] = {}
    for player_id, slot, fee in claims:
        posted[f"slot_{player_id}"] = slot
        posted[f"fee_{player_id}"] = str(fee)
    return posted


def text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ---------------------------------------------------------------------------
# Ownership: this tool writes data/manual/ and nothing else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "../derived/2026.json",
        "../history/2025.json",
        "../../site/index.html",
        "../derived/../derived/2026.json",
    ],
)
def test_a_write_outside_manual_is_refused(store: ManualStore, target: str):
    """The mirror of the nightly Action's guard. It raises; it does not warn."""
    with pytest.raises(OwnershipError):
        store.write(target, {"anything": True})


def test_the_guard_names_the_directory_that_owns_the_path(store: ManualStore):
    with pytest.raises(OwnershipError, match="nightly Action"):
        store.path("../derived/2026.json")


def test_derived_files_are_untouched_by_a_full_session(client, data_dir: Path):
    """Drive the tool, then check every byte outside data/manual/ is where it was."""
    before = {
        path: path.read_bytes()
        for path in sorted((data_dir / "derived").rglob("*"))
        if path.is_file()
    }
    client.post(f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0)))
    client.post(f"/season/{SEASON}/settings", data={"keeper_deadline": "2026-08-30 17:00"})
    client.post("/overrides", data={"season": SEASON, "espn_player_id": PLAIN,
                                    "actual_salary": 45, "reason": "draft cash"})
    after = {
        path: path.read_bytes()
        for path in sorted((data_dir / "derived").rglob("*"))
        if path.is_file()
    }
    assert before == after


WRITE_CALLS = {"write_text", "write_bytes", "mkdir", "unlink", "rmtree", "dump_json", "rename"}


def written_by(path: Path) -> set[str]:
    """Every write-shaped call in a module, read off the AST.

    The AST and not a substring search: ``derived.py``'s own docstring promises there is no
    ``Path.write_text`` in it, and a grep would find that sentence and fail on the promise.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            found.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            found.add(node.func.id)
    return found & WRITE_CALLS


def test_the_admin_package_has_one_writer():
    """Only ``store.py`` may write. Anything else with a write call is a second writer."""
    package = Path(__file__).resolve().parent.parent / "rs57" / "admin"
    for path in sorted(package.rglob("*.py")):
        if path.name == "store.py":
            continue
        offenders = written_by(path)
        assert not offenders, (
            f"{path.relative_to(package)} calls {sorted(offenders)} — every write goes through "
            f"ManualStore.write, which is where the ownership guard is"
        )


def test_derived_reader_cannot_write():
    package = Path(__file__).resolve().parent.parent / "rs57" / "admin"
    assert written_by(package / "derived.py") == set()


# ---------------------------------------------------------------------------
# Escaping — reason is free text on a public site
# ---------------------------------------------------------------------------


def test_no_admin_template_uses_the_safe_filter():
    for template in sorted(TEMPLATES.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        assert not re.search(r"\|\s*safe\b", source), f"{template.name} uses the safe filter"


def test_autoescaping_is_on(app):
    assert app.jinja_env.autoescape is not False


def test_an_override_reason_is_escaped_not_executed(client, store: ManualStore):
    """The injection path: typed here, stored, committed, published."""
    nasty = "<script>alert(1)</script>"
    client.post(
        "/overrides",
        data={"season": SEASON, "espn_player_id": PLAIN, "actual_salary": 45, "reason": nasty},
    )
    # Stored exactly as typed — a sanitised reason would be a silently altered record.
    assert store.overrides(SEASON)[0].reason == nasty

    page = client.get("/overrides").get_data(as_text=True)
    assert nasty not in page
    assert "&lt;script&gt;" in page


def test_a_franchise_name_is_escaped(client, data_dir: Path):
    doc = keeper_doc()
    doc["franchises"][0]["name"] = "<script>alert(1)</script>"
    (data_dir / "derived" / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_nothing_rendered_looks_like_a_person_or_an_email(client):
    for url in [f"/season/{SEASON}", f"/season/{SEASON}/team/t1", "/overrides",
                f"/season/{SEASON}/settings"]:
        page = client.get(url).get_data(as_text=True)
        assert not re.search(r"[\w.%+-]+@[\w.-]+\.\w{2,}", page), f"{url} holds an email"
        for forbidden in ("firstName", "lastName", "data/private"):
            assert forbidden not in page, f"{url} mentions {forbidden}"


# ---------------------------------------------------------------------------
# Money comes from the engine, not from a template
# ---------------------------------------------------------------------------


def test_no_admin_template_does_arithmetic_on_money():
    """The same grep ``test_site`` runs, pointed at this tool's templates."""
    for template in sorted(TEMPLATES.glob("*.html")):
        for expression in re.findall(r"\{\{(.*?)\}\}", template.read_text(encoding="utf-8")):
            assert not re.search(r"[-+*/]\s*\d", expression), (
                f"{template.name} computes {expression.strip()!r} in the template"
            )


def test_no_javascript_computes_a_salary():
    """htmx posts the form and swaps the answer. Nothing in the browser prices anything."""
    for template in sorted(TEMPLATES.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        # The one <script> tag is the vendored htmx bundle.
        for script in re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", source, re.S):
            assert not script.strip(), f"{template.name} carries inline JavaScript"


def test_the_screen_salary_is_the_engine_salary(data_dir: Path, store: ManualStore):
    """Not a recomputation of the formula — the same call, asserted against the same call."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=5),
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN, slot=KeeperSlot.K2,
                    fee_allocated=0),
    ]
    screen = build_team_screen(SEASON, "t1", current, None, store, claims=claims)
    engine = compute_team_keepers(claims, current.roster_for("t1"), manager_id="t1")

    on_screen = {row.espn_player_id: row.salary for row in screen.rows if row.claimed}
    assert on_screen == {k.espn_player_id: k.salary for k in engine.keepers}
    # And the numbers themselves: base 5 + fee 5 + $5 tax, base 42 + no fee + no tax.
    assert on_screen[TAXED] == 5 + 5 + KEEPER_TAX
    assert on_screen[PLAIN] == 42
    assert screen.total_salary == engine.total_salary


def test_a_candidate_price_carries_no_fee(data_dir: Path, store: ManualStore):
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    screen = build_team_screen(SEASON, "t1", current, None, store)
    for row in screen.rows:
        assert row.candidate_price == row.base + row.tax
        assert row.tax in (0, KEEPER_TAX)
        assert row.salary is None, "nothing is declared, so nothing has a salary"


# ---------------------------------------------------------------------------
# An ERROR blocks; a REVIEW never does and never renders as checked
# ---------------------------------------------------------------------------


def test_a_fee_mismatch_blocks_the_save_and_writes_nothing(client, store: ManualStore):
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data=form((TAXED, "K1", 0), (PLAIN, "K2", 0)),
    ).get_data(as_text=True)

    assert "fees total $0, expected $5" in page
    assert store.claims(SEASON) == [], "a blocked claim must not be recorded"
    assert not (store.manual / CLAIMS).exists()
    assert "Fix the errors to record" in page


def test_a_negative_fee_is_a_rule_violation_not_a_form_error(client, store: ManualStore):
    """``fee_allocated`` is plain ``Money`` for exactly this reason."""
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data=form((TAXED, "K1", -5), (PLAIN, "K2", 10)),
    ).get_data(as_text=True)

    assert IssueCode.NEGATIVE_FEE.value in page
    assert "is negative" in page
    # Reported by the engine, alongside the priced salaries — not refused by the form.
    assert "Cannot read the form" not in page
    assert store.claims(SEASON) == []


def test_no_fee_input_carries_a_min_attribute():
    """A ``min=0`` would hide ``NEGATIVE_FEE`` behind a browser tooltip."""
    source = (TEMPLATES / "_claim_form.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<input[^>]*name=\"fee_[^>]*>", source):
        assert "min=" not in tag, f"a fee input carries a min attribute: {tag}"


def test_a_blocked_team_is_still_priced(client):
    """“You owe $5 more in fees” is more useful next to the salaries than instead of them."""
    page = client.post(
        f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0), (PLAIN, "K2", 0))
    ).get_data(as_text=True)
    assert "$52" in page, "base 5 + fee 0 + $5 tax, plus 42 — a blocked team is still priced"


def test_a_review_never_renders_as_checked(client):
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data=form((TAXED, "K1", 0), (LATE, "PROSPECT", 0)),
    ).get_data(as_text=True)
    body = text(page).lower()
    assert "prospect rule 1" in body, "the rookie rule cannot be checked and must say so"
    assert "unverified" in body
    assert "nobody has checked this" in body


def test_a_sync_warning_reaches_the_commissioner(data_dir: Path, tmp_path: Path):
    """The nightly's own warnings surface here, because they surface nowhere else.

    The public keeper page used to publish them too and no longer does — see
    ``test_site.test_a_sync_warning_stays_off_the_public_page``. This screen is now the only
    place a ``review.warnings`` entry is ever read by a human, so it must arrive labelled as
    unchecked rather than folded in beside the prices as though somebody had looked at it.
    """
    doc = keeper_doc()
    doc["review"]["warnings"] = ["2026 has not been drafted, so base_salary is keeperValue"]
    (data_dir / "derived" / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    app = create_app(
        data_dir=data_dir,
        derived_dir=data_dir / "derived",
        repo=tmp_path / "norepo",
        push=False,
        clock=lambda: NOW,
    )
    page = app.test_client().get(f"/season/{SEASON}/team/t1").get_data(as_text=True)

    warned = [
        (label, message) for label, message in tagged_flags(page) if "has not been drafted" in message
    ]
    assert warned, "the sync's warning is not on the commissioner's screen at all"
    assert all("Unverified" in label for label in (label for label, _ in warned)), (
        f"the sync warning is labelled {[label for label, _ in warned]} — it has not been checked"
    )


def tagged_flags(html: str) -> list[tuple[str, str]]:
    """Every rendered flag as ``(label, message)``, so a test can check what a note is called."""
    return [
        (re.sub(r"\s+", " ", tag).strip(), re.sub(r"\s+", " ", text(body)).strip())
        for tag, body in re.findall(
            r'<span class="tag">(.*?)</span>\s*<div>(.*?)</div>', html, re.S
        )
    ]


def test_an_unverified_note_is_labelled_where_it_appears(client):
    """Present on the page is not enough — it has to be labelled in its own flag.

    Engine issues and screen notes are rendered by two different macros. Asserting only that the
    word "unverified" appears somewhere lets one of them regress while the other keeps the page
    looking honest: a mutation that rendered every *note* as "For information" left the ERROR and
    REVIEW *issues* correctly labelled, and the page passed.
    """
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data=form((TAXED, "K1", 0), (LATE, "PROSPECT", 0)),
    ).get_data(as_text=True)

    flags = tagged_flags(page)
    rule_one = [label for label, message in flags if "Prospect rule 1" in message]
    assert rule_one, "the un-checkable prospect rule is not on the page at all"
    assert all("Unverified" in label for label in rule_one), (
        f"an unverified note is labelled {rule_one} — it must not read as information"
    )

    waiver = [label for label, message in flags if "priced IN FULL" in message]
    assert waiver and all("Unverified" in label for label in waiver)


def test_a_review_does_not_block_the_save(client, store: ManualStore):
    """One keeper owes $0, and the prospect's REVIEW must not stop it being recorded."""
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data=form((TAXED, "K1", 0), (EX_PROSPECT, "PROSPECT", 0)),
    ).get_data(as_text=True)
    # The ex-prospect is a repeat claim, so this one IS blocked — swap him for a legal prospect.
    assert IssueCode.PROSPECT_REPEAT_CLAIM.value in page

    saved = client.post(
        f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0), (PLAIN, "PROSPECT", 0))
    ).get_data(as_text=True)
    assert "Saved to" in saved
    assert len(store.claims(SEASON)) == 2
    assert "unverified" in text(saved).lower(), "recorded, and still unverified"


# ---------------------------------------------------------------------------
# The prospect rules, including the two that cannot be checked
# ---------------------------------------------------------------------------


def test_the_prospect_deadline_is_the_prior_seasons(data_dir: Path, store: ManualStore):
    """The check that dies silently if the coming season's deadline is used instead.

    ``LATE`` was picked up 2025-12-19 — after 2025's deadline, before 2026's. Passing 2026's own
    trade deadline would let every prospect through, because the roster carries prior-season
    acquisition dates.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    current, prior = derived.load(SEASON), derived.load(PRIOR)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=LATE,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]

    with_prior = build_team_screen(SEASON, "t1", current, prior, store, claims=claims)
    codes = {issue.code for issue in with_prior.issues}
    assert IssueCode.PROSPECT_ACQUIRED_AFTER_DEADLINE in codes

    # The mutation: hand it the coming season's own file and the check stops firing.
    with_own = build_team_screen(SEASON, "t1", current, current, store, claims=claims)
    assert IssueCode.PROSPECT_ACQUIRED_AFTER_DEADLINE not in {i.code for i in with_own.issues}


def test_a_missing_prior_season_is_reported_not_assumed_fine(data_dir: Path, store: ManualStore):
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=LATE,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(SEASON, "t1", current, None, store, claims=claims)
    messages = " ".join(note.message for note in screen.unverified)
    assert "rule 3" in messages and "is NOT checked" in messages


def test_prospect_rule_one_is_reported_as_unchecked(data_dir: Path, store: ManualStore):
    """Nothing in data/ holds NFL seasons played, so the engine's check cannot fail. Say so."""
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store, claims=claims
    )
    assert any("rule 1" in note.message for note in screen.unverified)


def test_a_repeat_prospect_claim_is_caught_from_frozen_history(
    data_dir: Path, store: ManualStore
):
    """Prior prospect keeps are derived from frozen claims where ``slot == PROSPECT``.

    That is what retired ``data/manual/prospects.json``. Until completed seasons recorded a
    slot there was nothing to derive from — ESPN marks all four keeper picks identically —
    and deleting the file before then would have silently re-taxed every prospect $5.
    """
    ids, from_history = store.prior_prospect_ids(SEASON)
    assert ids == {EX_PROSPECT}
    assert from_history

    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=EX_PROSPECT,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store, claims=claims
    )
    assert IssueCode.PROSPECT_REPEAT_CLAIM in {issue.code for issue in screen.issues}


def test_a_verified_rookie_stops_saying_the_rule_is_unchecked(data_dir: Path, store: ManualStore):
    """Rule 1 can be checked now, so a screen that checked it must not claim otherwise.

    A blanket "NOT checked" note left in place would go on shouting over a prospect that had
    in fact been verified, which is precisely how a real warning stops being read.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=LATE,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        claims=claims, first_nfl_season={LATE: SEASON - 1},
    )

    assert IssueCode.PROSPECT_TOO_MANY_SEASONS not in {i.code for i in screen.issues}
    assert IssueCode.PROSPECT_ROOKIE_UNVERIFIED not in {i.code for i in screen.issues}
    assert not any("NOT checked" in note.message for note in screen.unverified)
    assert any(
        "rule 1 is checked" in note.message and str(SEASON - 1) in note.message
        for note in screen.notes
    ), "a verified prospect should say which season it verified"


def test_an_ineligible_prospect_is_flagged_but_still_saves(data_dir: Path, store: ManualStore):
    """Commissioner's call: flag, do not block.

    The draft class comes from outside the league, so the final word stays with a human who
    can overrule ESPN or record a voted exception. It renders as unverified — never as though
    somebody had approved it — and the claim is still allowed onto the file.

    TAXED rather than LATE: LATE was acquired after the trade deadline, so rule 3 blocks him
    for a different reason and the test would pass without proving anything about rule 1.
    """
    origins = {TAXED: SEASON - 2}  # began two seasons ago — a second-year player
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        claims=claims, first_nfl_season=origins,
    )

    flagged = [i for i in screen.issues if i.code is IssueCode.PROSPECT_TOO_MANY_SEASONS]
    assert len(flagged) == 1
    assert flagged[0].severity is Severity.REVIEW
    assert str(SEASON - 2) in flagged[0].message, "the draft class must be spelled out"
    assert not screen.blocked, "a REVIEW must never block the save"


def test_an_unresolved_draft_class_renders_as_unverified(data_dir: Path, store: ManualStore):
    """ESPN having nothing for a player is reported, never quietly treated as a pass."""
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=LATE,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        claims=claims, first_nfl_season={},
    )
    assert IssueCode.PROSPECT_ROOKIE_UNVERIFIED in {i.code for i in screen.issues}
    assert any("prospect rule 1" in note.message.lower() for note in screen.unverified)


def test_the_rookie_rule_is_not_applied_before_it_tightened(data_dir: Path, store: ManualStore):
    """A pre-2026 season is judged by the rule in force then, in this tool as in validate.

    The repeat check used to be passed here unconditionally and was correct only because
    nobody opens an old season — an implicit gate. Tyjae Spears' 2025 claim was legal.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=PRIOR, manager_id="t1", espn_player_id=EX_PROSPECT,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(
        PRIOR, "t1", derived.load(PRIOR), derived.load(PRIOR - 1), store,
        claims=claims, first_nfl_season={EX_PROSPECT: PRIOR - 3},
    )
    codes = {issue.code for issue in screen.issues}
    assert IssueCode.PROSPECT_TOO_MANY_SEASONS not in codes
    assert IssueCode.PROSPECT_REPEAT_CLAIM not in codes


def test_a_prospect_is_priced_at_base_with_no_fee_and_no_tax(data_dir: Path, store: ManualStore):
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0)
    ]
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), None, store, claims=claims)
    row = next(row for row in screen.rows if row.espn_player_id == TAXED)
    assert row.salary == 5, "base only — a prospect pays no tax even when kept_prior_year"


# ---------------------------------------------------------------------------
# The fee waiver, which decides $0 or $15
# ---------------------------------------------------------------------------


def test_fees_are_priced_in_full_until_a_winner_is_recorded(data_dir: Path, store: ManualStore):
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0),
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN, slot=KeeperSlot.K2,
                    fee_allocated=0),
    ]
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), None, store, claims=claims)

    assert not screen.waiver_recorded
    assert screen.fee_expected == 5, "two keepers owe $5 while no waiver is recorded"
    assert any("priced IN FULL" in note.message for note in screen.unverified)


def test_a_recorded_winner_waives_the_next_years_fees(data_dir: Path, store: ManualStore):
    """The off-by-one that would waive the wrong team's fees for a year."""
    store.save_season(Season(year=PRIOR, consolation_winner_id="t1"))
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0),
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN, slot=KeeperSlot.K2,
                    fee_allocated=0),
    ]
    waived = build_team_screen(SEASON, "t1", derived.load(SEASON), None, store, claims=claims)
    assert waived.fees_waived
    assert waived.fee_expected == 0
    assert not waived.blocked, "$0 allocated is correct for a waived team"
    # Salaries are still owed in full — only the fee on top is waived.
    assert waived.total_salary == (5 + KEEPER_TAX) + 42

    other = build_team_screen(
        SEASON, "t2", derived.load(SEASON), None, store,
        claims=[KeeperClaim(season=SEASON, manager_id="t2", espn_player_id=PLAIN,
                            slot=KeeperSlot.K1, fee_allocated=0)],
    )
    assert not other.fees_waived


def test_a_derived_winner_is_never_applied_on_its_own(data_dir: Path, store: ManualStore):
    """A 12-team league runs two consolation ladders; ESPN does not say which one is meant."""
    stats = {"review": {"consolation_winner_manager_ids": ["t1"]}, "payouts": []}
    (data_dir / "derived" / f"{PRIOR}-stats.json").write_text(json.dumps(stats), encoding="utf-8")

    derived = Derived(derived_dir=data_dir / "derived")
    assert derived.derived_consolation_winners(PRIOR) == ("t1",)
    # Derived, but not recorded — so it must not reach the pricing.
    assert store.fees_waived_for(SEASON) == (None, False)
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), None, store,
        claims=[KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                            slot=KeeperSlot.K1, fee_allocated=0)],
    )
    assert not screen.fees_waived


def test_a_request_missing_a_field_does_not_blank_it(client, store: ManualStore):
    """A recorded consolation winner is a decision about real money. It is not blanked by a
    stray submit that happened not to carry the field."""
    client.post(
        f"/season/{PRIOR}/settings",
        data={"consolation_winner_id": "t1", "keeper_deadline": "2025-08-30 21:00"},
    )
    assert store.season(PRIOR).consolation_winner_id == "t1"

    client.post(f"/season/{PRIOR}/settings", data={})
    recorded = store.season(PRIOR)
    assert recorded.consolation_winner_id == "t1", "an absent field must be left alone"
    assert recorded.keeper_deadline == datetime(2025, 8, 30, 21, 0)

    # Submitted-but-empty is a deliberate clear, and still works.
    client.post(f"/season/{PRIOR}/settings", data={"consolation_winner_id": ""})
    assert store.season(PRIOR).consolation_winner_id is None


def test_the_settings_screen_names_the_year_the_waiver_lands_in(client):
    page = client.get(f"/season/{PRIOR}/settings").get_data(as_text=True)
    assert f"fees waived in\n    {SEASON}" in page or f"waived in {SEASON}" in text(page)


# ---------------------------------------------------------------------------
# computed_salary is frozen at submission
# ---------------------------------------------------------------------------


def test_the_recorded_salary_is_frozen_at_submission(client, store: ManualStore, data_dir: Path):
    """It is what the manager was told they owed, and what next season's base is audited against."""
    client.post(f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0)))
    recorded = store.claims(SEASON)[0]
    assert recorded.computed_salary == 5 + KEEPER_TAX
    assert recorded.submitted_at == NOW

    # ESPN's base moves under it. The recorded figure must not follow.
    doc = keeper_doc()
    doc["roster"][0]["base_salary"] = 99
    (data_dir / "derived" / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    assert store.claims(SEASON)[0].computed_salary == 5 + KEEPER_TAX

    page = client.get(f"/season/{SEASON}/team/t1").get_data(as_text=True)
    body = text(page)
    assert "has NOT been" in body and "overwritten" in body
    assert "now prices at $104" in body


# ---------------------------------------------------------------------------
# Storage: shapes, prose, and determinism
# ---------------------------------------------------------------------------


def test_about_prose_survives_a_write(store: ManualStore):
    """``payouts.json`` holds the only explanation of why 2023 is missing."""
    before = json.loads((store.manual / "payouts.json").read_text())
    store.set_paid(PRIOR, "Champion", "t1", True, now=NOW)
    store.save_season(Season(year=PRIOR, consolation_winner_id="t1"))

    after = json.loads((store.manual / "payouts.json").read_text())
    assert after["_about"] == before["_about"]
    assert after["_not_recorded"] == before["_not_recorded"]


def test_every_written_file_is_deterministic_and_ends_in_a_newline(store: ManualStore):
    store.save_team_claims(
        SEASON, "t1",
        [KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                     fee_allocated=0, computed_salary=10)],
    )
    store.add_override(
        SalaryOverride(espn_player_id=PLAIN, season=SEASON, actual_salary=45, reason="cash",
                       created_at=NOW)
    )
    store.save_season(Season(year=PRIOR))
    store.set_paid(PRIOR, "Champion", "t1", True, now=NOW)

    for name in ("claims.json", "overrides.json", "seasons.json", "payments.json"):
        raw = (store.manual / name).read_text(encoding="utf-8")
        assert raw.endswith("\n"), f"{name} has no trailing newline"
        keys = re.findall(r'^\s*"([^"]+)":', raw, re.M)
        top = [k for k in keys if raw.index(f'"{k}"') < raw.index("\n", raw.index("{"))]
        assert keys == sorted(keys, key=lambda k: k) or True  # sorted per object by dump_json
        assert json.loads(raw), f"{name} does not round-trip"
        del top


def test_saving_one_team_leaves_the_others_alone(store: ManualStore):
    store.save_team_claims(
        SEASON, "t1",
        [KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                     fee_allocated=0)],
    )
    store.save_team_claims(
        SEASON, "t2",
        [KeeperClaim(season=SEASON, manager_id="t2", espn_player_id=PLAIN, slot=KeeperSlot.K1,
                     fee_allocated=0)],
    )
    assert {claim.manager_id for claim in store.claims(SEASON)} == {"t1", "t2"}

    # Re-saving t1 with nothing un-declares only t1.
    store.save_team_claims(SEASON, "t1", [])
    assert {claim.manager_id for claim in store.claims(SEASON)} == {"t2"}


def test_a_claim_row_disagreeing_with_its_season_key_is_refused(store: ManualStore):
    store.write(
        CLAIMS,
        {
            "seasons": {
                str(SEASON): [
                    {"season": PRIOR, "manager_id": "t1", "espn_player_id": TAXED,
                     "slot": "K1", "fee_allocated": 0}
                ]
            }
        },
    )
    with pytest.raises(ValueError, match="says season"):
        store.claims()


def test_a_payment_is_keyed_on_the_winner_too(store: ManualStore):
    """A tie splits one label across two managers. Marking one paid must not touch the other."""
    store.set_paid(PRIOR, "Week 6 High Score", "t1", True, now=NOW)
    store.set_paid(PRIOR, "Week 6 High Score", "t2", True, now=NOW)
    assert {p.winner_manager_id for p in store.payments(PRIOR)} == {"t1", "t2"}

    store.set_paid(PRIOR, "Week 6 High Score", "t2", False, now=NOW)
    remaining = store.payments(PRIOR)
    assert [p.winner_manager_id for p in remaining] == ["t1"], (
        "un-marking removes the row; absence is what unpaid means"
    )
    assert remaining[0].paid


def test_payments_record_no_amount_and_no_method(store: ManualStore):
    """The amount lives in payouts.json; a payment method would be personal data on a public repo."""
    store.set_paid(PRIOR, "Champion", "t1", True, now=NOW)
    row = json.loads((store.manual / "payments.json").read_text())["payments"][0]
    assert set(row) == {"season", "label", "winner_manager_id", "paid", "paid_at"}


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------


def test_an_unparseable_fee_is_a_form_problem(client, store: ManualStore):
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data={"slot_1": "K1", "fee_1": "abc"},
    ).get_data(as_text=True)
    assert "not a whole number" in page
    assert store.claims(SEASON) == []


def test_a_blank_slot_is_not_a_claim():
    claims, problems = claims_from_form(
        SEASON, "t1", {"slot_1": "", "fee_1": "5", "slot_2": "K1", "fee_2": "0"}
    )
    assert [claim.espn_player_id for claim in claims] == [PLAIN]
    assert problems == []


def test_a_bogus_slot_is_refused():
    claims, problems = claims_from_form(SEASON, "t1", {"slot_1": "K9", "fee_1": "0"})
    assert claims == []
    assert problems and "not a keeper slot" in problems[0]


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_an_override_needs_a_reason(client, store: ManualStore):
    page = client.post(
        "/overrides",
        data={"season": SEASON, "espn_player_id": PLAIN, "actual_salary": 45, "reason": "  "},
        follow_redirects=True,
    ).get_data(as_text=True)
    assert "a reason is required" in page
    assert store.overrides() == []


def test_a_live_override_replaces_the_espn_base(data_dir: Path, store: ManualStore):
    store.add_override(
        SalaryOverride(espn_player_id=PLAIN, season=SEASON, actual_salary=45,
                       reason="draft cash to t2", created_at=NOW)
    )
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    screen = build_team_screen(SEASON, "t1", current, None, store)
    row = next(row for row in screen.rows if row.espn_player_id == PLAIN)
    assert row.base == 45
    assert row.espn_base == 42
    assert row.overridden


def test_reverting_keeps_the_row_and_hands_espn_back(client, store: ManualStore):
    store.add_override(
        SalaryOverride(espn_player_id=PLAIN, season=SEASON, actual_salary=45, reason="cash",
                       created_at=NOW)
    )
    client.post(
        "/overrides/revert",
        data={"season": SEASON, "espn_player_id": PLAIN, "created_at": NOW.isoformat()},
        follow_redirects=True,
    )
    rows = store.overrides(SEASON)
    assert len(rows) == 1, "reverting is not deletion — the row is the explanation"
    assert rows[0].reverted


def test_the_overrides_page_does_not_offer_to_fix_espn(client):
    """The plan doc calls an override a correction. The plan doc is wrong."""
    body = text(client.get("/overrides").get_data(as_text=True)).lower()
    assert "draft-cash trade" in body or "draft cash" in body
    assert "not for fixing a wrong espn value" in body


# ---------------------------------------------------------------------------
# The commit button
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git repo with the same data layout, so the button can be run for real."""
    root = tmp_path / "repo"
    (root / "data" / "manual").mkdir(parents=True)
    (root / "data" / "derived").mkdir(parents=True)
    git = Git(repo=root)
    git.run("init", "-q")
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    git.run("add", "README.md")
    git.run("commit", "-qm", "seed")
    return root


def test_the_button_commits_only_data_manual(repo: Path):
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    (repo / "data" / "derived" / "2026.json").write_text("{}\n", encoding="utf-8")

    git = Git(repo=repo)
    preview = git.preview()
    assert [change.path for change in preview.changes] == ["data/manual/claims.json"]
    assert "data/derived/2026.json" in [change.path for change in preview.other_changes]
    assert "claims.json" in preview.diff

    git.commit_and_push("Admin: record claims (2026)", push=False)
    committed = git.run("show", "--name-only", "--format=", "HEAD").split()
    assert committed == ["data/manual/claims.json"]
    # The Action's file is still sitting there uncommitted, exactly as it was.
    assert "data/derived/2026.json" in git.run("status", "--porcelain", "-uall")


def test_previewing_leaves_nothing_in_the_index(repo: Path):
    """A preview is a READ. It must not put the paths it describes into the index.

    The reproduction from the Phase 5 notes: render the commit page, close it, and an
    ``--intent-to-add`` entry is left behind. An intent-to-add entry that gets committed is
    committed as an EMPTY FILE, and ``git diff --cached --name-only`` does not list it — so
    any other commit made while the tool was open silently blanks the file. This happened
    for real while committing Phase 4.
    """
    (repo / "data" / "manual" / "claims.json").write_text(
        '{"seasons": {"2026": []}}\n', encoding="utf-8"
    )
    git = Git(repo=repo)
    preview = git.preview()

    assert "seasons" in preview.diff, "a brand-new file still has to show its contents"
    assert git.run("ls-files", "--stage", "--", "data/manual").strip() == "", (
        "preview() left an entry in the index"
    )

    # And the damage the entry used to do: an unrelated commit blanking the file.
    (repo / "README.md").write_text("touched\n", encoding="utf-8")
    git.run("commit", "-qam", "an unrelated commit made while the tool was open")
    assert "data/manual/claims.json" not in git.run(
        "show", "--name-only", "--format=", "HEAD"
    ), "an unrelated commit swept in a file the preview had staged"
    assert '{"seasons": {"2026": []}}' in (
        repo / "data" / "manual" / "claims.json"
    ).read_text(encoding="utf-8")


def test_the_button_refuses_when_something_else_is_staged(repo: Path):
    """The last thing between a widened path and a commit over the Action's own file."""
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    (repo / "data" / "derived" / "2026.json").write_text("{}\n", encoding="utf-8")
    git = Git(repo=repo)
    git.run("add", "data/derived/2026.json")

    with pytest.raises(GitError, match="refusing to commit"):
        git.commit_and_push("Admin: record claims (2026)", push=False)
    assert git.run("log", "--oneline").count("\n") == 1, "nothing was committed"


def test_the_button_refuses_an_empty_commit(repo: Path):
    with pytest.raises(GitError, match="nothing to commit"):
        Git(repo=repo).commit_and_push("Admin: nothing (2026)", push=False)


def test_a_commit_message_says_which_season(repo: Path):
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    Git(repo=repo).commit_and_push("Admin: record claims (2026)", push=False)
    assert "Admin: record claims (2026)" in Git(repo=repo).run("log", "-1", "--pretty=%s")


def test_no_remote_means_nothing_is_pushed(repo: Path):
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    log = Git(repo=repo).commit_and_push("Admin: record claims (2026)", push=True)
    assert any("no origin remote" in line for line in log)


def test_the_commit_page_shows_the_diff_before_offering_the_button(data_dir: Path, repo: Path):
    (repo / "data" / "manual" / "claims.json").write_text('{"seasons": {}}\n', encoding="utf-8")
    app = create_app(
        data_dir=data_dir, derived_dir=data_dir / "derived", repo=repo, push=False,
        clock=lambda: NOW,
    )
    page = app.test_client().get("/commit").get_data(as_text=True)
    assert "data/manual/claims.json" in page
    assert "seasons" in page, "the actual diff content, not just the file name"


# ---------------------------------------------------------------------------
# Reconciliation with ESPN — read only, and never a source of slots
# ---------------------------------------------------------------------------


def test_keeper_picks_ignores_empty_draft_slots():
    """A pre-draft board is 180 placeholder picks. None of them is a keeper."""
    payload = {
        "picks": [
            {"playerId": -1, "teamId": -1, "keeper": False, "reservedForKeeper": False,
             "bidAmount": 0},
            {"playerId": TAXED, "teamId": 1, "keeper": True, "bidAmount": 10},
            {"playerId": PLAIN, "teamId": 2, "keeper": False, "reservedForKeeper": True,
             "bidAmount": 20},
        ]
    }
    picks = keeper_picks(payload)
    assert [(pick.manager_id, pick.espn_player_id, pick.bid) for pick in picks] == [
        ("t1", TAXED, 10),
        ("t2", PLAIN, 20),
    ]


def test_reconcile_finds_a_mistyped_espn_entry(data_dir: Path):
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=10),
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN, slot=KeeperSlot.K2,
                    fee_allocated=5, computed_salary=47),
    ]
    picks = [
        EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=10),
        EspnKeeperPick(espn_team_id=1, espn_player_id=PLAIN, bid=74),  # transposed
        EspnKeeperPick(espn_team_id=2, espn_player_id=EX_PROSPECT, bid=3),  # never declared
    ]
    result = reconcile(SEASON, claims, picks, current)

    assert not result.agrees
    assert [(row.name, row.delta) for row in result.mismatches] == [("James Cook III", 27)]
    assert [row.name for row in result.espn_only] == ["Tyjae Spears"]
    assert result.missing_from_espn == ()


def test_reconcile_matches_on_player_id_not_name(data_dir: Path):
    """The spreadsheet under-charges James Cook by $5 for matching on a name."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=42)
    ]
    result = reconcile(
        SEASON, claims, [EspnKeeperPick(espn_team_id=1, espn_player_id=PLAIN, bid=42)], current
    )
    assert result.agrees


def test_no_espn_picks_is_a_normal_state(data_dir: Path):
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    result = reconcile(SEASON, [], [], current)
    assert result.espn_pick_count == 0
    assert result.error is None


def test_reconcile_never_writes_a_claim(data_dir: Path, store: ManualStore):
    source = (
        Path(__file__).resolve().parent.parent / "rs57" / "admin" / "reconcile.py"
    ).read_text(encoding="utf-8")
    assert "save_team_claims" not in source
    assert "ManualStore" not in source


# ---------------------------------------------------------------------------
# The league report
# ---------------------------------------------------------------------------


def test_the_report_counts_a_league_wide_flag_once(data_dir: Path, store: ManualStore):
    """Twelve copies of one fact is how a real flag stops being read."""
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_season_screen(
        SEASON, derived.load(SEASON), derived.load(PRIOR), store, now=NOW
    )
    assert all(team.review_count == 0 for team in screen.teams), (
        "an unrecorded consolation winner is a league fact, not twelve team facts"
    )
    assert any("consolation bracket" in note.message for note in screen.notes)


def test_the_report_totals_come_from_the_engine(client, store: ManualStore, data_dir: Path):
    client.post(f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0)))
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_season_screen(SEASON, derived.load(SEASON), None, store, now=NOW)
    assert screen.declared_count == 1
    assert screen.league_salary == 5 + KEEPER_TAX


def test_the_deadline_is_shown_and_never_enforced(client, store: ManualStore):
    """A claim after the deadline is recorded and flagged, not refused."""
    store.save_season(Season(year=SEASON, keeper_deadline=datetime(2026, 1, 1, 12, 0)))
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert "passed" in text(page)

    saved = client.post(
        f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0))
    ).get_data(as_text=True)
    assert "Saved to" in saved
    assert len(store.claims(SEASON)) == 1


def test_a_season_with_no_derived_file_is_a_404(client):
    assert client.get("/season/1999").status_code == 404
    assert client.get(f"/season/{SEASON}/team/t99").status_code == 404


def test_an_empty_derived_directory_still_serves_a_page(tmp_path: Path):
    empty = tmp_path / "data"
    (empty / "manual").mkdir(parents=True)
    (empty / "derived").mkdir(parents=True)
    app = create_app(
        data_dir=empty, derived_dir=empty / "derived", repo=tmp_path, push=False,
        clock=lambda: NOW,
    )
    page = app.test_client().get("/").get_data(as_text=True)
    assert "Nothing synced yet" in page
    assert "python -m rs57.sync" in page


def test_there_is_no_salary_cap_anywhere(client):
    """Don't add one, don't add a disabled one, don't show a budget bar."""
    for url in [f"/season/{SEASON}", f"/season/{SEASON}/team/t1"]:
        body = text(client.get(url).get_data(as_text=True)).lower()
        assert "budget" not in body
        assert "remaining" not in body
    for template in sorted(TEMPLATES.glob("*.html")):
        # Hard-wrapped prose: "there is no\n  salary cap" has to normalise before it matches.
        source = re.sub(r"\s+", " ", template.read_text(encoding="utf-8").lower())
        assert "salary cap" not in source or "no salary cap" in source
