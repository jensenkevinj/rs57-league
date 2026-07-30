"""The local admin tool: Flask + HTMX on localhost, writing ``data/manual/``.

    python -m rs57.admin              # http://127.0.0.1:5057
    python -m rs57.admin --no-push    # commit button commits, never pushes

What it is for: recording keeper claims with their slots and fee allocations, salary overrides
with reasons, payout tracking, and per-season settings — then committing that to the repo, which
is what publishes it. Done when an offseason runs without opening a spreadsheet.

Direction of dependency
-----------------------

``keeper_rules`` and ``stats`` are pure and stay that way. This package imports them; neither
learns that Flask exists. Every salary, fee tier and tax on every screen comes out of
``compute_team_keepers`` — there is no second implementation in a view, a template, or a line of
JavaScript, and a test greps the templates for arithmetic on money.

Ownership
---------

**This tool is the only writer of ``data/manual/``.** It never writes ``data/derived/`` or
``site/`` (the nightly Action's) or ``data/history/`` (frozen). Every write goes through
``store.ManualStore.write``, which raises ``OwnershipError`` on any path outside ``data/manual/``,
and the commit button re-checks the git index after staging.

Escaping
--------

Autoescaping is on. ``SalaryOverride.reason`` is free text a human types into this tool, stored
in ``data/manual/``, committed to a public repo and rendered on a public site — it is the
injection path. No template uses the ``safe`` filter.

htmx is vendored in ``static/`` rather than loaded from a CDN: the tool runs on localhost and
should not need the network to render a page.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from rs57.admin.derived import DERIVED, Derived
from rs57.admin.gitops import Git, GitError, commit_message
from rs57.admin.reconcile import fetch_deadlines, fetch_keeper_picks, reconcile
from rs57.admin.screens import (
    FEE_HELP,
    LIMITS_HELP,
    SLOT_CHOICES,
    build_season_screen,
    build_team_screen,
    claims_from_form,
    override_form,
)
from rs57.admin.store import DATA, ManualStore, OwnershipError
from rs57.keeper_rules import KEEPER_TAX, MAX_KEEPERS, MAX_PROSPECTS
from rs57.models import Season

ROOT = Path(__file__).resolve().parent.parent.parent


def create_app(
    *,
    data_dir: Path = DATA,
    derived_dir: Path = DERIVED,
    repo: Path = ROOT,
    push: bool = True,
    clock: Callable[[], datetime] | None = None,
) -> Flask:
    """Build the app. Every path and the clock are injectable so tests get the real thing.

    ``clock`` exists because two screens depend on the current time — whether the keeper
    deadline has passed, and the ``submitted_at`` stamp on a claim — and a test that has to wait
    for a real clock is a test nobody runs.
    """
    app = Flask(__name__)
    # Localhost, single user, no accounts: the session is used for flash messages and nothing
    # else. Not a security boundary, and deliberately not pretending to be one.
    app.secret_key = "rs57-admin-localhost"
    app.config.update(
        DATA_DIR=data_dir,
        DERIVED_DIR=derived_dir,
        REPO=repo,
        PUSH=push,
        CLOCK=clock or datetime.now,
    )

    store = ManualStore(data_dir=data_dir)
    derived = Derived(derived_dir=derived_dir)

    def now() -> datetime:
        return app.config["CLOCK"]()

    def git() -> Git:
        return Git(repo=app.config["REPO"])

    @app.context_processor
    def shared() -> dict[str, Any]:
        """Facts every page needs. Read off the engine rather than retyped into a template.

        ``prior_season`` is here because the payouts link points at the completed season, and
        ``current_season - 1`` in a template is arithmetic — which is exactly what the templates
        are tested for not doing. The rule is worth keeping literal: the moment a year subtraction
        is acceptable in a template, so is a salary addition.
        """
        current = derived.current_season()
        return {
            "prior_season": current - 1 if current is not None else None,
            "keeper_tax": KEEPER_TAX,
            "max_keepers": MAX_KEEPERS,
            "max_prospects": MAX_PROSPECTS,
            "fee_help": FEE_HELP,
            "limits_help": LIMITS_HELP,
            "slot_choices": SLOT_CHOICES,
            "seasons_available": derived.seasons(),
            "current_season": current,
        }

    def season_or_404(year: int):
        loaded = derived.load(year)
        if loaded is None:
            abort(404, f"data/derived/{year}.json does not exist — sync that season first")
        return loaded

    # -- claims -----------------------------------------------------------------

    @app.get("/")
    def home():
        current = derived.current_season()
        if current is None:
            return render_template("empty.html")
        return redirect(url_for("season", year=current))

    @app.get("/season/<int:year>")
    def season(year: int):
        current = season_or_404(year)
        screen = build_season_screen(year, current, derived.load(year - 1), store, now=now())
        return render_template("season.html", screen=screen, source=current)

    @app.get("/season/<int:year>/team/<manager_id>")
    def team(year: int, manager_id: str):
        current = season_or_404(year)
        if manager_id not in current.manager_ids:
            abort(404, f"{manager_id} has no roster in {year}")
        screen = build_team_screen(
            year, manager_id, current, derived.load(year - 1), store
        )
        return render_template("team.html", screen=screen, source=current)

    @app.post("/season/<int:year>/team/<manager_id>/preview")
    def preview(year: int, manager_id: str):
        """Live salary math. Prices and validates, writes nothing."""
        current = season_or_404(year)
        claims, problems = claims_from_form(year, manager_id, request.form.to_dict())
        screen = build_team_screen(
            year, manager_id, current, derived.load(year - 1), store, claims=claims
        )
        return render_template(
            "_claim_form.html", screen=screen, source=current, problems=problems
        )

    @app.post("/season/<int:year>/team/<manager_id>")
    def save(year: int, manager_id: str):
        """Record the claims. An ERROR blocks the save; a REVIEW never does."""
        current = season_or_404(year)
        claims, problems = claims_from_form(
            year,
            manager_id,
            request.form.to_dict(),
            now=now(),
            price_with=(current, store),
        )
        screen = build_team_screen(
            year, manager_id, current, derived.load(year - 1), store, claims=claims
        )

        if problems or screen.blocked:
            # Re-render with the numbers still on screen. "You owe $5 more in fees" is more
            # useful next to the salaries than instead of them, which is why the engine prices
            # a blocked team at all.
            return render_template(
                "_claim_form.html", screen=screen, source=current, problems=problems
            )

        store.save_team_claims(year, manager_id, claims)
        saved = build_team_screen(
            year, manager_id, current, derived.load(year - 1), store, saved=True
        )
        return render_template("_claim_form.html", screen=saved, source=current, problems=[])

    # -- season settings --------------------------------------------------------

    @app.get("/season/<int:year>/settings")
    def settings(year: int):
        current = season_or_404(year)
        return render_template(
            "settings.html",
            year=year,
            source=current,
            settings=store.season(year),
            waiver_year=year + 1,
            derived_winners=[
                (mid, current.name_of(mid)) for mid in derived.derived_consolation_winners(year)
            ],
            managers=[(mid, current.name_of(mid)) for mid in current.manager_ids],
            espn=None,
        )

    @app.post("/season/<int:year>/settings")
    def save_settings(year: int):
        """Record one season's settings.

        **A field absent from the request is left as it was; only a field submitted empty is
        cleared.** An HTML form always posts every input it holds, so "present but blank" is a
        deliberate clear while "missing" means this submission was not about that field. Without
        the distinction, any request that omits a field silently blanks it — and the field most
        likely to be blanked that way is ``consolation_winner_id``, which is a decision somebody
        made about real money and cannot be re-derived.
        """
        current = season_or_404(year)
        form = request.form
        existing = store.season(year)

        def when(field: str) -> datetime | None:
            if field not in form:
                return getattr(existing, field) if existing else None
            raw = (form.get(field) or "").strip()
            if not raw:
                return None
            try:
                return datetime.fromisoformat(raw)
            except ValueError:
                flash(f"{field} is not a date I can read: {raw!r}", "error")
                return getattr(existing, field) if existing else None

        if "consolation_winner_id" in form:
            winner = (form.get("consolation_winner_id") or "").strip() or None
            if winner and winner not in current.manager_ids:
                flash(f"{winner} has no roster in {year}", "error")
                winner = existing.consolation_winner_id if existing else None
        else:
            winner = existing.consolation_winner_id if existing else None

        store.save_season(
            Season(
                year=year,
                season_start=when("season_start"),
                trade_deadline=when("trade_deadline"),
                keeper_deadline=when("keeper_deadline"),
                consolation_winner_id=winner,
            )
        )
        flash(
            f"Recorded {year} settings."
            + (
                f" {current.name_of(winner)} has their fees waived in {year + 1} — check the "
                f"{year + 1} report."
                if winner
                else ""
            ),
            "ok",
        )
        return redirect(url_for("settings", year=year))

    @app.post("/season/<int:year>/settings/espn")
    def settings_from_espn(year: int):
        """Read the deadlines off ESPN instead of retyping them. Fills the form, saves nothing."""
        current = season_or_404(year)
        raw, error = fetch_deadlines(year)
        if error:
            flash(f"ESPN: {error}", "error")
        # Already naive UTC — see reconcile._epoch_ms. Rendered in the same convention the
        # derived files use, so saving this cannot disagree with the trade deadline the
        # prospect check reads.
        filled = {
            key: value.isoformat(" ", "minutes") for key, value in raw.items() if value
        }
        if filled:
            flash("Deadlines read from ESPN. Nothing is saved until you submit.", "ok")
        return render_template(
            "settings.html",
            year=year,
            source=current,
            settings=store.season(year),
            waiver_year=year + 1,
            derived_winners=[
                (mid, current.name_of(mid)) for mid in derived.derived_consolation_winners(year)
            ],
            managers=[(mid, current.name_of(mid)) for mid in current.manager_ids],
            espn=filled,
        )

    # -- overrides --------------------------------------------------------------

    @app.get("/overrides")
    def overrides():
        current_year = derived.current_season()
        current = derived.load(current_year) if current_year else None
        rows = []
        for override in sorted(
            store.overrides(), key=lambda o: (o.season, o.espn_player_id), reverse=True
        ):
            loaded = derived.load(override.season)
            player = (loaded.player_by_id.get(override.espn_player_id)) if loaded else None
            entry = next(
                (
                    e
                    for e in (loaded.roster if loaded else ())
                    if e.espn_player_id == override.espn_player_id
                ),
                None,
            )
            rows.append(
                {
                    "override": override,
                    "name": player.name if player else f"player {override.espn_player_id}",
                    "espn_base": entry.base_salary if entry else None,
                    "manager": loaded.name_of(entry.manager_id) if loaded and entry else None,
                }
            )
        return render_template(
            "overrides.html", rows=rows, year=current_year, source=current
        )

    @app.post("/overrides")
    def add_override():
        override, problems = override_form(request.form.to_dict(), now=now())
        if override is None:
            for problem in problems:
                flash(problem, "error")
            return redirect(url_for("overrides"))
        store.add_override(override)
        flash(
            f"Recorded an override for player {override.espn_player_id} in {override.season}. "
            f"ESPN still holds the distorted value until you change it back and mark this "
            f"reverted.",
            "ok",
        )
        return redirect(url_for("overrides"))

    @app.post("/overrides/revert")
    def revert_override():
        """Flip ``reverted`` once ESPN has been put back. History, not deletion.

        Nothing is removed: the override explains why a base moved, and deleting it would take
        that explanation out of the file that ``check_base_continuity`` sends people to.
        """
        try:
            season = int(request.form.get("season", ""))
            player_id = int(request.form.get("espn_player_id", ""))
            created = datetime.fromisoformat(request.form.get("created_at", ""))
        except ValueError:
            flash("could not identify which override to revert", "error")
            return redirect(url_for("overrides"))

        rows = store.overrides(season)
        updated = [
            row.model_copy(update={"reverted": True})
            if (row.espn_player_id, row.created_at) == (player_id, created)
            else row
            for row in rows
        ]
        if updated == rows:
            flash("no matching override", "error")
        else:
            store.save_overrides(season, updated)
            flash("Marked reverted — ESPN's value wins again for that player.", "ok")
        return redirect(url_for("overrides"))

    # -- payouts ----------------------------------------------------------------

    @app.get("/season/<int:year>/payouts")
    def payouts(year: int):
        rows, totals = _payout_rows(derived, store, year)
        return render_template(
            "payouts.html",
            year=year,
            rows=rows,
            totals=totals,
            schedule=store.prize_schedule(year),
        )

    @app.post("/season/<int:year>/payouts")
    def toggle_paid(year: int):
        label = request.form.get("label", "")
        winner = request.form.get("winner_manager_id", "")
        paid = request.form.get("paid") == "1"
        if not label or not winner:
            abort(400, "a payment needs a label and a winner")
        store.set_paid(year, label, winner, paid, now=now())
        rows, totals = _payout_rows(derived, store, year)
        return render_template("_payout_table.html", year=year, rows=rows, totals=totals)

    # -- reconciliation with ESPN ----------------------------------------------

    @app.get("/season/<int:year>/reconcile")
    def reconcile_espn(year: int):
        current = season_or_404(year)
        picks, error = fetch_keeper_picks(year)
        result = reconcile(year, store.claims(year), picks, current, error=error)
        return render_template("reconcile.html", result=result, source=current, year=year)

    # -- the commit button ------------------------------------------------------

    @app.get("/commit")
    def commit():
        return render_template(
            "commit.html", preview=git().preview(), push=app.config["PUSH"], log=None
        )

    @app.post("/commit")
    def do_commit():
        year = derived.current_season() or 0
        summary = (request.form.get("summary") or "").strip() or "record data/manual/"
        try:
            log = git().commit_and_push(
                commit_message(year, summary), push=app.config["PUSH"]
            )
        except (GitError, OwnershipError) as exc:
            flash(str(exc), "error")
            return render_template(
                "commit.html", preview=git().preview(), push=app.config["PUSH"], log=None
            )
        return render_template(
            "commit.html", preview=git().preview(), push=app.config["PUSH"], log=log
        )

    return app


def _payout_rows(derived: Derived, store: ManualStore, year: int):
    """Join derived payout rows to recorded payments. Totals are added here, not in a template."""
    paid = {
        (payment.label, payment.winner_manager_id): payment
        for payment in store.payments(year)
        if payment.paid
    }
    loaded = derived.load(year)
    names = loaded.names if loaded else {}

    rows = []
    for payout in derived.payouts(year):
        payment = paid.get((payout.label, payout.winner_manager_id or ""))
        rows.append(
            {
                "label": payout.label,
                "amount": payout.amount,
                "winner_manager_id": payout.winner_manager_id,
                "winner": names.get(payout.winner_manager_id or "", payout.winner_manager_id),
                "paid": payment is not None,
                "paid_at": payment.paid_at if payment else None,
            }
        )

    totals = {
        "pot": sum(row["amount"] for row in rows),
        "paid": sum(row["amount"] for row in rows if row["paid"]),
        "unawarded": sum(row["amount"] for row in rows if not row["winner_manager_id"]),
    }
    totals["outstanding"] = totals["pot"] - totals["paid"] - totals["unawarded"]
    return rows, totals
