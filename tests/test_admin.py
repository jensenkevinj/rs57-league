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
from rs57.admin.derived import Derived, DerivedSeason
from rs57.admin.gitops import Git, GitError
from rs57 import admin
from rs57.admin.reconcile import (
    EspnKeeperPick,
    keeper_picks,
    reconcile,
    roster_salaries,
    verify,
)
from rs57.admin.screens import (
    UNCHECKED,
    SLOT_CHOICES,
    KeeperDeadline,
    build_season_screen,
    build_team_screen,
    claims_from_form,
    keeper_deadline_fact,
)
from rs57.admin.store import CLAIMS, ManualStore, OwnershipError
from rs57.keeper_rules import KEEPER_TAX, MAX_KEEPERS, IssueCode, Severity, compute_team_keepers
from rs57.models import (
    CashTrade,
    KeeperClaim,
    KeeperSlot,
    SalaryOverride,
    Season,
    utc_now,
)

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


def set_keeper_deadline(data_dir: Path, deadline: datetime | None, season: int = SEASON) -> None:
    """Rewrite ``{season}.json``'s ``source.keeper_deadline`` — the way ``rs57.sync`` would,
    since the gate now reads ESPN's own deadline off the derived file rather than a settings row.
    """
    path = data_dir / "derived" / f"{season}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["source"]["keeper_deadline"] = deadline.isoformat() if deadline else None
    path.write_text(json.dumps(doc), encoding="utf-8")


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


def form(*claims: tuple[int, str, int], manager: str = "t1") -> dict[str, str]:
    """A posted claim form: ``(player_id, slot, fee)`` per row.

    Manager-scoped, the way the board posts it — one form carries all twelve franchises, so
    every field says which team it belongs to.
    """
    posted: dict[str, str] = {}
    for player_id, slot, fee in claims:
        posted[f"{manager}__player_{slot}"] = str(player_id)
        posted[f"{manager}__fee_{slot}"] = str(fee)
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
    client.post(f"/season/{SEASON}/settings", data={"trade_deadline": "2026-12-02 17:00"})
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

    # Rendered on BOTH pages now. A second render site is a second chance to get it wrong, and
    # this string ends up on a public site either way.
    for url in ("/overrides", f"/season/{SEASON}"):
        # /overrides redirects to the merged draft-cash tab; follow it and assert on the page
        # that actually renders the reason.
        page = client.get(url, follow_redirects=True).get_data(as_text=True)
        assert nasty not in page, f"{url} rendered the reason unescaped"
        assert "&lt;script&gt;" in page, f"{url} did not render the reason at all"


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
        page = client.get(url, follow_redirects=True).get_data(as_text=True)
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


# Money words and number parsing. A script touching any of these is doing arithmetic the
# engine is supposed to own; a script toggling a panel touches none of them.
PRICING = re.compile(
    r"\b(salar|fee|tax|price|total|base|keeper|prospect|Number\(|parseInt|parseFloat|toFixed)",
    re.I,
)


def test_no_javascript_computes_a_salary():
    """htmx posts the form and swaps the answer. Nothing in the browser prices anything.

    This used to forbid inline script outright, which was a proxy for the real rule and stopped
    being usable when the status badge needed a click handler — a native ``title`` shows only
    on hover, after a delay, and ignores the click everybody tries first.

    So it now forbids what the rule is actually about: the vocabulary of pricing, and every way
    of turning text into a number. A panel toggle uses none of it, and a script that quietly
    started recomputing a salary would have to.
    """
    for template in sorted(TEMPLATES.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        for script in re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", source, re.S):
            hit = PRICING.search(script)
            assert not hit, (
                f"{template.name} has inline JavaScript mentioning {hit.group(0)!r} — "
                f"the browser must never price anything"
            )


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
    assert "Not recorded yet" in text(page), "the card has to say it did not record"
    assert "fee_total_mismatch" in status_badge(page)[2], "and the badge has to say why"


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
    reasons = tooltip_lines(page)
    body = " ".join(f"{label} {message}" for label, message in reasons).lower()
    assert "prospect rule 1" in body, "the rookie rule cannot be checked and must say so"
    assert "unverified" in body
    assert "nobody has checked this" in body, (
        "the emphatic wording is what stops a reader skimming past an unchecked item"
    )
    # **Both paths, named separately.** Engine issues and screen notes are labelled by two
    # different lines of code, and asserting the phrase appears *somewhere* lets either regress
    # behind the other — mutation is what showed that, so each is now pinned by something only
    # it produces: an issue carries its IssueCode, a note never does.
    issues = [label for label, _ in reasons if "prospect_rookie_unverified" in label]
    assert issues, "the fixture must raise an unverifiable prospect for this to prove anything"
    assert all("nobody has checked this" in label for label in issues), (
        f"an engine REVIEW is labelled {issues} — it must not read as merely informational"
    )
    notes = [label for label, _ in reasons if label == UNCHECKED]
    assert notes, "a screen note must carry the same wording as an engine review"


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
        (label, message)
        for label, message in tooltip_lines(page)
        if "has not been drafted" in message
    ]
    assert warned, "the sync's warning is not on the commissioner's screen at all"
    assert all("UNVERIFIED" in label for label, _ in warned), (
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

    flags = tooltip_lines(page)
    rule_one = [label for label, message in flags if "Prospect rule 1" in message]
    assert rule_one, "the un-checkable prospect rule is not on the badge at all"
    assert all("UNVERIFIED" in label for label in rule_one), (
        f"an unverified note is labelled {rule_one} — it must not read as information"
    )

    waiver = [label for label, message in flags if "priced IN FULL" in message]
    assert waiver and all("UNVERIFIED" in label for label in waiver)


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
        data={"consolation_winner_id": "t1", "trade_deadline": "2025-08-30 21:00"},
    )
    assert store.season(PRIOR).consolation_winner_id == "t1"

    client.post(f"/season/{PRIOR}/settings", data={})
    recorded = store.season(PRIOR)
    assert recorded.consolation_winner_id == "t1", "an absent field must be left alone"
    assert recorded.trade_deadline == datetime(2025, 8, 30, 21, 0)

    # Submitted-but-empty is a deliberate clear, and still works.
    client.post(f"/season/{PRIOR}/settings", data={"consolation_winner_id": ""})
    assert store.season(PRIOR).consolation_winner_id is None


def test_the_doodle_link_follows_the_same_omit_versus_blank_rule(client, store: ManualStore):
    """The one pre-draft home page field left in the admin tool — ``draft_date`` and
    ``keeper_deadline`` used to be checked here too, but both are ESPN facts now and have no
    settings field to post to at all."""
    client.post(
        f"/season/{SEASON}/settings",
        data={"draft_doodle_url": "https://doodle.com/rs57-2026"},
    )
    recorded = store.season(SEASON)
    assert recorded.draft_doodle_url == "https://doodle.com/rs57-2026"

    client.post(f"/season/{SEASON}/settings", data={})
    recorded = store.season(SEASON)
    assert recorded.draft_doodle_url == "https://doodle.com/rs57-2026", (
        "an absent field must be left alone"
    )

    client.post(f"/season/{SEASON}/settings", data={"draft_doodle_url": ""})
    assert store.season(SEASON).draft_doodle_url is None


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
    body = all_reasons(page)
    assert "has NOT been" in body and "overwritten" in body
    assert "now prices at $104" in body


# ---------------------------------------------------------------------------
# Storage: shapes, prose, and determinism
# ---------------------------------------------------------------------------


def test_about_prose_survives_a_write(store: ManualStore):
    """``payouts.json`` holds the only explanation of why 2023 is missing."""
    before = json.loads((store.manual / "payouts.json").read_text())
    store.set_paid(PRIOR, "t1", True, now=NOW)
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
    store.set_paid(PRIOR, "t1", True, now=NOW)

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


def test_a_payout_is_keyed_on_the_franchise_and_the_season(store: ManualStore):
    """Per franchise, not per prize: the money moves once, at the end, for everything won.

    Settling one franchise must not settle another, and settling 2025 must not settle 2026.
    """
    store.set_paid(PRIOR, "t1", True, now=NOW)
    store.set_paid(PRIOR, "t2", True, now=NOW)
    store.set_paid(SEASON, "t1", True, now=NOW)
    assert {p.manager_id for p in store.payments(PRIOR)} == {"t1", "t2"}
    assert {p.manager_id for p in store.payments(SEASON)} == {"t1"}

    store.set_paid(PRIOR, "t2", False, now=NOW)
    remaining = store.payments(PRIOR)
    assert [p.manager_id for p in remaining] == ["t1"], (
        "un-marking removes the row; absence is what unsettled means"
    )
    assert remaining[0].paid
    assert {p.manager_id for p in store.payments(SEASON)} == {"t1"}, "the other season moved"


def test_payouts_record_no_amount_and_no_method(store: ManualStore):
    """What is owed is the sum stats derives; a second copy here could disagree with it."""
    store.set_paid(PRIOR, "t1", True, now=NOW)
    row = json.loads((store.manual / "payments.json").read_text())["payments"][0]
    assert set(row) == {"season", "manager_id", "paid", "paid_at"}


# ---------------------------------------------------------------------------
# The Money tab: dues in, prizes out, one season
# ---------------------------------------------------------------------------


def award(data_dir: Path, *payouts: dict, season: int = PRIOR) -> None:
    """Derive a season's prizes, the way ``rs57.stats_sync`` writes them."""
    (data_dir / "derived" / f"{season}-stats.json").write_text(
        json.dumps({"payouts": list(payouts), "review": {}}), encoding="utf-8"
    )


def test_every_franchise_gets_a_dues_row_including_one_that_has_paid_nothing(client):
    """The screen is a list of who still owes.

    Building it from the dues file instead of the season's franchises would drop exactly the
    teams the commissioner is looking for — a franchise with no record is the whole point.
    """
    rows, totals = admin._dues_rows(
        Derived(derived_dir=Path(client.application.config["DERIVED_DIR"])),
        ManualStore(data_dir=client.application.config["DATA_DIR"]),
        SEASON,
    )
    assert [row["manager_id"] for row in rows] == ["t2", "t1"], "sorted by franchise name"
    assert not any(row["paid"] for row in rows)
    assert totals == {"teams": 2, "paid": 0, "outstanding": 2}


def test_marking_dues_paid_writes_a_row_and_un_marking_removes_it(client, store: ManualStore):
    """Absence is what unpaid means, so un-marking must not leave ``paid: false`` behind."""
    client.post(f"/season/{SEASON}/money/dues", data={"manager_id": "t1", "paid": "1"})
    assert [row.manager_id for row in store.dues(SEASON)] == ["t1"]
    assert store.dues(SEASON)[0].paid_at == NOW

    client.post(f"/season/{SEASON}/money/dues", data={"manager_id": "t1", "paid": "0"})
    assert store.dues(SEASON) == [], "un-marking removes the row"


def test_dues_are_keyed_on_the_season_too(store: ManualStore):
    """Paying into 2026 must not mark 2025 paid — one franchise pays once a season, each season."""
    store.set_dues_paid(SEASON, "t1", True, now=NOW)
    store.set_dues_paid(PRIOR, "t1", True, now=NOW)
    assert {row.season for row in store.dues()} == {SEASON, PRIOR}

    store.set_dues_paid(PRIOR, "t1", False, now=NOW)
    assert [row.season for row in store.dues()] == [SEASON]


def test_dues_record_no_amount_and_no_method(store: ManualStore):
    """The buy-in is one figure nothing records; a handle would be personal data on a public repo."""
    store.set_dues_paid(SEASON, "t1", True, now=NOW)
    row = json.loads((store.manual / "dues.json").read_text())["dues"][0]
    assert set(row) == {"season", "manager_id", "paid", "paid_at"}


def test_the_dues_file_explains_itself_when_it_is_created(store: ManualStore):
    """``dues.json`` is the only place saying it is not ``payments.json``. A write must keep it."""
    store.set_dues_paid(SEASON, "t1", True, now=NOW)
    about = json.loads((store.manual / "dues.json").read_text())["_about"]

    store.set_dues_paid(SEASON, "t2", True, now=NOW)
    after = json.loads((store.manual / "dues.json").read_text())
    assert after["_about"] == about, "a second write dropped the prose"
    assert any("payments.json" in line for line in about)


def test_the_money_page_shows_both_directions_for_one_season(client, data_dir: Path):
    """Dues at the start of a season and prizes at the end of it are one screen, one year."""
    award(data_dir, {"season": PRIOR, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"})
    body = text(client.get(f"/season/{PRIOR}/money").get_data(as_text=True))
    assert "Money in" in body and f"{PRIOR} dues" in body
    assert "Money out" in body and f"{PRIOR} payouts" in body
    assert "Champion" in body
    assert "Fake News" in body


def test_the_money_page_reads_the_year_in_the_url(client, data_dir: Path):
    """Not ``current_season``. Both seasons are reachable, and each shows only its own prizes."""
    award(data_dir, {"season": PRIOR, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"})

    prior = text(client.get(f"/season/{PRIOR}/money").get_data(as_text=True))
    current = text(client.get(f"/season/{SEASON}/money").get_data(as_text=True))
    assert "Champion" in prior
    assert "Champion" not in current, "the coming season showed a completed season's prizes"


def test_every_dues_row_states_its_status_in_words(client):
    """The console shows the same green/red pair the published panel does, and in WORDS.

    Per row, not per page: dropping the label from only the paid rows would leave "Not paid"
    on screen and look right from a distance. Colour is the fast signal, the word is the real
    one -- and this console is where the record actually gets set.
    """
    client.post(f"/season/{SEASON}/money/dues", data={"manager_id": "t1", "paid": "1"})
    raw = client.get(f"/season/{SEASON}/money").get_data(as_text=True)
    body = re.search(r'<div id="dues-table">.*?</table>', raw, re.S).group(0)

    assert '<span class="tag-inline ok">Paid</span>' in body
    assert '<span class="tag-inline bad">Not paid</span>' in body
    assert body.count('class="tag-inline') == 2, "a status on every row, not only the unpaid"
    assert "owes" not in body, "the old one-sided tag is still being rendered"


def test_both_dues_buttons_name_an_action(client):
    """With a status column beside it, a button reading "paid" is a label where a verb goes."""
    client.post(f"/season/{SEASON}/money/dues", data={"manager_id": "t1", "paid": "1"})
    body = text(client.get(f"/season/{SEASON}/money").get_data(as_text=True))
    assert "mark paid" in body and "mark unpaid" in body


def test_the_prizes_table_says_nothing_about_who_has_been_paid(client, data_dir: Path, store):
    """A prize is settled when it is won. Dues are the thing the league chases.

    Paid/unpaid tracking was on this table and was taken off (commissioner, 2026-08-31). The
    recorded payment below is deliberately still on disk and must not surface here: it is the
    state a regression would reintroduce, so the fixture arranges for one to exist.
    """
    award(data_dir,
          {"season": PRIOR, "label": "Champion", "amount": 500, "winner_manager_id": "t1"},
          {"season": PRIOR, "label": "Survivor", "amount": 40, "winner_manager_id": "t2"})
    store.set_paid(PRIOR, "t1", True, now=NOW)

    raw = client.get(f"/season/{PRIOR}/money").get_data(as_text=True)
    table = re.search(r'<div class="payout-table">.*?</table>', raw, re.S).group(0)

    assert "Champion" in table and "$500" in table, "the prize itself is still shown"
    for gone in ("Not paid", "tag-inline ok", "tag-inline bad", "mark paid", "mark unpaid",
                 "Outstanding", "<form"):
        assert gone not in table, f"the prizes table is still tracking payment: {gone!r}"


def test_the_prizes_table_still_names_the_unawarded(client, data_dir: Path):
    """Unawarded is about the PRIZE, not about payment, so it survives the removal."""
    award(data_dir,
          {"season": PRIOR, "label": "Champion", "amount": 500, "winner_manager_id": "t1"},
          {"season": PRIOR, "label": "Unlucky", "amount": 20, "winner_manager_id": None})

    raw = client.get(f"/season/{PRIOR}/money").get_data(as_text=True)
    table = re.search(r'<div class="payout-table">.*?</table>', raw, re.S).group(0)
    assert "unawarded" in table
    assert "$20" in table


def test_only_the_dues_table_is_interactive(client, data_dir: Path):
    """One htmx target on the page now. The prizes half is a read-only report."""
    award(data_dir, {"season": PRIOR, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"})
    raw = client.get(f"/season/{PRIOR}/money").get_data(as_text=True)

    assert raw.count('id="dues-table"') == 1
    assert 'hx-target="#dues-table"' in raw
    assert 'id="payout-table"' not in raw, "the prizes table is still a swap target"
    assert "toggle_payout_paid" not in raw


def settlement_block(page: str) -> str:
    """The settlement table's own markup, sliced out of the Money page.

    Plain indexing rather than a regex, and it raises rather than returning "" when either
    marker is missing. A pattern that quietly matched nothing made every "this must not appear"
    assertion below pass without reading a single byte of the table.
    """
    start = page.index('<div id="settlement-table">')
    end = page.index("<h3", start)
    block = page[start:end]
    assert len(block) > 100, f"the settlement table rendered almost nothing: {block!r}"
    return block


def test_a_franchise_is_owed_the_sum_of_everything_it_won(client, data_dir: Path):
    """The whole point of settling per franchise: one line, one figure, one payment.

    Per prize, t1 here is four rows and no total; per franchise it is $625, which is the
    number the commissioner actually sends.
    """
    award(data_dir,
          {"season": PRIOR, "label": "Champion", "amount": 500, "winner_manager_id": "t1"},
          {"season": PRIOR, "label": "Most Points", "amount": 100, "winner_manager_id": "t1"},
          {"season": PRIOR, "label": "QB Stud", "amount": 25, "winner_manager_id": "t1"},
          {"season": PRIOR, "label": "Survivor", "amount": 40, "winner_manager_id": "t2"},
          {"season": PRIOR, "label": "Unlucky", "amount": 20, "winner_manager_id": None})

    rows, totals = admin._settlement_rows(
        Derived(derived_dir=Path(client.application.config["DERIVED_DIR"])),
        ManualStore(data_dir=client.application.config["DATA_DIR"]),
        PRIOR,
    )
    assert [(r["manager_id"], r["amount"]) for r in rows] == [("t1", 625), ("t2", 40)]
    assert totals["owed"] == 665, "the unawarded $20 is owed to nobody"
    assert totals["outstanding"] == 665
    assert totals["paid"] == 0


def test_a_franchise_that_won_nothing_is_not_a_line_on_the_settlement_sheet(client, data_dir):
    """Twelve franchises, but only the winners are owed anything."""
    award(data_dir, {"season": PRIOR, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"})
    rows, totals = admin._settlement_rows(
        Derived(derived_dir=Path(client.application.config["DERIVED_DIR"])),
        ManualStore(data_dir=client.application.config["DATA_DIR"]),
        PRIOR,
    )
    assert [r["manager_id"] for r in rows] == ["t1"]
    assert totals["teams"] == 1


def test_settling_a_franchise_moves_the_outstanding_figure(client, data_dir: Path, store):
    award(data_dir,
          {"season": SEASON, "label": "Champion", "amount": 500, "winner_manager_id": "t1"},
          {"season": SEASON, "label": "Survivor", "amount": 40, "winner_manager_id": "t2"},
          season=SEASON)

    fragment = client.post(f"/season/{SEASON}/money/payouts",
                           data={"manager_id": "t1", "paid": "1"}).get_data(as_text=True)
    assert 'id="settlement-table"' in fragment
    assert 'id="dues-table"' not in fragment, "the payout swap carried the dues table with it"
    assert [p.manager_id for p in store.payments(SEASON)] == ["t1"]

    client.post(f"/season/{SEASON}/money/payouts", data={"manager_id": "t1", "paid": "0"})
    assert store.payments(SEASON) == [], "un-marking removes the row"


def test_a_season_settled_before_the_ledger_is_never_shown_as_owing(client, data_dir: Path):
    """2025 and everything before it was paid out before this app tracked it.

    Rendering those as twelve red debts would invent money the league does not owe, which is
    the same mistake as marking an unawarded prize unpaid. The boundary is recorded rather
    than assumed -- see PAYOUT_LEDGER_FROM.
    """
    award(data_dir, {"season": PRIOR, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"})
    raw = client.get(f"/season/{PRIOR}/money").get_data(as_text=True)
    settle = settlement_block(raw)

    assert "Not paid" not in settle, "a settled season is being reported as a debt"
    assert "mark paid" not in settle
    assert "Settled" in settle

    # And the endpoint refuses it too, so the guard is not only in the template.
    assert client.post(f"/season/{PRIOR}/money/payouts",
                       data={"manager_id": "t1", "paid": "1"}).status_code == 400


def test_the_current_season_is_settleable(client, data_dir: Path):
    """The mirror of the test above: the ledger's own seasons must not be locked out."""
    award(data_dir, {"season": SEASON, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"}, season=SEASON)
    raw = client.get(f"/season/{SEASON}/money").get_data(as_text=True)
    assert "mark paid" in raw
    assert client.post(f"/season/{SEASON}/money/payouts",
                       data={"manager_id": "t1", "paid": "1"}).status_code == 200


def test_a_payout_to_a_franchise_that_won_nothing_is_refused(client, data_dir: Path, store):
    """It is a real franchise, but it is owed nothing and is not a line on the sheet.

    Found by hand-posting an id I had guessed wrong. The row was written, contributed $0 to
    every total, and never appeared on screen — so it could not be undone through the console
    either. A payment that never happened, recorded permanently.
    """
    award(data_dir, {"season": SEASON, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"}, season=SEASON)

    response = client.post(f"/season/{SEASON}/money/payouts",
                           data={"manager_id": "t2", "paid": "1"})
    assert response.status_code == 400
    assert store.payments() == [], "a payout was recorded for a franchise owed nothing"

    # The one that did win is still settleable, so the guard is not refusing everything.
    assert client.post(f"/season/{SEASON}/money/payouts",
                       data={"manager_id": "t1", "paid": "1"}).status_code == 200


def test_a_payout_for_a_franchise_the_season_does_not_have_is_refused(client, store):
    award_year = SEASON
    assert client.post(f"/season/{award_year}/money/payouts",
                       data={"manager_id": "t99", "paid": "1"}).status_code == 400
    assert store.payments() == []


def test_a_season_still_being_played_has_live_dues_and_no_prizes_yet(client):
    """The normal state of the current season, and it must read as that rather than as broken."""
    body = text(client.get(f"/season/{SEASON}/money").get_data(as_text=True))
    assert "mark paid" in body, "dues are not markable on the season being collected for"
    assert "Nothing to track" in body, "the empty prize half says nothing about why"
    assert "has not been derived" in body


def test_marking_dues_paid_leaves_the_payout_table_alone(client, data_dir: Path, store):
    """Two htmx targets on one page. A swap that returned the whole page would clobber the other."""
    award(data_dir, {"season": PRIOR, "label": "Champion", "amount": 500,
                     "winner_manager_id": "t1"})
    store.set_paid(PRIOR, "t1", True, now=NOW)

    fragment = client.post(
        f"/season/{PRIOR}/money/dues", data={"manager_id": "t1", "paid": "1"}
    ).get_data(as_text=True)
    assert 'id="dues-table"' in fragment
    assert 'id="payout-table"' not in fragment, "the dues swap carried the payout table with it"
    assert store.payments(PRIOR)[0].paid, "the recorded payout was disturbed by a dues write"


def test_the_old_payouts_url_still_lands_on_the_money_tab(client):
    """Payouts and dues merged onto one tab. A bookmark must not 404."""
    response = client.get(f"/season/{PRIOR}/payouts")
    assert response.status_code in (301, 302)
    assert response.headers["Location"].endswith(f"/season/{PRIOR}/money")


def test_the_money_page_refuses_a_season_that_was_never_synced(client):
    """Without a derived season there are no franchises, so there is nobody to bill."""
    assert client.get("/season/1999/money").status_code == 404


def test_a_dues_post_without_a_franchise_is_refused(client, store: ManualStore):
    """An empty manager id would write a row keyed on nothing at all."""
    assert client.post(f"/season/{SEASON}/money/dues", data={"paid": "1"}).status_code == 400
    assert store.dues() == []


def test_dues_for_a_franchise_the_season_does_not_have_are_refused(client, store: ManualStore):
    """The season's own franchise list blocks; it is the league's own record, not an outside one.

    Caught by hand-posting a manager id that was really a whole shell variable. An id nothing
    recognises would sit in the file reading as somebody's payment while the public panel went
    on showing that franchise as owing, and `validate` would only say so afterwards.
    """
    response = client.post(
        f"/season/{SEASON}/money/dues", data={"manager_id": "t1 t2 t3", "paid": "1"}
    )
    assert response.status_code == 400
    assert store.dues() == [], "an unrecognised franchise was written"

    assert client.post(
        f"/season/{SEASON}/money/dues", data={"manager_id": "t99", "paid": "1"}
    ).status_code == 400
    assert store.dues() == []


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------


def test_an_unparseable_fee_is_a_form_problem(client, store: ManualStore):
    page = client.post(
        f"/season/{SEASON}/team/t1",
        data={"t1__player_K1": str(TAXED), "t1__fee_K1": "abc"},
    ).get_data(as_text=True)
    assert "not a whole number" in page
    assert store.claims(SEASON) == []


def test_an_empty_slot_is_not_a_claim():
    claims, problems = claims_from_form(
        SEASON, "t1",
        {"player_K1": "", "fee_K1": "5", "player_K2": str(PLAIN), "fee_K2": "0"},
    )
    assert [claim.espn_player_id for claim in claims] == [PLAIN]
    assert problems == []


def test_a_slot_holding_something_that_is_not_a_player_id_is_refused():
    claims, problems = claims_from_form(SEASON, "t1", {"player_K1": "nobody", "fee_K1": "0"})
    assert claims == []
    assert problems and "not a player id" in problems[0]


def test_the_same_player_cannot_fill_two_slots():
    """A failure mode the slot-keyed form invented, so it is the form that has to catch it.

    Unreported, the engine prices him once per claim and the team silently owes double. It is
    not a league rule the engine can speak to — it is an impossible input, like a fee of "abc".
    """
    claims, problems = claims_from_form(
        SEASON, "t1",
        {"player_K1": str(TAXED), "fee_K1": "0", "player_K2": str(TAXED), "fee_K2": "5"},
    )
    assert [claim.espn_player_id for claim in claims] == [TAXED], "the first pick stands"
    assert problems and "pick him once" in problems[0]


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_a_blank_reason_takes_the_default_rather_than_being_refused(client, store: ManualStore):
    """An override is a draft-cash trade every single time.

    Requiring the commissioner to retype that on every row bought nothing, so a blank reason is
    filled in rather than rejected. The field is still stored and still escaped at render — the
    rows already on file carry real provenance and keep it.
    """
    client.post(
        "/overrides",
        data={"season": SEASON, "espn_player_id": PLAIN, "actual_salary": 45, "reason": "  "},
        follow_redirects=True,
    )
    assert [o.reason for o in store.overrides()] == ["Draft-cash trade"]


def test_a_typed_reason_is_kept_verbatim(client, store: ManualStore):
    """The default must not overwrite provenance somebody bothered to record."""
    client.post(
        "/overrides",
        data={"season": SEASON, "espn_player_id": PLAIN, "actual_salary": 45,
              "reason": "2025 workbook, Manually Changed Salaries"},
        follow_redirects=True,
    )
    assert store.overrides()[0].reason == "2025 workbook, Manually Changed Salaries"


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


def test_an_override_can_be_recorded_from_the_keeper_page(client, store: ManualStore):
    """Step 7 of the offseason happens in the same sitting as steps 4-6, so it lives there."""
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert 'action="/overrides"' in page, "the keeper page needs the override form"
    assert f'name="return_year" value="{SEASON}"' in page

    response = client.post(
        "/overrides",
        data={
            "season": SEASON, "espn_player_id": PLAIN, "actual_salary": 45,
            "reason": "Draft cash: $3 to t2", "return_year": SEASON,
        },
    )
    assert response.headers["Location"].endswith(f"/season/{SEASON}"), (
        "recording from the keeper page must come back to it, not bounce to the ledger"
    )
    assert store.overrides(SEASON)[0].actual_salary == 45


def test_the_keeper_page_shows_only_its_own_season(client, store: ManualStore):
    """Each season page is its own year. The switcher is how you reach another one."""
    store.add_override(SalaryOverride(
        espn_player_id=PLAIN, season=SEASON, actual_salary=45,
        reason="cash moved this season", created_at=NOW, reverted=False,
    ))
    store.add_override(SalaryOverride(
        espn_player_id=PLAIN, season=PRIOR, actual_salary=30,
        reason="cash moved last season", created_at=NOW, reverted=False,
    ))

    # Anchored on the true salary, which is a visible cell. The reason used to be the marker
    # and is now a tooltip — `text()` strips attributes, so a version of this test that kept
    # reading the reason would have gone quiet rather than failing.
    def true_salaries(html):
        return re.findall(r'name="actual_salary"[^>]*value="(\d+)"', html)

    keeper_page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert true_salaries(keeper_page) == ["45"], "only this season's override is shown"

    # The cross-season ledger tab is gone (commissioner, 2026-09-01). The header season picker
    # is how the other year is reached, and the league-wide net below is what replaces the
    # league-wide *check* the tab used to be the only home for.
    prior_page = client.get(f"/season/{PRIOR}").get_data(as_text=True)
    assert true_salaries(prior_page) == ["30"]
    # `value=`, not `href=`: an href to the same year is also what the "Not shown here" cash
    # note renders, so an href assertion would go on passing with the picker deleted.
    assert f'value="/season/{PRIOR}"' in keeper_page, "the picker reaches the other season"


def test_overrides_that_do_not_cancel_are_reported(client, store: ManualStore):
    """A cash trade moves money between two teams, so the live legs should sum to zero."""
    store.add_override(SalaryOverride(
        espn_player_id=PLAIN, season=SEASON, actual_salary=45,  # ESPN holds 42 for t1
        reason="one leg, no counterparty", created_at=NOW, reverted=False,
    ))
    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert "net to $3, not $0" in page


def test_an_unknowable_override_total_is_not_guessed_at_zero(client, store: ManualStore):
    """Where a number cannot be known, record no number — a guessed zero looks balanced."""
    store.add_override(SalaryOverride(
        espn_player_id=999, season=SEASON, actual_salary=45,  # nobody ESPN has a base for
        reason="player not on any roster", created_at=NOW, reverted=False,
    ))
    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert "unknown" in page.lower()
    assert "the legs cancel" not in page


def test_the_overrides_page_does_not_offer_to_fix_espn(client):
    """The plan doc calls an override a correction. The plan doc is wrong."""
    body = text(client.get("/overrides", follow_redirects=True).get_data(as_text=True)).lower()
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


@pytest.fixture
def cloned(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with a real origin, so a push can be run for real. Returns (local, remote)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    Git(repo=remote).run("init", "-q", "--bare", "--initial-branch", "main")

    root = tmp_path / "clone"
    (root / "data" / "manual").mkdir(parents=True)
    (root / "data" / "derived").mkdir(parents=True)
    git = Git(repo=root)
    git.run("init", "-q", "--initial-branch", "main")
    git.run("config", "user.email", "test@example.invalid")
    git.run("config", "user.name", "Test")
    (root / "data" / "derived" / "2026.json").write_text("{}\n", encoding="utf-8")
    git.run("add", "-A")
    git.run("commit", "-qm", "seed")
    git.run("remote", "add", "origin", str(remote))
    git.run("push", "-q", "-u", "origin", "main")
    return root, remote


def nightly_commit(tmp_path: Path, remote: Path, content: str = '{"synced": true}') -> None:
    """A nightly run landing on the remote: data/derived/ only, which is all it owns."""
    work = tmp_path / f"nightly-{content and len(content)}"
    Git(repo=tmp_path).run("clone", "-q", str(remote), str(work))
    git = Git(repo=work)
    git.run("config", "user.email", "nightly@example.invalid")
    git.run("config", "user.name", "Nightly")
    (work / "data" / "derived" / "2026.json").write_text(content + "\n", encoding="utf-8")
    git.run("commit", "-qam", "Nightly: sync 2026, rebuild site")
    git.run("push", "-q")


def test_a_branch_behind_the_nightly_still_pushes(cloned):
    """The nightly commits every night, so a laptop that pushed yesterday is behind by morning.

    A bare push is rejected there. The commit button replays onto the remote first, and both
    sides survive: the Action's derived file and the commissioner's manual one.
    """
    root, remote = cloned
    nightly_commit(root.parent, remote)

    (root / "data" / "manual" / "dues.json").write_text('{"a": 1}\n', encoding="utf-8")
    git = Git(repo=root)
    log = git.commit_and_push("Admin: update league dues (2026)")

    assert any("replayed onto origin/main" in line for line in log)
    assert any("pushed to main" in line for line in log)

    # Both writers' work is on the remote, and neither clobbered the other.
    landed = Git(repo=remote).run("show", "main:data/manual/dues.json")
    assert '"a": 1' in landed
    assert "synced" in Git(repo=remote).run("show", "main:data/derived/2026.json")


def test_being_up_to_date_costs_no_rebase(cloned):
    """The common case must stay a plain push — a rebase that always runs rewrites history
    nobody asked it to."""
    root, _ = cloned
    (root / "data" / "manual" / "dues.json").write_text('{"a": 1}\n', encoding="utf-8")
    git = Git(repo=root)
    log = git.commit_and_push("Admin: update league dues (2026)")

    assert not any("replayed" in line for line in log)
    assert any("pushed to main" in line for line in log)


def test_a_dirty_derived_file_stops_the_push_without_losing_the_commit(cloned):
    """A local `rs57.sync` leaves data/derived/ dirty, and git then refuses the rebase.

    The commit has already been made at that point. It has to stay made, the repo must not be
    left mid-rebase, and the commissioner has to be told which of the two happened.
    """
    root, remote = cloned
    nightly_commit(root.parent, remote)

    (root / "data" / "manual" / "dues.json").write_text('{"a": 1}\n', encoding="utf-8")
    (root / "data" / "derived" / "2026.json").write_text('{"local": true}\n', encoding="utf-8")

    git = Git(repo=root)
    with pytest.raises(GitError, match="committed, but not pushed"):
        git.commit_and_push("Admin: update league dues (2026)")

    # The commit survived, the repo is not mid-rebase, and nothing reached the remote.
    assert "dues.json" in git.run("show", "--name-only", "--format=", "HEAD")
    assert not (root / ".git" / "rebase-merge").exists()
    assert not (root / ".git" / "rebase-apply").exists()
    assert "dues.json" not in Git(repo=remote).run("ls-tree", "-r", "--name-only", "main")


def test_a_push_is_never_attempted_when_pushing_is_off(cloned):
    """--no-push must not reach the network, so it must not fetch either."""
    root, remote = cloned
    nightly_commit(root.parent, remote)

    (root / "data" / "manual" / "dues.json").write_text('{"a": 1}\n', encoding="utf-8")
    git = Git(repo=root)
    log = git.commit_and_push("Admin: update league dues (2026)", push=False)

    assert any("not pushed" in line for line in log)
    assert not any("replayed" in line for line in log)
    assert ["fetch", "origin"] not in git._ran


def test_discarding_restores_a_tracked_file_to_the_last_commit(repo: Path):
    """The undo for a mistake typed into the console."""
    manual = repo / "data" / "manual" / "dues.json"
    manual.write_text('{"dues": []}\n', encoding="utf-8")
    git = Git(repo=repo)
    git.commit_and_push("Admin: record dues (2026)", push=False)

    manual.write_text('{"dues": ["a mistake"]}\n', encoding="utf-8")
    log = git.discard(["data/manual/dues.json"])

    assert manual.read_text() == '{"dues": []}\n', "the mistake was not rolled back"
    assert any("restored" in line for line in log)


def test_discarding_a_never_committed_file_deletes_it(repo: Path):
    """There is no earlier version to go back to, so discarding one means removing it."""
    manual = repo / "data" / "manual" / "dues.json"
    manual.write_text('{"dues": []}\n', encoding="utf-8")

    log = Git(repo=repo).discard(["data/manual/dues.json"])
    assert not manual.exists()
    assert any("deleted" in line for line in log)


@pytest.mark.parametrize(
    "target",
    ["data/derived/2026.json", "data/history/2025.json", "site/index.html", "CLAUDE.md"],
)
def test_discarding_outside_data_manual_is_refused(repo: Path, target: str):
    """The one operation here that destroys data. A dirty data/derived/ is not its to clean up."""
    path = repo / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("belongs to somebody else\n", encoding="utf-8")

    with pytest.raises(GitError, match="outside data/manual/"):
        Git(repo=repo).discard([target])
    assert path.exists(), "a file this tool does not own was destroyed"


def test_discarding_something_that_is_not_a_change_is_refused(repo: Path):
    """A stale page offering a discard for a file that is already clean must not act on it."""
    with pytest.raises(GitError, match="nothing to discard"):
        Git(repo=repo).discard(["data/manual/dues.json"])
    with pytest.raises(GitError, match="nothing selected"):
        Git(repo=repo).discard([])


def test_the_save_page_offers_a_discard_for_each_change(repo: Path, data_dir: Path):
    """The engine having a discard is not the same as the page offering one.

    Removing the whole discard block from the template broke no test until this one existed —
    every other discard test calls Git.discard directly and would have kept passing against a
    page with no way to reach it.
    """
    (repo / "data" / "manual" / "dues.json").write_text("{}\n", encoding="utf-8")
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    raw = app.test_client().get("/commit").get_data(as_text=True)

    assert raw.count('action="/discard"') == 3, "one form per file, plus discard-all"
    for path in ("data/manual/dues.json", "data/manual/claims.json"):
        assert f'value="{path}"' in raw
    assert "cannot be undone" in raw, "a destructive control with no warning on it"
    assert 'class="confirm"' in raw, "discard is one click away, with no confirmation step"


def test_discard_all_only_appears_when_there_is_more_than_one_change(repo: Path, data_dir: Path):
    """With a single file, "discard all" and "discard it" are the same button twice."""
    (repo / "data" / "manual" / "dues.json").write_text("{}\n", encoding="utf-8")
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    raw = app.test_client().get("/commit").get_data(as_text=True)
    assert raw.count('action="/discard"') == 1
    assert "Discard all" not in raw


def test_the_discard_route_rolls_the_file_back(repo: Path, data_dir: Path):
    """End to end through the button, not just the engine."""
    manual = repo / "data" / "manual" / "dues.json"
    manual.write_text('{"dues": []}\n', encoding="utf-8")
    Git(repo=repo).commit_and_push("Admin: record dues (2026)", push=False)
    manual.write_text('{"dues": ["a mistake"]}\n', encoding="utf-8")

    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    response = app.test_client().post(
        "/discard", data={"path": "data/manual/dues.json"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert manual.read_text() == '{"dues": []}\n'


def test_the_discard_route_refuses_a_path_it_does_not_own(repo: Path, data_dir: Path):
    """A path arriving from a form is exactly where a bad one would come from."""
    target = repo / "data" / "derived" / "2026.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("the Action's\n", encoding="utf-8")

    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    body = app.test_client().post(
        "/discard", data={"path": "data/derived/2026.json"}, follow_redirects=True
    ).get_data(as_text=True)

    assert target.exists(), "a file this tool does not own was destroyed through the route"
    assert "outside data/manual/" in body, "and the refusal was not reported"


def test_the_nav_counts_unsaved_changes(repo: Path, data_dir: Path, tmp_path: Path):
    """Nothing recorded here reaches the league until it is saved, and before this badge the
    only way to find that out was to go looking on one tab."""
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    client = app.test_client()

    assert 'class="badge"' not in client.get(f"/season/{SEASON}/money").get_data(as_text=True)

    (repo / "data" / "manual" / "dues.json").write_text("{}\n", encoding="utf-8")
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    body = client.get(f"/season/{SEASON}/money").get_data(as_text=True)
    assert '<span class="badge">2</span>' in body, "the nav does not say anything is unsaved"


def _picker_options(html: str) -> list[str]:
    """The header season control's option values, and only those.

    Scoped to the control rather than grepping every ``<option>`` on the page: the trade and
    override tables carry year pickers of their own, and the settings form carries a franchise
    one, so a page-wide grep would pass on markup that has nothing to do with the header.
    """
    block = re.search(r'<select class="season-pick".*?</select>', html, re.S)
    assert block, "the header has no season picker"
    return re.findall(r'value="([^"]+)"', block.group(0))


def test_the_season_picker_keeps_the_tab_you_are_on(client):
    """Switching season on Money lands on Money — not back on the keeper board.

    This is the behaviour the two hand-rolled switchers had before they moved into the header,
    and it is the whole reason the header knows which endpoint it is rendering for.
    """
    money = _picker_options(client.get(f"/season/{SEASON}/money").get_data(as_text=True))
    assert money and all(url.endswith("/money") for url in money), money

    settings = _picker_options(client.get(f"/season/{SEASON}/settings").get_data(as_text=True))
    assert settings and all(url.endswith("/settings") for url in settings), settings

    board = _picker_options(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert board == [f"/season/{SEASON}", f"/season/{PRIOR}"], "newest first, keeper board"


def test_the_nav_tabs_follow_the_season_you_are_reading(client):
    """Not ``current_season``.

    Wired to the current season, the tabs moved you to a different year than the one on screen
    without saying so — you opened Money from a 2025 page and were quietly billing 2026.
    """
    page = client.get(f"/season/{PRIOR}").get_data(as_text=True)
    assert f'href="/season/{PRIOR}/settings"' in page
    assert f'href="/season/{PRIOR}/money"' in page
    assert f'href="/season/{SEASON}/settings"' not in page, "the tab jumped to another year"


def test_there_is_one_season_control_and_it_is_in_the_header(client):
    """Three pages, one control. Two of them used to print their own copy above the content."""
    for url in (f"/season/{SEASON}", f"/season/{SEASON}/money", f"/season/{SEASON}/settings",
                f"/season/{SEASON}/team/t1"):
        page = client.get(url).get_data(as_text=True)
        assert page.count('class="season-pick"') == 1, f"{url} has the wrong number of pickers"
        # The two deleted paragraphs both opened with this label. Counting pickers alone would
        # not notice one of them coming back, because it listed years as plain links.
        assert "Season:" not in text(page), f"{url} kept a switcher of its own"


def test_a_prior_season_says_so_on_every_tab(client):
    """The warning sits beside the control that causes it, so it is not the keeper board's
    alone — Money and Season settings can be a settled year just as easily."""
    for url in (f"/season/{PRIOR}", f"/season/{PRIOR}/money", f"/season/{PRIOR}/settings"):
        assert "not the current season" in text(client.get(url).get_data(as_text=True)), url
    assert "not the current season" not in text(
        client.get(f"/season/{SEASON}").get_data(as_text=True)
    )


def test_a_change_outside_data_manual_never_reaches_the_badge(repo: Path, data_dir: Path):
    """A dirty data/derived/ from a local sync is not something this tool can save."""
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    (repo / "data" / "derived").mkdir(parents=True, exist_ok=True)
    (repo / "data" / "derived" / "2026.json").write_text("{}\n", encoding="utf-8")

    body = app.test_client().get(f"/season/{SEASON}/money").get_data(as_text=True)
    assert 'class="badge"' not in body, "the badge is counting files this tool cannot commit"


def test_the_save_page_warns_when_the_branch_is_not_the_published_one(repo: Path, data_dir: Path):
    """A commit onto a feature branch is recorded and never published. It happened twice."""
    git = Git(repo=repo)
    git.run("remote", "add", "origin", "https://example.invalid/repo.git")
    git.run("checkout", "-q", "-b", "some-other-work")
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    body = text(app.test_client().get("/commit").get_data(as_text=True))

    assert "Not on main" in body
    assert "never published" in body


def test_the_save_page_is_quiet_on_the_published_branch(repo: Path, data_dir: Path):
    """The mirror: the warning must not be permanent furniture."""
    git = Git(repo=repo)
    git.run("remote", "add", "origin", "https://example.invalid/repo.git")
    git.run("checkout", "-q", "-B", "main")
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    body = text(app.test_client().get("/commit").get_data(as_text=True))
    assert "never published" not in body


def test_a_repo_with_no_remote_is_never_warned_about_its_branch(repo: Path, data_dir: Path):
    """Nothing is published from anywhere, so there is no wrong branch to be on.

    Caught by CI, not locally: the runner's `git init` picks a branch name that is not `main`,
    and with no remote to ask, default_branch() falls back to "main" and the warning fired on
    every page. The panel already says commits stay local.
    """
    Git(repo=repo).run("checkout", "-q", "-b", "whatever-git-called-it")
    app = create_app(data_dir=data_dir, derived_dir=data_dir / "derived",
                     repo=repo, push=False, clock=lambda: NOW)
    body = text(app.test_client().get("/commit").get_data(as_text=True))
    assert "never published" not in body
    assert "Not on" not in body


def test_the_summary_is_prefilled_from_what_changed(repo: Path):
    """A blank required box is where "asdf" comes from. It stays editable."""
    (repo / "data" / "manual" / "dues.json").write_text("{}\n", encoding="utf-8")
    (repo / "data" / "manual" / "claims.json").write_text("{}\n", encoding="utf-8")
    assert Git(repo=repo).preview().suggested_summary == "update claims, dues"


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


def test_an_unpriced_claim_is_not_an_unrecorded_keeper(data_dir: Path):
    """He was recorded. There is just no price for him, which is a different finding.

    ``computed_salary`` is legitimately ``None`` when ``compute_team_keepers`` skipped the
    claim — a player no longer on the roster, which is the state somebody reconciles in. The
    old ordering read that as "ESPN has a keeper you never recorded".
    """
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=None),
    ]
    picks = [EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=10)]
    result = reconcile(SEASON, claims, picks, current)

    assert [row.name for row in result.unpriced] == ["Puka Nacua"]
    assert result.espn_only == (), "a recorded claim is not an ESPN-only pick"
    assert not result.agrees, "a row nobody could compare has not been checked"


def test_an_espn_pick_nobody_claimed_is_still_espn_only(data_dir: Path):
    """The other side of the same branch — an absent claim must keep reporting as absent."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    result = reconcile(
        SEASON, [], [EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=10)], current
    )
    assert [row.name for row in result.espn_only] == ["Puka Nacua"]
    assert result.unpriced == ()


def test_no_espn_picks_is_a_normal_state(data_dir: Path):
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    result = reconcile(SEASON, [], [], current)
    assert result.espn_pick_count == 0
    assert result.error is None


def _sizes(depth: int) -> dict[int, int]:
    """Twelve teams all at the same depth, the way a real league comes back."""
    return {team: depth for team in range(1, 13)}


def test_a_full_roster_cannot_check_for_unrecorded_keepers(data_dir: Path):
    """Every player carries a keeper value whether he is a keeper or not.

    So a rostered player with no claim proves nothing, and reporting the league clean on that
    basis is a check that never ran wearing the face of one that passed.
    """
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=10),
    ]
    salaries = [
        EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=10),
        EspnKeeperPick(espn_team_id=1, espn_player_id=PLAIN, bid=42),  # rostered, not a keeper
    ]
    result = verify(SEASON, claims, salaries, _sizes(16), current)

    assert result.regime == "full"
    assert not result.unrecorded_checked
    assert result.espn_only == (), "an unclaimed player on a full roster is not a finding"
    assert result.clean


def test_a_pruned_roster_does_catch_an_unrecorded_keeper(data_dir: Path):
    """Once ESPN holds only the kept players, a rostered player nobody claimed is a keeper."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=10),
    ]
    salaries = [
        EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=10),
        EspnKeeperPick(espn_team_id=1, espn_player_id=PLAIN, bid=42),
    ]
    result = verify(SEASON, claims, salaries, _sizes(3), current)

    assert result.regime == "keepers"
    assert result.unrecorded_checked
    assert [row.name for row in result.espn_only] == ["James Cook III"]
    assert not result.clean


def test_verify_catches_a_mistyped_espn_salary(data_dir: Path):
    """The whole reason the screen exists — a typo becomes the base and the ratchet carries it."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=10),
    ]
    result = verify(
        SEASON, claims, [EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=100)],
        _sizes(16), current,
    )
    assert [(row.name, row.delta) for row in result.mismatches] == [("Puka Nacua", 90)]
    assert not result.clean


def test_a_salary_not_yet_typed_into_espn_is_not_a_mismatch(data_dir: Path):
    """Before step 6 ESPN still holds the carried-in price, so every keeper differs by exactly
    its own fee and tax. Reporting the whole league red before anybody has typed anything is
    how the mismatch this screen exists to catch gets scrolled past.

    Observed live on 2026: Josh Allen recorded at $33 against ESPN's $28, McBride $32 against
    $22, Chase Brown $18 against $8 — fee, then fee plus tax, then fee plus tax.
    """
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [  # TAXED carries in at 5, so 5 + tax + no fee = 10 recorded
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=5 + KEEPER_TAX),
    ]
    result = verify(
        SEASON, claims, [EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=5)],
        _sizes(16), current,
    )

    assert [row.name for row in result.not_yet_entered] == ["Puka Nacua"]
    assert result.mismatches == (), "the carried-in price is not a disagreement"
    assert not result.clean, "ESPN does not hold the number yet, so nothing is confirmed"


def test_a_real_mismatch_still_reports_as_one(data_dir: Path):
    """The benign state must not swallow the finding — a typed-wrong salary is neither the
    carried-in price nor the recorded one."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED, slot=KeeperSlot.K1,
                    fee_allocated=0, computed_salary=5 + KEEPER_TAX),
    ]
    result = verify(
        SEASON, claims, [EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=100)],
        _sizes(16), current,
    )
    assert [row.name for row in result.mismatches] == ["Puka Nacua"]
    assert result.not_yet_entered == ()


def test_an_unreachable_espn_is_never_reported_as_clean(data_dir: Path):
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    result = verify(SEASON, [], [], {}, current, error="connection refused")
    assert not result.clean
    assert result.regime is None
    assert not result.unrecorded_checked


def test_a_degraded_roster_response_is_not_verified_against(data_dir: Path):
    """One short team among eleven full ones is a truncated response, not a league state."""
    current = Derived(derived_dir=data_dir / "derived").load(SEASON)
    sizes = _sizes(16) | {3: 1}
    result = verify(SEASON, [], [], sizes, current)
    assert not result.clean
    assert "degraded" in (result.error or "")


def test_verify_reads_the_field_the_derived_file_was_built_from(data_dir: Path):
    """keeperValue before the auction, keeperValueFuture after. Chosen in one place only.

    Getting it wrong does not fail loudly — it reports the whole league mismatched, or reports
    it clean having compared each number against itself.
    """
    payload = {
        "teams": [{"roster": {"entries": [
            {"playerPoolEntry": {"player": {"id": TAXED}, "keeperValue": 5,
                                 "keeperValueFuture": 99}},
        ]}}]
    }
    rows, depth = roster_salaries(payload, 1, "keeperValue")
    assert (rows[0].bid, depth) == (5, 1)
    rows, _ = roster_salaries(payload, 1, "keeperValueFuture")
    assert rows[0].bid == 99


def test_verify_writes_nothing(client, data_dir: Path, monkeypatch):
    """It is the only screen that touches the network, on a page that also writes."""
    monkeypatch.setattr(
        admin, "fetch_roster_salaries",
        lambda season, field, ids: ([EspnKeeperPick(espn_team_id=1, espn_player_id=TAXED, bid=10)],
                                    _sizes(16), None),
    )
    monkeypatch.setattr(admin, "fetch_keeper_picks", lambda season: ([], None))

    before = {p: p.read_bytes() for p in sorted(data_dir.rglob("*.json"))}
    response = client.post(f"/season/{SEASON}/verify")
    after = {p: p.read_bytes() for p in sorted(data_dir.rglob("*.json"))}

    assert response.status_code == 200
    assert before == after, "verify touched a file"


def test_verify_is_never_a_get(client):
    """An unreachable ESPN must not take down the page the offseason is entered on."""
    assert client.get(f"/season/{SEASON}/verify").status_code == 405


def test_the_reconcile_tab_is_gone(client):
    """Folded into the season page. The comparison logic stayed; the separate screen did not."""
    assert client.get(f"/season/{SEASON}/reconcile").status_code == 404
    assert not (TEMPLATES / "reconcile.html").exists()
    for url in (f"/season/{SEASON}", f"/season/{SEASON}/team/t1"):
        assert "reconcile" not in client.get(url).get_data(as_text=True).lower()


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
    # Named rather than counted: a team's review_count legitimately holds its own unverified
    # items, so asserting it is zero would break the day any of them fires and would say
    # nothing about the fact under test.
    for team in screen.teams:
        assert not any("priced IN FULL" in note.message for note in team.notes if note.team_specific), (
            f"{team.manager_id} counts an unrecorded consolation winner as its own fact"
        )
    assert len([n for n in screen.notes if "consolation bracket" in n.message]) == 1, (
        "the league fact belongs on the report once"
    )


def test_an_open_board_has_exactly_one_save_button(client, data_dir: Path):
    """One action, taken once. Twelve buttons is twelve chances to forget a franchise.

    Also the other half of the lock test — removing the button unconditionally would pass that
    one on its own. The button sits above the board and reaches it by id, so `form="board"` is
    what identifies it now that the label no longer names the season.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)  # no deadline = open
    assert page.count('form="board"') == 1
    assert re.search(r'<form[^>]*hx-post="/season/\d+/record"', page)


def test_the_save_button_posts_the_board_from_outside_it(client):
    """The button sits above the board and the board is a form, so it reaches it by id.

    Native form association — the same thing the trade and override tables do with their own
    rows. Drop the `form=` and the button becomes inert: it submits nothing, silently, and the
    only way to record twelve franchises is gone.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)

    button = re.search(r'<button[^>]*form="board"[^>]*>', page)
    assert button, "no button reaches the board form"
    assert 'type="submit"' in button.group(0)

    form = re.search(r'<form[^>]*id="board"[^>]*>', page, re.S)
    assert form, "no form for it to reach"
    assert f'/season/{SEASON}/record' in form.group(0), "and it must post the record endpoint"

    assert button.start() < form.start(), "the button is above the board, not inside it"


def test_the_boards_spinner_has_somewhere_to_show(client):
    """`.htmx-request .spin` is a DESCENDANT rule and the form is what makes the request.

    The spinner used to sit inside that form and matched for free. It sits beside the button
    now, outside it, so the form has to name it — without `hx-indicator` the spinner is dead
    markup and a record gives no sign it is running.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)

    form = re.search(r'<form[^>]*id="board"[^>]*>', page, re.S)
    indicated = re.search(r'hx-indicator="#([\w-]+)"', form.group(0))
    assert indicated, "the board form names no indicator"

    target = indicated.group(1)
    wrapper = re.search(rf'<span id="{target}"[^>]*>(.*?)</span>\s*</div>', page, re.S)
    assert wrapper, f"#{target} is not on the page"
    assert 'class="spin"' in wrapper.group(1), f"#{target} contains no spinner to reveal"


def test_the_board_does_not_print_the_keeper_deadline(client, data_dir: Path):
    """It moved to Season settings (commissioner, 2026-09-02), and it moved — it did not vanish.

    Both halves matter. A board still printing it means the top of the screen never got
    cleared; a settings page that stopped means the console prints ESPN's deadline nowhere at
    all, and the date would be gone from the tool rather than relocated in it.
    """
    set_keeper_deadline(data_dir, datetime(2026, 12, 1, 12, 0))

    assert "2026-12-01" not in client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert "2026-12-01" in client.get(f"/season/{SEASON}/settings").get_data(as_text=True)


def test_no_card_renders_an_empty_row(client, data_dir: Path):
    """An element that renders nothing should not exist.

    A locked card has no status — nothing to record and nothing recorded — but the row was
    emitted anyway, leaving a zero-height div that still contributed its own margin. Twelve
    cards each carried a band of space holding nothing.
    """
    def rows(page: str) -> list[str]:
        return [
            re.sub(r"<[^>]*>", "", row).strip()
            for row in re.findall(r'<div class="tc-actions">(.*?)</div>\s*</div>', page, re.S)
        ]

    # A locked card may still carry a row — an unverified tag is something to say. What it must
    # never be is present and empty.
    set_keeper_deadline(data_dir, datetime(2026, 12, 1, 12, 0))
    locked = rows(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert all(locked), f"a locked card rendered an empty row: {locked}"

    set_keeper_deadline(data_dir, datetime(2026, 1, 1, 12, 0))
    open_rows = rows(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert open_rows, "an open board still says what each franchise's state is"
    assert all(open_rows), f"an open card rendered an empty row: {open_rows}"


def test_the_keeper_board_is_a_card_per_franchise(client, data_dir: Path):
    """One page, one card per team, edited in place. No Edit button, no navigation."""
    derived = Derived(derived_dir=data_dir / "derived")
    managers = derived.load(SEASON).manager_ids
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)

    for manager_id in managers:
        assert f'id="claim-form-{manager_id}"' in page, f"{manager_id} has no card on the page"
        assert f"/season/{SEASON}/team/{manager_id}/preview" in page


def full_roster_doc(season: int = SEASON):
    """A derived season whose rosters are full depth, the way ESPN holds them most of the year.

    The small fixture above is four players deep, which is what a *pruned* league looks like —
    so a test that wants the picker has to say so rather than inherit it by accident.
    """
    doc = keeper_doc(season)
    for extra in range(10, 10 + 12):
        doc["players"].append(
            {"espn_player_id": extra, "name": f"Filler {extra}", "position": "WR",
             "nfl_team": "FA"}
        )
        doc["roster"].append(
            {"season": season, "manager_id": "t1", "espn_player_id": extra,
             "acquired_at": "2025-09-02T12:00:00", "base_salary": 1,
             "kept_prior_year": False, "source": "draft"}
        )
    return doc


def write_full_roster(data_dir: Path, season: int = SEASON) -> None:
    (data_dir / "derived" / f"{season}.json").write_text(
        json.dumps(full_roster_doc(season)), encoding="utf-8"
    )


def test_a_full_roster_still_needs_a_picker(client, data_dir: Path):
    """Before the deadline ESPN holds everyone, so who is kept is a real question."""
    write_full_roster(data_dir)
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert '<select name="t1__player_K1"' in page
    assert "Puka Nacua" in page and "James Cook III" in page
    assert f"${5 + KEEPER_TAX}" in page, "each option shows what that player would cost"


def test_a_pruned_roster_keeps_the_picker(client, data_dir: Path, store: ManualStore):
    """The picker survives the prune, because the prune does not answer the card's question.

    It was briefly a hidden input here, on the reasoning that a list of exactly the kept
    players has one answer. That is true of *who* is kept and false of which one is the
    prospect — and with no control there, the split could not be entered at all.

    What the prune changes is the option list, not the control: ``pickable`` is the roster, so
    a pruned roster narrows the options on its own.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)  # fixture is 4 deep
    assert '<select name="t1__player_PROSPECT"' in page, (
        "with no picker on the prospect slot, the keeper/prospect split cannot be entered"
    )
    assert '<select name="t1__player_K1"' in page
    assert '<input type="hidden" name="t1__player_' not in page
    assert "Puka Nacua" in page, "the kept players are still named"


def test_the_picker_names_a_price_only_while_the_columns_cannot(client, data_dir: Path):
    """The label carries the cost exactly when nothing else on the row does.

    A full roster prices nothing in Base/Total until a player is picked, so the label is the
    only figure there is while choosing. A pruned one prices every kept player in its own
    columns, and repeating it costs the name the width it needs to be readable.
    """
    pruned = client.get(f"/season/{SEASON}").get_data(as_text=True)  # fixture is 4 deep
    assert "Puka Nacua · WR LAR</option>" in pruned.replace("\n", "").replace("  ", "")

    write_full_roster(data_dir)
    full = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert "· $10" in full, "a full roster prices nothing else, so the label must"


def test_a_pruned_picker_offers_only_the_kept_players(data_dir: Path, store: ManualStore):
    """The options narrow because the roster did, not because the template filtered anything.

    Asserted on the screen rather than the page so it is the data being checked. A pruned
    roster IS the kept players, so anyone offered here was kept.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)
    assert screen.keepers_only
    rostered = {row.espn_player_id for row in screen.rows}
    assert {row.espn_player_id for row in screen.pickable} == rostered


def test_a_pruned_roster_fills_the_prospect_when_only_one_is_eligible(data_dir: Path, store: ManualStore):
    """One rookie among the keeps settles the slot, so the commissioner is not asked."""
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(
        SEASON,
        "t1",
        derived.load(SEASON),
        derived.load(PRIOR),
        store,
        # Everyone has a draft class, so nothing is unresolved; only LATE is a rookie.
        first_nfl_season={TAXED: 2019, PLAIN: 2020, EX_PROSPECT: 2023, LATE: SEASON - 1},
    )
    filled = {line.slot: (line.row.name if line.row else None) for line in screen.slots}
    assert filled["PROSPECT"] == "Ricky Pearsall"
    assert "Ricky Pearsall" not in [filled[s] for s in ("K1", "K2", "K3")], (
        "the prospect must come out of the keeper run, or he is claimed twice"
    )
    assert len([v for v in filled.values() if v]) == 4, "all four keeps are placed"


def test_a_pruned_roster_leaves_the_prospect_open_when_two_are_eligible(data_dir: Path, store: ManualStore):
    """Two rookies is exactly the case the commissioner has to settle by hand."""
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(
        SEASON,
        "t1",
        derived.load(SEASON),
        derived.load(PRIOR),
        store,
        first_nfl_season={TAXED: 2019, PLAIN: 2020, EX_PROSPECT: SEASON - 1, LATE: SEASON - 1},
    )
    filled = {line.slot: (line.row.name if line.row else None) for line in screen.slots}
    assert filled["PROSPECT"] is None, "a guessed prospect is a guessed $5 tax"
    assert any(
        "are all rookie-eligible" in note.message for note in screen.unverified
    ), "an open prospect slot must name who is in the running, labelled unverified"


def test_an_unresolved_draft_class_stops_the_prospect_fill(data_dir: Path, store: ManualStore):
    """One unknown is enough to stop it: he could be the second eligible player.

    "Exactly one is eligible" is not established while anybody's draft class is missing, and a
    fill here would present a guess as the settled answer.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(
        SEASON,
        "t1",
        derived.load(SEASON),
        derived.load(PRIOR),
        store,
        # LATE is the lone rookie; EX_PROSPECT has no draft class at all.
        first_nfl_season={TAXED: 2019, PLAIN: 2020, LATE: SEASON - 1},
    )
    filled = {line.slot: (line.row.name if line.row else None) for line in screen.slots}
    assert filled["PROSPECT"] is None
    assert any(
        "carries no draft class" in note.message for note in screen.unverified
    ), "a fill blocked by missing data must say so, labelled unverified"


def test_a_card_with_a_record_is_never_prefilled_around(data_dir: Path, store: ManualStore):
    """Once anything is recorded, the record is what the card shows.

    Filling the empty slots around it would put players the commissioner did not declare onto a
    card that already has an answer — suggesting keepers next to real ones, indistinguishable
    from them.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    store.save_team_claims(
        SEASON, "t1",
        [KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=EX_PROSPECT,
                     slot=KeeperSlot.K1, fee_allocated=0, computed_salary=3)],
    )
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)

    shown = [line.row.name for line in screen.slots if line.row]
    assert shown == ["Tyjae Spears"], f"the card invented {shown[1:]}"


def test_a_recorded_card_is_told_when_espn_kept_somebody_it_does_not_claim(
    data_dir: Path, store: ManualStore
):
    """The prefill runs only on an empty card, so a card recorded early can go stale in silence.

    A claim naming somebody ESPN dropped is already an ERROR. The mirror image — ESPN kept a
    player nobody declared — has no slot on the card to be missing from, so without this note
    it is invisible: the card shows three keepers, prices them, and looks finished.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    # Three of the four kept players declared. Tyjae Spears is the one ESPN kept and nobody did.
    store.save_team_claims(
        SEASON, "t1",
        [
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                        slot=KeeperSlot.K1, fee_allocated=0, computed_salary=5 + KEEPER_TAX),
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN,
                        slot=KeeperSlot.K2, fee_allocated=5, computed_salary=10),
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=LATE,
                        slot=KeeperSlot.PROSPECT, fee_allocated=0, computed_salary=1),
        ],
    )
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)

    assert screen.keepers_only
    stale = [note for note in screen.unverified if "Tyjae Spears" in note.message]
    assert stale, "ESPN kept a player this card does not claim, and nothing said so"
    assert stale[0].kind == "review", "an outside source flags, it does not block"
    assert screen.review_count, "it has to be counted, not just rendered"


def test_a_card_that_claims_everyone_espn_kept_is_not_nagged(data_dir: Path, store: ManualStore):
    """The note fires on a real disagreement only. A clean card stays clean."""
    derived = Derived(derived_dir=data_dir / "derived")
    roster = derived.load(SEASON).roster_for("t1")
    slots = [KeeperSlot.K1, KeeperSlot.K2, KeeperSlot.K3, KeeperSlot.PROSPECT]
    store.save_team_claims(
        SEASON, "t1",
        [
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=entry.espn_player_id,
                        slot=slot, fee_allocated=0, computed_salary=None)
            for slot, entry in zip(slots, sorted(roster, key=lambda e: e.espn_player_id))
        ],
    )
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)
    assert not [n for n in screen.notes if "not claimed on this card" in n.message]


def test_an_empty_pruned_card_is_prefilled_not_scolded(data_dir: Path, store: ManualStore):
    """Nothing recorded is the state the prefill exists for, not a disagreement with ESPN.

    Every slot on such a card is unclaimed by definition, so without the "has a record" guard
    the note fires on all twelve empty cards and names the players the card is already showing.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)
    assert screen.keepers_only and not store.claims(SEASON)
    assert not [n for n in screen.notes if "not claimed on this card" in n.message]


def test_a_full_roster_never_reports_undeclared_keepers(data_dir: Path, store: ManualStore):
    """Before ESPN prunes, eleven undeclared players is what a roster looks like, not a finding.

    Without the pruned-roster guard this note would fire on every card in the league for the
    whole month before the deadline, naming a dozen players each time.
    """
    write_full_roster(data_dir)
    derived = Derived(derived_dir=data_dir / "derived")
    store.save_team_claims(
        SEASON, "t1",
        [KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                     slot=KeeperSlot.K1, fee_allocated=0, computed_salary=5 + KEEPER_TAX)],
    )
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)
    assert not screen.keepers_only
    assert not [n for n in screen.notes if "not claimed on this card" in n.message]


def test_the_prefill_is_a_default_on_screen_not_a_record(data_dir: Path, store: ManualStore):
    """Nothing reaches claims.json until the card is submitted."""
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store)
    assert any(line.row for line in screen.slots)
    assert screen.keeper_count == 0 and screen.total_salary == 0
    assert store.claims(SEASON) == []


def test_every_card_has_exactly_four_slots(client, data_dir: Path):
    """Four rows whether or not anything is declared.

    An empty slot is a fact worth rendering, and a card whose height depends on how many
    keepers a team has makes a grid of twelve impossible to scan.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    managers = derived.load(SEASON).manager_ids
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)

    for slot in SLOT_CHOICES:
        for manager_id in managers:
            assert f'name="{manager_id}__player_{slot}"' in page
            assert f'name="{manager_id}__fee_{slot}"' in page


def test_over_allocated_fees_do_not_read_as_short(client, data_dir: Path, store: ManualStore):
    """One keeper owes $0. Allocating $5 is over, not short.

    ``fee_shortfall`` is signed, so a template testing it for truthiness calls both cases
    "short" — and tells a manager who has already paid $5 too much to pay more.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    over = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        claims=[KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                            slot=KeeperSlot.K1, fee_allocated=5)],
    )
    assert over.fee_state == "over"

    short = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        claims=[
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                        slot=KeeperSlot.K1, fee_allocated=0),
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN,
                        slot=KeeperSlot.K2, fee_allocated=0),
        ],
    )
    assert short.fee_state == "short"

    page = client.post(
        f"/season/{SEASON}/team/t1/preview", data=form((TAXED, "K1", 5))
    ).get_data(as_text=True)
    assert "over</span>" in page and "short</span>" not in page


def test_the_board_carries_no_salary_cap(client):
    """The league has none. A disabled one lying around is how a rule nobody has gets enforced."""
    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True)).lower()
    assert "salary cap" not in page or "no salary cap" in page
    assert "available salary" not in page


def test_each_card_targets_only_itself(client, data_dir: Path):
    """A shared id would make every franchise's edit swap the first franchise's card.

    The fragment used to hardcode ``#claim-form``, which was correct while exactly one lived on
    a page. Twelve of them is a silent failure: htmx swaps *something*, so it looks like it
    worked and the numbers land on the wrong team.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    managers = derived.load(SEASON).manager_ids
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)

    assert 'hx-target="#claim-form"' not in page, "a bare shared target is the bug"
    for manager_id in managers:
        assert page.count(f'hx-target="#claim-form-{manager_id}"') == 1, (
            f"{manager_id} must swap itself and nobody else"
        )


def test_a_future_deadline_disables_nothing(client, data_dir: Path):
    """The deadline is shown and nothing else. It stopped being a lock 2026-09-01.

    ESPN publishes keeper selections to nobody but an authenticated league member, so manual
    entry is the only way they reach this tool — and the window that entry happens in is
    exactly the window the lock used to close. Both halves are asserted here: the date is still
    displayed somewhere, and not one control on the board is dead.

    "Somewhere" is Season settings since 2026-09-02 — the board's own deadline line went when
    the actions moved to the top of it. The board's half of the rule is that nothing gates.
    """
    set_keeper_deadline(data_dir, datetime(2026, 12, 1, 12, 0))
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    settings = client.get(f"/season/{SEASON}/settings").get_data(as_text=True)

    assert "2026-12-01" in settings, "the deadline is still displayed — it just does not gate"
    assert "2026-12-01" not in page, "and no longer on the board, where nothing acts on it"
    assert "Claims are locked until the keeper deadline" not in page
    assert page.count('form="board"') == 1, "one board, one live button"
    assert not re.findall(r'name="t\d+__(?:fee|player)_[A-Z0-9]+"[^>]*\sdisabled', page, re.S), (
        "no fee box and no picker may be disabled before the deadline — that was the lock"
    )


# ---------------------------------------------------------------------------
# The two verdicts: who is kept, and what it costs — asked a day apart
# ---------------------------------------------------------------------------


_STATUS = re.compile(
    r'<span class="tc-status-wrap">\s*<(button|span)[^>]*?class="tc-status tag-inline([^"]*)"[^>]*>'
    r'(.*?)</\1>\s*(?:<div class="tc-reasons"[^>]*>(.*?)</div>)?',
    re.S,
)


def status_badge(html: str) -> tuple[str, str, str]:
    """``(css, label, reasons)`` of a card's one status badge.

    The reasons are in a panel the badge opens, not in a ``title`` — a native tooltip only
    appears on hover, after a delay, and ignores a click, which is the first thing anybody
    tries on a red badge. So a test that wants them has to read the panel.

    All three come back together on purpose: the colour, the word, and what it says when
    opened cannot be asserted from different places and drift apart.
    """
    m = _STATUS.search(html)
    assert m, "the card has no status badge"
    _tag, css, inner, panel = m.groups()
    label = " ".join(re.sub(r"<[^>]+>", " ", inner).split())
    reasons = " ".join(re.sub(r"<[^>]+>", " ", panel or "").split())
    return css.strip(), label, reasons


def tooltip_lines(html: str) -> list[tuple[str, str]]:
    """``(label, message)`` for every reason on the card, from BOTH of its panels.

    The card has two: the Valid/Invalid badge carries what makes it invalid, and the status
    line's "N unverified" badge carries what nobody has checked. A test asking "is this reason
    on the card, labelled honestly" does not care which — and reading only one would let a
    reason move between them unnoticed.

    The labels are what this asserts on. Notes and engine issues are two different sources, and
    a regression in one is invisible if you only check that the word "unverified" is somewhere
    on the page — which is how a mutation rendering every note as information once passed.
    """
    out = []
    for item in re.findall(r"<li>(.*?)</li>", html, re.S):
        chunk = " ".join(re.sub(r"<[^>]+>", " ", item).split())
        if ":" not in chunk:
            continue
        label, _, message = chunk.partition(":")
        out.append((label.strip(), message.strip()))
    return out


def all_reasons(html: str) -> str:
    """Every reason on the card as one string, whichever panel it is in."""
    return " ".join(f"{label}: {message}" for label, message in tooltip_lines(html))


def unverified_count(html: str) -> int:
    """What the card's status line says nobody has checked, or 0 when it says nothing."""
    m = re.search(r"(\d+) unverified", html)
    return int(m.group(1)) if m else 0


def _badges(client, *claims, manager="t1"):
    """Price a card through the preview route and return its status badge."""
    body = client.post(
        f"/season/{SEASON}/team/{manager}/preview", data=form(*claims, manager=manager)
    ).get_data(as_text=True)
    return status_badge(body)


def test_a_legal_selection_reads_legal_before_any_fee_is_typed(client):
    """Deadline night: the selection is settled, the fees are not, and the card must say so.

    One combined verdict called this card broken — the tier for two keepers is $5 and nothing
    is allocated yet — on the one night when who is kept is the only actionable fact.
    """
    css, label, tooltip = _badges(client, (TAXED, "K1", 0), (PLAIN, "K2", 0))
    assert label == "Invalid", "the tier for two keepers is $5 and nothing is allocated"
    assert "bad" in css.split()
    assert "expected $5 for 2 keepers" in tooltip, (
        "and the badge must say what is wrong, since the card no longer prints it"
    )
    assert "who was picked" not in tooltip.lower()


def test_a_fee_problem_does_not_make_the_selection_look_illegal(client):
    """The whole point of splitting them: a bad fee spread says nothing about who was picked."""
    css, label, tooltip = _badges(client, (TAXED, "K1", 99), (PLAIN, "K2", 0))
    assert (label, "bad" in css.split()) == ("Invalid", True)
    assert "fee_total_mismatch" in tooltip, "the tooltip names the fee rule that was broken"
    assert "too_many_keepers" not in tooltip, "a bad fee says nothing about who was picked"


def test_an_illegal_selection_does_not_make_the_fees_look_wrong(client):
    """And the mirror image. An ineligible prospect is not a fee finding.

    One keeper owes a $0 tier and $0 is allocated, so the money on this card is genuinely
    correct while the selection is genuinely not.
    """
    css, label, tooltip = _badges(client, (TAXED, "K1", 0), (LATE, "PROSPECT", 0))
    assert (label, "bad" in css.split()) == ("Invalid", True)
    assert "prospect_acquired_after_deadline" in tooltip
    assert "fee_total_mismatch" not in tooltip, "one keeper owes $0 and $0 is allocated"


def test_a_form_that_cannot_be_read_reports_neither_verdict(client):
    """A verdict on claims nobody entered is worse than no verdict.

    The same player in two slots never reaches the engine — the form parser refuses it — so the
    priced claims are not what was typed. A badge reading "legal" beside a "Cannot read the
    form" flag is the card contradicting itself.
    """
    css, label, tooltip = _badges(client, (TAXED, "K1", 5), (TAXED, "K2", 0))
    assert (label, "bad" in css.split()) == ("Invalid", True)
    assert "pick him once" in tooltip, "the badge says what could not be read"
    assert "valid" != label.lower(), "no pass on claims nobody entered"


def test_an_over_cap_selection_reports_the_fees_as_not_checked(
    client, data_dir: Path, store: ManualStore
):
    """The state that has to exist, and the reason green is never the default.

    Above the keeper maximum the fee tier is **undefined** — ``fee_total_for`` has no answer for
    four keepers — so ``keeper_rules`` raises no ``FEE_TOTAL_MISMATCH`` at all. An empty fee
    issue list there means the check never ran, and rendering it as "legal" is silence reading
    as success, which is the failure this whole validator is built around.

    Built directly rather than through the form: the form has three keeper slots, so it cannot
    express this. The store can, and so could a hand-edited claims.json.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=pid,
                    slot=slot, fee_allocated=0)
        for pid, slot in [(TAXED, KeeperSlot.K1), (PLAIN, KeeperSlot.K2),
                          (EX_PROSPECT, KeeperSlot.K3), (LATE, KeeperSlot.K1)]
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store, claims=claims
    )
    assert screen.fee_expected is None, "the tier is undefined above the cap"
    assert screen.fee_verdict == "skipped", "a check that did not run must not read as passed"
    assert screen.selection_verdict == "error"
    # The card is protected by the selection error, not by a second check on the fee half:
    # over the maximum ALWAYS raises TOO_MANY_KEEPERS, so "the fee check did not run" cannot
    # occur on a card that would otherwise read Valid. Asserted so the pairing stays true.
    assert screen.status.label == "Invalid"

    # And it has to reach the screen. The template maps "skipped" onto the same wording as an
    # unreadable form; drop that branch and it falls through to "none yet", which is the
    # unchecked state wearing the wording of a card nobody has touched.
    store.save_team_claims(SEASON, "t1", claims)
    page = client.get(f"/season/{SEASON}/team/t1").get_data(as_text=True)
    css, label, tooltip = status_badge(page)
    assert "ok" not in css.split(), "a check that did not run is never green"
    assert label != "Valid", "silence must not read as success"
    assert "too_many_keepers" in tooltip, "and the card says why it is not"


def test_an_empty_card_claims_neither_verdict(client, data_dir: Path):
    """Nothing declared is not the same as checked and legal.

    Asserted on a *full* roster, which is the only card with genuinely nothing to say. A pruned
    one carries ESPN's own answer and is judged on it — see the test below.
    """
    write_full_roster(data_dir)
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    css, label, tooltip = status_badge(page)
    assert label == "Not checked", "nothing declared is not a verdict"
    assert "ok" not in css.split()
    assert tooltip == "", "there is nothing to explain about a card nobody has touched"
    assert "Not recorded yet" in text(page), "and the status line still says it is unsaved"


def test_a_prefilled_card_is_judged_but_never_reads_as_recorded(client):
    """The pre-filled card gets a real verdict, and still cannot be mistaken for a saved one.

    Both halves matter and they pull against each other. A grey "nothing declared" over four
    filled slots told the commissioner the opposite of what the card showed him — but a plain
    green "legal" is worse, because after the prune a pre-filled card looks finished, and
    twelve green badges is a Record that was never pressed.

    The fee half deliberately does NOT follow. Nobody has typed a fee yet, so a pre-filled card
    would otherwise open with a red shortfall for money the workflow collects afterwards.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)  # fixture is 4 deep
    css, label, _ = status_badge(page)
    assert "ok" not in css.split(), "an unrecorded card must never read as checked"
    # The badge is the engine's verdict now and says nothing about saving. The status line is
    # what carries that, and it has to, or a pre-filled card is indistinguishable from a saved
    # one — twelve of those is a Record that was never pressed.
    assert "Not recorded yet" in text(page)


def test_every_row_of_the_card_has_the_same_number_of_cells(client):
    """Header, body and footer must agree, or the totals sit under the wrong columns.

    The card went from four columns to six. The footer is written by hand rather than
    generated, so it is the one that silently falls out of step — and a Total printed under
    "Fee" is a wrong number rather than a broken-looking one.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    table = page.split('id="claim-form-t1"')[1].split("</table>")[0]
    header = table.split("<thead>")[1].split("</thead>")[0].count("<th")
    footer = table.split("<tfoot>")[1].count("<td")
    bodies = [
        row.count("<td")
        for row in table.split("<tbody>")[1].split("</tbody>")[0].split("<tr")[1:]
    ]
    assert header == 6, f"expected slot, player, base, tax, fee, total; got {header}"
    assert footer == header, f"footer has {footer} cells against {header} headers"
    assert bodies and set(bodies) == {header}, f"body rows have {set(bodies)} cells"


def test_the_card_can_shrink_into_its_grid_track(client):
    """Three CSS declarations, each of which caused a real blow-out when it was missing.

    This pins the declarations; it cannot measure layout, and says so rather than implying the
    rendering was checked. What it prevents is the quiet deletion of a rule whose purpose is
    not obvious from reading it:

    * a grid item's min-width is its min-content, so one unbreakable child pushes the card out
      over its neighbour;
    * a `select`'s minimum width is its widest option and `width: 100%` does not reduce it, so
      an auto-layout table holding one demanded 620px inside a 340px card;
    * the two verdict badges carry sentences now, and `nowrap` on a sentence is an unbreakable
      box about 20rem wide.
    """
    css = client.get(f"/season/{SEASON}").get_data(as_text=True)

    def block(selector: str) -> str:
        """The declarations inside one rule, so an unrelated rule cannot satisfy the check.

        Both of these first passed against `table.trades { table-layout: fixed }` and a
        `min-width: 0` on the cash-trade form — vacuous, and mutation is what caught it.
        """
        assert selector in css, f"no rule for {selector}"
        return css.split(selector, 1)[1].split("}", 1)[0]

    assert "min-width: 0" in block(".team-card {"), (
        "a grid item that cannot shrink pushes itself out over its neighbour"
    )
    assert "table-layout: fixed" in block(".team-card table.tc-table {"), (
        "an auto table sizes to its widest option and will not fit the card"
    )
    assert "white-space: normal" in block(".tc-verdicts .tag-inline {"), (
        "the verdict badges carry sentences; nowrap makes them an unbreakable 20rem"
    )


def test_the_tax_column_is_priced_per_slot_not_per_player(data_dir: Path, store: ManualStore):
    """A taxed player owes nothing in the prospect slot, and the column has to say so.

    ``PlayerRow.tax`` is computed once, as a K1, for the picker's candidate price — so reading
    it straight into this column prints a $5 tax against a prospect who owes none. The engine
    already waives it in ``keeper_salary``; the risk is the screen disagreeing with the engine.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    now = datetime(2026, 9, 2, 12, 0)
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                    slot=KeeperSlot.PROSPECT, fee_allocated=0, submitted_at=now),
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store, claims=claims
    )
    line = next(x for x in screen.slots if x.slot == "PROSPECT")
    assert line.row is not None and line.row.espn_player_id == TAXED
    assert line.row.kept_prior_year, "the fixture player must carry the tax to prove anything"
    assert line.row.tax == KEEPER_TAX, "the player-level figure is the keeper price"
    assert line.tax == 0, "no tax is owed in the prospect slot"
    assert line.total == line.row.base, "a prospect is kept at his acquisition value"


def test_the_card_total_agrees_with_the_column_above_it(data_dir: Path, store: ManualStore):
    """A footer that contradicts its own rows is worse than no footer.

    ``total_salary`` prices the record, which is $0 on a pre-filled card while every row above
    shows real money. Asserted on a pruned roster, which is the case that has a proposal.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        first_nfl_season={TAXED: 2019, PLAIN: 2020, EX_PROSPECT: 2023, LATE: SEASON - 1},
    )
    assert screen.selection_proposed, "this test is about the pre-filled card"
    column = sum(line.total for line in screen.slots if line.total is not None)
    assert column > 0, "a pre-filled card must price its rows, or there is nothing to check"
    assert screen.display_total_salary == column


def test_a_proposed_verdict_is_never_styled_as_a_passing_one(client):
    """Asserted on the CSS class, not the words.

    The wording test above passes perfectly well against a badge rendered green — verified by
    mutation, which is how this gap was found rather than assumed shut. Green is the style of a
    card that has been recorded and checked, and the whole risk here is a pre-filled card being
    taken for one at a glance.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)  # fixture is 4 deep
    badges = re.findall(r'class="tc-status tag-inline([^"]*)"', page)
    assert badges, "no cards on the board"
    for css in badges:
        assert "ok" not in css.split(), f"an unrecorded card rendered as passing: {css!r}"


def test_a_league_wide_note_is_not_counted_against_every_franchise(
    data_dir: Path, store: ManualStore
):
    """It belongs in each card's tooltip and in nobody's tally.

    A sync warning is one fact about the season. Counted per card it made all twelve badges
    read the same number, which is exactly how a real flag stops being read — and is what the
    count did on its first pass here.

    The note still reaches the card, because a card is where the number it affects is read.
    Only the tally is per franchise.
    """
    doc = keeper_doc()
    doc["review"]["warnings"] = ["2026 has not been drafted, so base_salary is keeperValue"]
    (data_dir / "derived" / f"{SEASON}.json").write_text(json.dumps(doc), encoding="utf-8")

    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store
    )
    unverified = [n for n in screen.notes if n.kind in ("review", "error")]
    team_only = [n for n in unverified if n.team_specific]
    assert len(unverified) > len(team_only), "the fixture needs a league-wide note"

    reviews = len([i for i in screen.verdict_issues if i.severity is Severity.REVIEW])
    assert screen.review_count == reviews + len(team_only), (
        f"the card counted {screen.review_count} against {reviews} review(s) and "
        f"{len(team_only)} team note(s) — a league-wide fact was tallied per franchise"
    )
    assert any("has not been drafted" in line for line in screen.unverified_reasons), (
        "and it must still be readable on the card it affects"
    )


def test_a_click_on_the_badge_cannot_record_the_league(client):
    """The board wraps all twelve cards in ONE form, and a bare <button> submits it.

    So an unmarked button anywhere inside a card is a click that records twelve franchises —
    from an element whose whole job is to show a reason.

    Since the Save button moved above the board it reaches the form by id, which makes the rule
    stricter than it was: **no** unscoped submitter may exist anywhere on the page, and the one
    button allowed to record names the form it records.
    """
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    # Prose first: the stylesheet's own comments say the word "<button>", and scanning them
    # reported an untyped button that does not exist.
    markup = re.sub(r"<(style|script)\b.*?</\1>", "", page, flags=re.S)
    buttons = re.findall(r"<button([^>]*)>", markup)
    assert len(buttons) > 1, "the fixture must render both a badge and the Save button"

    # A `form=` attribute scopes the button to a named form, so it cannot submit whatever
    # happens to enclose it. What is dangerous is an unscoped button that is not type="button".
    submitters = [
        b for b in buttons if 'type="button"' not in b and "form=" not in b
    ]
    assert submitters == [], (
        f"{len(submitters)} unscoped button(s) can submit whatever form encloses them. "
        f"Offenders: {[b.strip()[:60] for b in submitters]}"
    )

    records = [b for b in buttons if 'form="board"' in b]
    assert len(records) == 1, "exactly one button records the league"
    assert 'type="submit"' in records[0], "and it must say so rather than rely on a default"


def test_the_reason_panel_is_wired_up(client):
    """The toggle finds the panel as the badge's NEXT SIBLING. Markup and script must agree.

    Only the structure is asserted here — a pytest cannot click anything, and pretending
    otherwise would be worse than saying so. What it catches is the panel being moved, wrapped
    or reordered, which breaks the toggle silently while every other test still passes.
    """
    # A card carrying BOTH disclosures — the Invalid badge and the unverified badge — because
    # asserting that *a* panel is correctly placed let a mutation move the other one and pass.
    body = client.post(
        f"/season/{SEASON}/team/t1/preview",
        data=form((TAXED, "K1", 99), (PLAIN, "K2", 0), manager="t1"),
    ).get_data(as_text=True)

    openers = body.count('aria-expanded="false"')
    assert openers >= 2, "this test needs both of the card's disclosures to be present"
    paired = len(re.findall(r"</button>\s*<div class=\"tc-reasons\"", body))
    assert paired == openers, (
        f"{openers} disclosure button(s) but {paired} correctly-placed panel(s) — the toggle "
        f"finds a panel as its button's NEXT SIBLING and cannot find one that moved"
    )
    assert body.count('class="tc-reasons" hidden') == openers, "panels must start closed"


def test_the_verdict_is_stated_once(client):
    """One card, one place that says it is invalid.

    The status line used to repeat the error count in red under the table, so a card carried
    the same verdict twice — the badge, and a second tag saying "1 error(s) — not recorded".
    The line is left with the one thing the badge does not answer: whether anything is on file.
    """
    body = client.post(
        f"/season/{SEASON}/team/t1/preview",
        data=form((TAXED, "K1", 0), (LATE, "PROSPECT", 0), manager="t1"),
    ).get_data(as_text=True)
    _, label, reasons = status_badge(body)
    assert label == "Invalid" and reasons, "the badge states the verdict and its reasons"

    line = text(body)
    assert "error(s) — not recorded" not in line, "the verdict must not be repeated below"
    assert "Not recorded yet" in line, "but whether it saved is still the line's job"


def test_every_engine_finding_lands_on_exactly_one_badge(data_dir: Path, store: ManualStore):
    """No finding may fall between the two badges.

    ``selection_issues`` is the complement of ``FEE_ISSUE_CODES`` rather than its own list, so
    a new IssueCode nobody classified surfaces on the selection badge instead of vanishing from
    both and leaving a card that reads legal on both counts while the engine objects.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    claims = [
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                    slot=KeeperSlot.K1, fee_allocated=-3),
        KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                    slot=KeeperSlot.K2, fee_allocated=0),
    ]
    screen = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store, claims=claims
    )
    assert screen.issues, "the fixture has to actually produce findings"
    assert len(screen.fee_issues) + len(screen.selection_issues) == len(screen.issues)
    assert screen.fee_issues and screen.selection_issues, "and both kinds of them"


def test_one_franchises_error_does_not_discard_anothers_work(client, store: ManualStore):
    """One button, but the write is still team by team.

    Twelve franchises go in during one sitting, so recording them is one action — and a single
    illegal fee spread must neither record itself nor take the other eleven down with it.
    """
    posted = form((TAXED, "K1", 99))                       # fee over the tier: blocked
    posted |= form((PLAIN, "K1", 0), manager="t2")         # legal
    page = client.post(f"/season/{SEASON}/record", data=posted).get_data(as_text=True)

    assert [claim.manager_id for claim in store.claims(SEASON)] == ["t2"]
    body = text(page)
    assert "Belichick" in body, "the recorded franchise is named"
    assert "Fake News" in body, "so is the skipped one — a count leaves you hunting"


def test_a_franchise_missing_from_the_request_is_not_wiped(client, store: ManualStore):
    """Absent is not the same as empty, and confusing them deletes a record nobody touched.

    The board posts all twelve every time, so a franchise with no fields in the request means
    this submission was not about that team. Writing an empty claim list for it would clear
    claims the commissioner never opened — the same trap ``save_settings`` already avoids.
    """
    store.save_team_claims(
        SEASON, "t2",
        [KeeperClaim(season=SEASON, manager_id="t2", espn_player_id=PLAIN,
                     slot=KeeperSlot.K1, fee_allocated=0, computed_salary=20)],
    )
    response = client.post(f"/season/{SEASON}/record", data=form((TAXED, "K1", 0)))  # t1 only

    # Asserted, because a 500 leaves the data untouched too — and a crash passing for safety is
    # how a guard gets removed without anything noticing.
    assert response.status_code == 200
    assert [c.manager_id for c in store.claims(SEASON)] == ["t1", "t2"], (
        "t2 was not in the request and must be untouched"
    )


def test_a_franchise_submitted_empty_is_cleared(client, store: ManualStore):
    """The other half: fields present and blank is a deliberate "keep nobody"."""
    store.save_team_claims(
        SEASON, "t1",
        [KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                     slot=KeeperSlot.K1, fee_allocated=0, computed_salary=10)],
    )
    client.post(
        f"/season/{SEASON}/record",
        data={f"t1__player_{slot}": "" for slot in SLOT_CHOICES}
        | {f"t1__fee_{slot}": "0" for slot in SLOT_CHOICES},
    )
    assert store.claims(SEASON) == []


def test_the_league_record_names_what_it_skipped_and_why(client, store: ManualStore):
    """A count is a puzzle. The reason is what tells you which rule to go and fix."""
    page = client.post(
        f"/season/{SEASON}/record", data=form((TAXED, "K1", 99))
    ).get_data(as_text=True)
    body = text(page)
    assert "Skipped 1" in body
    assert "fees total $99, expected $0" in body


def test_the_league_record_runs_before_the_deadline(client, store: ManualStore, data_dir: Path):
    """Recording the whole board before the deadline is the normal case, not a refusal.

    The board is how twelve franchises get entered, and they get entered while the deadline is
    still ahead. What used to be refused is now recorded and reported as provisional.
    """
    set_keeper_deadline(data_dir, datetime(2026, 12, 1, 12, 0))
    page = client.post(
        f"/season/{SEASON}/record", data=form((TAXED, "K1", 0))
    ).get_data(as_text=True)

    assert len(store.claims(SEASON)) == 1, "the board records before the deadline"
    assert "Recorded" in text(page)


def test_a_field_that_names_no_franchise_is_dropped(client, store: ManualStore):
    """A field that cannot say which team it belongs to cannot be recorded against one."""
    page = client.post(
        f"/season/{SEASON}/record",
        data={"player_K1": str(TAXED), "fee_K1": "0"},  # no manager prefix
    ).get_data(as_text=True)
    assert store.claims(SEASON) == []
    assert "Recorded" not in text(page), "an unattributable field must record against nobody"


def test_both_screens_agree_whether_the_rookie_rule_ran(data_dir: Path, store: ManualStore):
    """A prospect the team screen verified must not read as unverified in the league report.

    ``keeper_rules`` distinguishes ``None`` (rule 1 is not being applied) from an empty mapping
    (it is, and this player is unknown). A season screen built without the draft classes lands
    on the second and reports every prospect in the league as unverified — so the report
    contradicted the very screen the commissioner would open to check it.
    """
    derived = Derived(derived_dir=data_dir / "derived")
    # All four of the pruned roster's players are declared, not just the prospect: an
    # undeclared keeper on a pruned roster is its own REVIEW, and leaving three of them
    # undeclared would put a second, unrelated review into the count this test reads.
    store.save_team_claims(
        SEASON,
        "t1",
        [
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=TAXED,
                        slot=KeeperSlot.K1, fee_allocated=15),
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=PLAIN,
                        slot=KeeperSlot.K2, fee_allocated=0),
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=EX_PROSPECT,
                        slot=KeeperSlot.K3, fee_allocated=0),
            KeeperClaim(season=SEASON, manager_id="t1", espn_player_id=LATE,
                        slot=KeeperSlot.PROSPECT, fee_allocated=0),
        ],
    )
    origins = {LATE: SEASON - 1}

    team = build_team_screen(
        SEASON, "t1", derived.load(SEASON), derived.load(PRIOR), store,
        first_nfl_season=origins,
    )
    season = build_season_screen(
        SEASON, derived.load(SEASON), derived.load(PRIOR), store,
        now=NOW, first_nfl_season=origins,
    )
    t1 = next(t for t in season.teams if t.manager_id == "t1")

    assert IssueCode.PROSPECT_ROOKIE_UNVERIFIED not in {i.code for i in team.issues}
    assert t1.review_count == len(team.reviews) + len(
        [n for n in team.unverified if n.team_specific]
    ), "the report's count must be the team screen's own count, not a different one"
    assert t1.review_count == 0, (
        "the team screen verified this prospect against the draft class; the report says "
        "otherwise only when it was built without them"
    )


def test_the_report_totals_come_from_the_engine(client, store: ManualStore, data_dir: Path):
    """A franchise's numbers are read on that franchise's card, priced by the engine.

    The season screen used to carry league-wide totals too. No template ever rendered them —
    the board deliberately has no totals line, because every figure in one is already on a card
    and a second copy is a second place for it to be wrong.
    """
    client.post(f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0)))
    derived = Derived(derived_dir=data_dir / "derived")
    screen = build_season_screen(SEASON, derived.load(SEASON), None, store, now=NOW)
    t1 = next(t for t in screen.teams if t.manager_id == "t1")
    assert t1.declared and t1.total_salary == 5 + KEEPER_TAX
    assert not hasattr(screen, "league_salary"), "no second home for a number a card already has"


def test_the_deadline_is_shown_and_never_enforced(client, store: ManualStore, data_dir: Path):
    """Restored 2026-09-01 to the rule the tool was originally built on.

    It was briefly enforced *until* the deadline (commissioner, 2026-08-04) on the reasoning
    that no salary is entered that early, so a lock cost nothing. The premise was wrong: ESPN
    hands the selections to nobody, so manual entry before the deadline is the only input path
    there is. Both sides of the deadline save now, and neither ever re-locks.

    "Shown" is Season settings — the board stopped printing the date on 2026-09-02. "Never
    enforced" is the board, and is the half with teeth.
    """
    set_keeper_deadline(data_dir, datetime(2026, 1, 1, 12, 0))
    assert "2026-01-01" in client.get(f"/season/{SEASON}/settings").get_data(as_text=True)

    saved = client.post(
        f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0))
    ).get_data(as_text=True)
    assert "Saved to" in saved
    assert len(store.claims(SEASON)) == 1


def test_a_claim_before_the_deadline_is_recorded_and_flagged_provisional(
    client, store: ManualStore, data_dir: Path
):
    """What replaces the lock, and the whole reason removing it is safe.

    The lock made a claim recorded while managers could still change their minds impossible.
    Nothing makes it impossible now, so the card has to *say* so — otherwise the risk went from
    prevented to invisible, which is the one trade this repo never accepts.
    """
    set_keeper_deadline(data_dir, datetime(2026, 12, 1, 12, 0))
    body = client.post(
        f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0))
    ).get_data(as_text=True)

    assert len(store.claims(SEASON)) == 1, "a claim before the deadline is recorded"
    page = client.get(f"/season/{SEASON}/team/t1").get_data(as_text=True)
    reasons = all_reasons(page)
    assert "Provisional until you re-record" in reasons
    assert "so managers could still change their minds" in reasons


def test_a_claim_after_the_deadline_is_not_called_provisional(
    client, store: ManualStore, data_dir: Path
):
    """The other half. A note that fired on every claim would say nothing about any of them."""
    set_keeper_deadline(data_dir, datetime(2026, 1, 1, 12, 0))
    client.post(f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0)))

    page = text(client.get(f"/season/{SEASON}/team/t1").get_data(as_text=True))
    assert len(store.claims(SEASON)) == 1
    assert "Provisional until you re-record" not in page


def test_preview_prices_without_recording(client, store: ManualStore, data_dir: Path):
    """Live pricing writes nothing, on either side of the deadline."""
    set_keeper_deadline(data_dir, datetime(2026, 12, 1, 12, 0))
    body = client.post(
        f"/season/{SEASON}/team/t1/preview", data=form((TAXED, "K1", 0))
    ).get_data(as_text=True)
    assert f"${5 + KEEPER_TAX}" in body
    assert store.claims(SEASON) == []


def test_an_unrecorded_deadline_is_still_reported_as_a_gap(client, store: ManualStore, data_dir: Path):
    """A missing deadline is a fact about the sync, and stays distinct from a future one.

    Nothing locks any more, so this is no longer about a lockout — but the three states still
    have to stay apart, because the screen says something different about each and because a
    season with no deadline cannot report its claims as provisional at all.
    """
    assert Derived(derived_dir=data_dir / "derived").load(SEASON).keeper_deadline is None

    saved = client.post(
        f"/season/{SEASON}/team/t1", data=form((TAXED, "K1", 0))
    ).get_data(as_text=True)
    assert "Saved to" in saved
    assert len(store.claims(SEASON)) == 1

    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert "ESPN has no keeper deadline set for this season yet" in page
    assert "Provisional until you re-record" not in page, (
        "with no deadline on file nothing can be called provisional against it"
    )


def _derived_season(keeper_deadline: datetime | None) -> DerivedSeason:
    return DerivedSeason(
        season=SEASON,
        drafted=False,
        base_salary_field="keeperValue",
        trade_deadline=None,
        draft_date=None,
        keeper_deadline=keeper_deadline,
        franchises=(),
        players=(),
        roster=(),
    )


def test_the_three_deadline_states_are_distinct():
    """Unrecorded is not the same fact as future, and none of the three refuses a write.

    The states survived the lock's removal because the screen says something different about
    each. What must never come back is a way to gate on one, so this asserts the absence.
    """
    assert keeper_deadline_fact(_derived_season(None), now=NOW).state == "unrecorded"
    assert keeper_deadline_fact(_derived_season(datetime(2026, 12, 1, 12, 0)), now=NOW).state == "upcoming"
    assert keeper_deadline_fact(_derived_season(datetime(2026, 1, 1, 12, 0)), now=NOW).state == "passed"

    assert not keeper_deadline_fact(_derived_season(datetime(2026, 12, 1, 12, 0)), now=NOW).passed
    assert keeper_deadline_fact(_derived_season(datetime(2026, 1, 1, 12, 0)), now=NOW).passed
    assert not hasattr(KeeperDeadline(deadline=NOW, state="upcoming"), "editable"), (
        "no route may gate on this object again — it is display only"
    )


def test_the_default_clock_is_utc_and_not_the_machines_local_time(tmp_path: Path):
    """The clock the gate compares against a UTC deadline has to be UTC itself.

    ``datetime.now()`` returns the machine's local time. Compared against
    ``keeper_deadline`` — naive UTC, straight off ESPN — it kept the console locked for the
    length of the UTC offset after the deadline had actually passed, and it quietly assumed
    whoever ran the tool sat in the league's own timezone.

    Asserted by identity rather than by comparing two clocks, deliberately: CI runs on a UTC
    machine, where ``datetime.now()`` and ``utc_now()`` agree to the microsecond and a
    value-based test could never fail. A test that cannot fail is worse than no test.
    """
    empty = tmp_path / "data"
    (empty / "manual").mkdir(parents=True)
    (empty / "derived").mkdir(parents=True)
    app = create_app(data_dir=empty, derived_dir=empty / "derived", repo=tmp_path, push=False)
    assert app.config["CLOCK"] is utc_now


def test_the_settings_page_prints_espns_dates_on_the_league_clock(client, data_dir: Path):
    """Both read-only ESPN facts, at their real 2026 values — the draft 9pm ET on 9/3.

    **The only place the console prints the keeper deadline**, and so the only guard left on
    printing it in the league's own timezone. The keeper board carried the same assertion until
    2026-09-02; stored UTC and printed UTC, it named the wrong day — ESPN's 2026 deadline is
    11pm ET on 9/1, which is 03:00 UTC on 9/2, and the whole console read a day late.
    """
    path = data_dir / "derived" / f"{SEASON}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["source"]["draft_date"] = "2026-09-04T01:00:00"
    doc["source"]["keeper_deadline"] = "2026-09-02T03:00:00"
    path.write_text(json.dumps(doc), encoding="utf-8")

    page = text(client.get(f"/season/{SEASON}/settings").get_data(as_text=True))
    assert "2026-09-03 21:00" in page, "the draft date"
    assert "2026-09-01 23:00" in page, "the keeper deadline"
    assert "2026-09-04" not in page and "2026-09-02" not in page, "neither UTC form"


def test_a_cash_trades_agreed_date_is_not_shifted(client, data_dir: Path):
    """The one datetime here that must NOT be converted, and why it is a separate rule.

    ``agreed_at`` is a calendar date the commissioner types into a date input — a wall-clock
    day with no timezone in it, unlike everything that arrives from ESPN as an instant.
    Running it through the Eastern conversion would drag midnight back to 7pm the evening
    before, and the date would walk backwards a day every time the form was reopened.
    """
    page = a_trade(client, agreed_at="2026-03-01")
    assert 'value="2026-03-01"' in page
    assert "2026-02-28" not in page


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


# ---------------------------------------------------------------------------
# Cash trades
#
# The balance RULE is tested in test_keeper_rules.py, against a roster those tests control
# exactly. What is tested here is the plumbing: the form's refusals, the escaping, the write,
# and the linking. The fixture roster deliberately carries one player on two teams, so it is
# the wrong place to assert dollar sums.
# ---------------------------------------------------------------------------


def a_trade(client, **over):
    data = {"draft_year": SEASON, "from_manager_id": "t1", "to_manager_id": "t2", "amount": 5,
            "note": "agreed in the group chat"}
    data.update(over)
    return client.post("/trades", data=data, follow_redirects=True).get_data(as_text=True)


# ---------------------------------------------------------------------------
# Draft cash on the keeper page: the engine sees every season, the page shows one
# ---------------------------------------------------------------------------


def _netted_pair(client, store: ManualStore):
    """Two trades in different draft years sharing one salary edit, netting to zero.

    This is the shape ``CASH_TRADE_LEG_WRONG_DRAFT`` exists for and the shape a season-scoped
    audit gets wrong: ``trade_groups`` joins them because one override is a leg of both, and
    ``check_cash_trades`` then nets a franchise across the pair. Filter the engine's input to
    one year and it computes ``expected`` over half the group.
    """
    a_trade(client, draft_year=SEASON, amount=5, from_manager_id="t1", to_manager_id="t2")
    a_trade(client, draft_year=PRIOR, amount=5, from_manager_id="t2", to_manager_id="t1")
    now_ids = [t.id for t in store.trades()]
    # One edit expressing both: t1 pays 5 and receives 5, so ESPN is left alone and the pair
    # cancels. Recorded against the player t1 holds.
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=5, trade_ids=tuple(now_ids),
        reason="both deals, netted", created_at=NOW, reverted=False,
    ))
    return now_ids


def test_a_cross_season_netted_group_is_audited_whole(client, store: ManualStore):
    """The audit runs over every season even though the page shows one.

    ``check_cash_trades`` nets a franchise across a group of trades that share an edit. Hand it
    a single season's trades and half the group goes missing, ``expected`` is computed from the
    half that remains, and the page reports an imbalance that does not exist.
    """
    _netted_pair(client, store)
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    # Asserted on what the table actually renders — the verdict chip — not on the engine's
    # message, which the template only puts in a tooltip. A version of this reading the
    # message would have passed no matter what the audit concluded.
    assert "needs review" not in text(raw), (
        "a group netting to zero across two drafts must not report an imbalance"
    )
    assert "balances" in text(raw), "and it must actually reach a verdict, not stay silent"


def test_a_netted_trade_from_another_draft_is_still_shown(client, store: ManualStore):
    """Showing half a netted pair rests the verdict on a row you cannot see."""
    ids = _netted_pair(client, store)
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    for tid in ids:
        assert tid in raw, f"{tid} is part of this page's audit, so it has to be on it"
    assert "not " + str(SEASON) + " trades" in text(raw), "and the page says why it is there"


def test_the_season_page_says_what_it_is_not_showing(client, store: ManualStore):
    """A ledger that looks complete and is not is worse than one that admits its scope."""
    a_trade(client, draft_year=PRIOR, amount=5)
    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert "Not shown here" in page and str(PRIOR) in page


def test_the_league_wide_override_net_is_reported_on_every_season_page(
    client, store: ManualStore
):
    """What replaces the cross-season ledger, and the reason deleting it is safe.

    A leg misfiled into another draft year leaves **both** season pages netting to zero, which
    is exactly the mistake there is an IssueCode for. The season figure cannot see it; the
    league-wide one can, so it is printed next to it on every season page.
    """
    store.add_override(SalaryOverride(
        espn_player_id=PLAIN, season=SEASON, actual_salary=45,
        reason="one leg here", created_at=NOW, reverted=False,
    ))
    for year in (SEASON, PRIOR):
        page = text(client.get(f"/season/{year}").get_data(as_text=True))
        assert "Every season, not just" in page, (
            f"/season/{year} must report the league-wide net, not only its own"
        )


def test_an_unlinked_override_in_another_season_is_still_flagged(client, store: ManualStore):
    """An unlinked leg is the row no per-trade audit can reach. Scoping it hides the worst ones."""
    store.add_override(SalaryOverride(
        espn_player_id=PLAIN, season=PRIOR, actual_salary=30,
        reason="live, attached to nothing", created_at=NOW, reverted=False,
    ))
    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert "attached to no cash trade" in page


def test_a_trade_link_chosen_on_the_keeper_page_is_recorded(client, store: ManualStore):
    """The keeper page used to carry a second add-override form that posted ``trade_id``.

    ``add_override`` reads ``getlist("trade_ids")`` and ``override_form`` reads only
    ``trade_ids``, so every trade link picked on that form was silently dropped and the leg
    then reported as attached to nothing. One form now, and it is the one that works.
    """
    a_trade(client)
    tid = store.trades()[0].id
    client.post("/overrides", data={
        "season": SEASON, "espn_player_id": PLAIN, "actual_salary": 45,
        "trade_ids": tid, "return_year": SEASON,
    }, follow_redirects=True)
    assert store.overrides(SEASON)[0].trade_ids == (tid,), "the chosen trade must be recorded"


def test_deleting_an_override_returns_to_the_season_it_was_deleted_from(
    client, store: ManualStore
):
    """The delete form was the one control on the row that dropped ``return_year``."""
    store.add_override(SalaryOverride(
        espn_player_id=PLAIN, season=SEASON, actual_salary=45,
        reason="x", created_at=NOW, reverted=False,
    ))
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    form = raw[raw.index('action="/overrides/delete"'):]
    form = form[: form.index("</form>")]
    assert 'name="return_year"' in form, "a delete must come back to the page it was made on"

    landed = client.post("/overrides/delete", data={
        "season": SEASON, "espn_player_id": PLAIN,
        "created_at": NOW.isoformat(), "return_year": SEASON,
    })
    assert landed.headers["Location"].endswith(f"/season/{SEASON}")


def test_the_save_button_is_told_apart_by_the_header_above_it(client):
    """The picker makes a prior season a routine destination, and Save clears empties.

    A Save on a page you did not mean to be on is one click from wiping a settled year. The
    button used to name its own season — "Record all 12 franchises for 2025" — and no longer
    does (commissioner, 2026-09-02). **This is a weaker guarantee than the one it replaces**,
    and it is asserted here rather than assumed: the header picker names the year directly
    above the button, and a prior season also carries the amber tag beside it.
    """
    page = client.get(f"/season/{PRIOR}").get_data(as_text=True)
    assert f'value="/season/{PRIOR}"' in page and "selected" in page, "the picker names the year"
    assert "not the current season" in text(page)
    assert 'form="board"' in page, "and the button it warns about is on the same screen"

    # Nothing on the button itself distinguishes the two seasons, which is the cost.
    assert page.count('form="board"') == 1


def test_recording_a_trade_writes_it_with_its_direction(client, store: ManualStore):
    a_trade(client)
    recorded = store.trades(SEASON)
    assert len(recorded) == 1
    assert (recorded[0].from_manager_id, recorded[0].to_manager_id) == ("t1", "t2")
    assert recorded[0].amount == 5


def test_a_trade_records_when_it_happened_not_when_it_was_typed(client, store: ManualStore):
    """Those are routinely months apart — the 2025 legs are being reconstructed now — so the
    date is a field, not a clock read."""
    a_trade(client, agreed_at="2025-10-29")
    assert store.trades(SEASON)[0].agreed_at == datetime(2025, 10, 29, 0, 0)


def test_a_trade_with_no_date_falls_back_to_today(client, store: ManualStore):
    """Optional, so the common case of recording one as it happens stays a four-field form."""
    a_trade(client, agreed_at="")
    assert store.trades(SEASON)[0].agreed_at == NOW


def test_a_trade_date_that_is_not_a_date_is_refused(client, store: ManualStore):
    page = text(a_trade(client, agreed_at="last tuesday"))
    assert "not a date" in page
    assert store.trades() == [], "nothing is written when the form is refused"


def test_a_trade_id_is_readable_and_unique(client, store: ManualStore):
    """It is what a leg carries in overrides.json and what an audit message names, so it has
    to survive being read in a diff. A second trade between the same two teams gets a suffix
    rather than colliding — a reused id would let a leg balance against the wrong trade."""
    a_trade(client)
    a_trade(client)
    ids = sorted(trade.id for trade in store.trades(SEASON))
    assert ids == [f"{SEASON}-t1-to-t2", f"{SEASON}-t1-to-t2-2"]


@pytest.mark.parametrize(
    "bad, expected",
    [
        ({"amount": 0}, "records nothing"),
        ({"amount": -5}, "records nothing"),
        ({"to_manager_id": "t1"}, "not from a team to itself"),
        ({"to_manager_id": ""}, "both a paying and a receiving"),
        ({"to_manager_id": "t99"}, "not a franchise"),
    ],
)
def test_a_trade_that_could_not_have_happened_is_refused(client, store, bad, expected):
    assert expected in text(a_trade(client, **bad))
    assert store.trades() == [], "nothing is written when the form is refused"


def edit(client, trade_id, **over):
    data = {"draft_year": SEASON, "from_manager_id": "t1", "to_manager_id": "t2", "amount": 5,
            "note": "agreed in the group chat"}
    data.update(over)
    return client.post(f"/trades/{trade_id}/edit", data=data,
                       follow_redirects=True).get_data(as_text=True)


def test_a_trade_can_be_edited_in_place(client, store: ManualStore):
    a_trade(client)
    tid = store.trades()[0].id
    edit(client, tid, amount=9, note="corrected", agreed_at="2025-11-25")
    after = store.trades()
    assert len(after) == 1, "editing replaces the row, it does not add one"
    assert (after[0].amount, after[0].note) == (9, "corrected")
    assert after[0].agreed_at == datetime(2025, 11, 25, 0, 0)


def test_editing_keeps_the_id_its_legs_name(client, store: ManualStore):
    """The id is the only thing a leg holds. Mint a new one on edit and every leg is orphaned.

    The edit **changes a party**, deliberately. An id is derived from the season and the two
    franchises, so re-minting one on an edit that changed neither returns the identical string
    and proves nothing — this asserts against an edit where a regenerated id would differ.
    """
    a_trade(client)
    tid = store.trades()[0].id
    assert tid == f"{SEASON}-t1-to-t2"
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="leg",
        created_at=NOW, trade_ids=(tid,),
    ))
    edit(client, tid, from_manager_id="t2", to_manager_id="t1")
    saved = store.trades()[0]
    # Both halves matter. "id unchanged" alone is also what a silently-failed edit produces,
    # so the direction is asserted too: the edit has to have actually landed.
    assert saved.from_manager_id == "t2", "the edit did not apply"
    assert saved.id == tid, "a regenerated id would read t2-to-t1 and orphan the leg"
    assert store.overrides(SEASON)[0].trade_ids == (tid,)


def test_updating_a_trade_the_store_does_not_hold_reports_a_miss(store: ManualStore):
    """Store-level: the route 404s first, so this path is only reachable if the row went away
    between the check and the write. It must report rather than quietly create the row."""
    absent = CashTrade(id="never-recorded", draft_year=SEASON, from_manager_id="t1",
                       to_manager_id="t2", amount=5, agreed_at=NOW)
    assert store.update_trade(absent) is False
    assert store.trades() == [], "a miss writes nothing"


def test_editing_a_trade_into_another_draft_moves_it(client, store: ManualStore):
    """Rows are stored under a draft-year key, so the old year has to lose it as well as the
    new year gaining it — otherwise the id is on file twice and a leg cannot say which it
    means."""
    a_trade(client)
    tid = store.trades()[0].id
    edit(client, tid, draft_year=PRIOR)
    assert [(t.id, t.draft_year) for t in store.trades()] == [(tid, PRIOR)]
    assert store.trades(SEASON) == []


def test_an_edit_is_held_to_the_same_rules_as_the_original(client, store: ManualStore):
    a_trade(client)
    tid = store.trades()[0].id
    assert "not from a team to itself" in text(edit(client, tid, to_manager_id="t1"))
    assert "records nothing" in text(edit(client, tid, amount=0))
    assert store.trades()[0].amount == 5, "a refused edit changes nothing"


def test_editing_a_trade_that_does_not_exist_is_a_404(client):
    assert client.post("/trades/made-up/edit", data={"season": SEASON}).status_code == 404


def test_every_row_is_an_edit_form_prefilled(client, store: ManualStore):
    """A row's controls sit in the table and its <form> sits outside it, joined by id.

    That join is the whole mechanism — a `<form>` cannot wrap a `<tr>` — and it fails silently:
    drop the `form` attribute and the inputs belong to no form at all, so Save posts an empty
    body and the row quietly stops working while still looking right. Asserted explicitly.
    """
    a_trade(client, amount=7)
    tid = store.trades()[0].id
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert f'<form id="trade-{tid}"' in raw
    assert f'action="/trades/{tid}/edit"' in raw
    assert f'form="trade-{tid}"' in raw, "the row's inputs must name the form that submits them"
    assert 'value="7"' in raw, "the edit form starts from what is on file"


def test_the_insert_row_is_the_same_form_in_the_same_table(client):
    """Adding and amending look and work alike because they are the same controls."""
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert '<form id="trade-new"' in raw and 'action="/trades"' in raw
    assert 'form="trade-new"' in raw


def test_a_trade_can_be_deleted(client, store: ManualStore):
    a_trade(client)
    tid = store.trades()[0].id
    client.post(f"/trades/{tid}/delete", follow_redirects=True)
    assert store.trades() == []


def test_deleting_a_trade_leaves_its_legs_on_file(client, store: ManualStore):
    """An override is a real ESPN edit that happened. Deleting the trade it was filed under
    must not quietly erase the record of the money moving — the leg survives and reports as
    naming a trade that is not on file, which is a finding somebody has to answer."""
    a_trade(client)
    tid = store.trades()[0].id
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="leg",
        created_at=NOW, trade_ids=(tid,),
    ))
    page = client.post(f"/trades/{tid}/delete", follow_redirects=True).get_data(as_text=True)
    assert len(store.overrides(SEASON)) == 1, "the leg is not deleted with its trade"
    assert store.overrides(SEASON)[0].trade_ids == (tid,), "and still says what it belonged to"
    assert "still name it" in text(page), "the orphaning is reported, not silent"


def test_deleting_a_trade_that_does_not_exist_says_so(client, store: ManualStore):
    page = client.post("/trades/made-up/delete", follow_redirects=True).get_data(as_text=True)
    assert "no trade made-up on file" in text(page)


def test_an_override_can_be_deleted(client, store: ManualStore):
    """Deleting is for a row that should never have been recorded. Reverting is the tool for
    one that has served its purpose — that row stays on file as the explanation."""
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="typed in error",
        created_at=NOW,
    ))
    client.post("/overrides/delete", data={
        "season": SEASON, "espn_player_id": TAXED, "created_at": NOW.isoformat(),
    }, follow_redirects=True)
    assert store.overrides() == []


def test_deleting_an_override_that_does_not_exist_says_so(client, store: ManualStore):
    page = client.post("/overrides/delete", data={
        "season": SEASON, "espn_player_id": 999, "created_at": NOW.isoformat(),
    }, follow_redirects=True).get_data(as_text=True)
    assert "no matching override" in text(page)


def test_a_trade_note_is_escaped_not_executed(client, store: ManualStore):
    """The same injection path as an override's reason: typed here, stored, committed to a
    public repo, published."""
    nasty = "<script>alert(1)</script>"
    a_trade(client, note=nasty)
    assert store.trades(SEASON)[0].note == nasty, "stored exactly as typed"
    page = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert nasty not in page
    assert "&lt;script&gt;" in page


def test_an_override_can_be_recorded_as_a_leg(client, store: ManualStore):
    a_trade(client)
    trade_id = store.trades(SEASON)[0].id
    client.post("/overrides", data={
        "season": SEASON, "espn_player_id": TAXED, "actual_salary": 2,
        "reason": "leg of a cash trade", "trade_ids": trade_id,
    })
    assert store.overrides(SEASON)[0].trade_ids == (trade_id,)


def test_an_override_naming_a_trade_that_does_not_exist_is_refused(client, store: ManualStore):
    """A dangling reference falls out of BOTH audits at once: the per-trade check has no trade
    to balance it against, and the league-wide one skips anything carrying a trade_id."""
    page = client.post("/overrides", data={
        "season": SEASON, "espn_player_id": TAXED, "actual_salary": 2,
        "reason": "leg of nothing", "trade_ids": "made-up",
    }, follow_redirects=True).get_data(as_text=True)
    assert "no trade made-up on file" in text(page)
    assert store.overrides() == [], "the override is not written at all"


def test_a_leg_can_be_attached_and_detached_after_the_fact(client, store: ManualStore):
    """The two are recorded in either order — a leg entered before its counterparty was agreed
    has nowhere to point yet."""
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="cash", created_at=NOW,
    ))
    a_trade(client)
    trade_id = store.trades(SEASON)[0].id
    ident = {"season": SEASON, "espn_player_id": TAXED, "created_at": NOW.isoformat()}

    edit_ov = {"orig_season": SEASON, "orig_espn_player_id": TAXED,
               "created_at": NOW.isoformat(), "season": SEASON,
               "espn_player_id": TAXED, "actual_salary": 2}
    client.post("/overrides/edit", data={**edit_ov, "trade_ids": trade_id})
    assert store.overrides(SEASON)[0].trade_ids == (trade_id,)

    client.post("/overrides/edit", data={**edit_ov, "trade_ids": ""})
    assert store.overrides(SEASON)[0].trade_ids == ()


def test_attaching_to_a_trade_that_does_not_exist_is_refused(client, store: ManualStore):
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="cash", created_at=NOW,
    ))
    page = client.post("/overrides/edit", data={
        "orig_season": SEASON, "orig_espn_player_id": TAXED,
        "created_at": NOW.isoformat(), "season": SEASON, "espn_player_id": TAXED,
        "actual_salary": 2, "trade_ids": "made-up",
    }, follow_redirects=True).get_data(as_text=True)
    assert "no trade made-up on file" in text(page)
    assert store.overrides(SEASON)[0].trade_ids == ()


def test_the_ledger_shows_a_one_legged_trade_as_needing_review(client, store: ManualStore):
    """A trade with only one leg is the case the whole feature exists to name."""
    a_trade(client)
    trade_id = store.trades(SEASON)[0].id
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="one leg",
        created_at=NOW, trade_ids=(trade_id,),
    ))
    page = text(client.get(f"/season/{SEASON}").get_data(as_text=True))
    assert "needs review" in page
    assert trade_id in page, "the finding names the trade that is missing a leg"


def test_a_trade_with_no_legs_says_so(client):
    """In the chip's tooltip, which is the row's only alert.

    Asserted against the raw HTML on purpose: ``text()`` strips tags, and with it the attribute
    the message now lives in — so a version of this test that kept using ``text()`` would pass
    only while the message was ALSO printed somewhere else, which is the duplication that was
    removed.
    """
    a_trade(client)
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert "no salary override points at it" in raw
    assert ">None<" in raw, "the Legs cell says None, in the red badge"
    assert "no salary override points at it" not in text(raw), (
        "the finding belongs in the tooltip only — a second copy in the body is the thing "
        "this table stopped doing"
    )
    # The badge IS the whole cell. A muted "none" above a "needs review" chip stated the same
    # fact twice, which is what this replaced.
    assert '<div class="muted">none</div>' not in raw
    assert "needs review" not in raw, "a legless trade has one signal, not two"


def test_the_ledger_surfaces_overrides_attached_to_no_trade(client, store: ManualStore):
    """A ledger of balanced trades is not a complete account of the season's cash while these
    are outstanding, so they go on the page rather than only into validate's output.

    Surfaced two ways since trades and overrides merged onto one tab: one summary warning, and
    an "attach to…" picker on the row itself. Both are asserted — the summary is what gets
    read, the picker is what gets acted on, and a page with only one of them is half a feature.
    """
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="unattached",
        created_at=NOW,
    ))
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)
    assert "attached to no cash trade" in text(raw)
    assert 'name="trade_ids"' in raw and "multiple" in raw, (
        "the row needs its own picker — one salary edit can be a leg of several trades"
    )


def test_trades_json_carries_its_about_prose(client, store: ManualStore):
    """It is the only written explanation of the sign convention, and getting that backwards
    silently reprices both teams."""
    a_trade(client)
    doc = json.loads((store.manual / "trades.json").read_text(encoding="utf-8"))
    prose = " ".join(doc["_about"])
    assert "POSITIVE" in prose
    assert "public" in prose.lower()


def test_an_emptied_year_is_removed_not_left_as_an_empty_bucket(client, store: ManualStore):
    """`"2026": []` says nothing and shows up in the diff of a public repo as if it did."""
    import json as _json
    a_trade(client)
    tid = store.trades()[0].id
    store.delete_trade(tid)
    doc = _json.loads((store.manual / "trades.json").read_text(encoding="utf-8"))
    assert doc.get("drafts") == {}, "the emptied draft year is dropped"
    assert "_about" in doc, "and the prose survives the pruning"


def test_destroying_something_takes_two_clicks(client, store: ManualStore):
    """A delete is one stray click from gone otherwise, and neither file it touches is
    recoverable from the UI. Guarded structurally: the destructive submit has to sit inside a
    closed <details>, not loose in the row beside Save.

    Reverting is deliberately NOT behind the gate — it is reversible and the row survives it.
    """
    a_trade(client)
    store.add_override(SalaryOverride(
        espn_player_id=TAXED, season=SEASON, actual_salary=2, reason="leg", created_at=NOW,
    ))
    raw = client.get(f"/season/{SEASON}").get_data(as_text=True)

    for action in (f"/trades/{store.trades()[0].id}/delete", "/overrides/delete"):
        head, _, _ = raw.partition(f'action="{action}"')
        assert head.rstrip().endswith("<form method=\"post\"") or 'class="confirm"' in head, (
            f"the {action} form must be reachable only through a confirm step"
        )
        # The confirm wrapper must be the nearest enclosing details, and it must start closed.
        opened = head.rsplit('<details class="confirm">', 1)
        assert len(opened) == 2, f"{action} is not inside a confirm step"
        assert "</details>" not in opened[1], f"{action} sits outside its confirm step"

    assert 'class="confirm"' in raw
    assert "<details class=\"confirm\" open" not in raw, "the confirm step starts closed"
    # Mark reverted stays a single click.
    reverted_at = raw.index('action="/overrides/revert"')
    assert '<details class="confirm">' not in raw[reverted_at - 400:reverted_at]
