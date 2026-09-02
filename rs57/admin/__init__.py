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
from rs57.admin.reconcile import (
    fetch_deadlines,
    fetch_keeper_picks,
    fetch_roster_salaries,
    verify,
)
from rs57.admin.screens import (
    FEE_HELP,
    LIMITS_HELP,
    SLOT_CHOICES,
    build_season_screen,
    cash_screen,
    build_team_screen,
    claims_from_form,
    keeper_deadline_fact,
    split_league_form,
    override_form,
    override_row,
    trade_form,
    trade_rows,
    unlinked_override_issues,
)
from rs57.admin.store import DATA, ManualStore, OwnershipError
from rs57.keeper_rules import KEEPER_TAX, MAX_KEEPERS, MAX_PROSPECTS
from rs57.models import PAYOUT_LEDGER_FROM
from rs57.models import Season, to_league_time, utc_now

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

    **The clock reads UTC, not the machine's local time.** It is compared against
    ``keeper_deadline``, which came off ESPN as a naive UTC instant, so ``datetime.now()`` was
    comparing two different timezones and unlocking the console a full UTC offset late. It also
    assumed whoever ran the tool sat in the league's timezone, which nothing guarantees. Every
    stamp this clock writes is therefore UTC too, like every other datetime in ``data/``, and
    the templates convert to Eastern to print one.
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
        CLOCK=clock or utc_now,
    )

    # Stored UTC, shown Eastern, and this filter is the only border between them — the same
    # job ``site.mdy`` does for the public pages. Note what it is deliberately NOT applied to:
    # ``CashTrade.agreed_at`` is a calendar date the commissioner types into a date input, not
    # an instant, and converting it would drag it back to the previous evening.
    app.jinja_env.filters["et"] = to_league_time

    store = ManualStore(data_dir=data_dir)
    derived = Derived(derived_dir=derived_dir)

    def now() -> datetime:
        return app.config["CLOCK"]()

    def git() -> Git:
        return Git(repo=app.config["REPO"])

    @app.context_processor
    def shared() -> dict[str, Any]:
        """Facts every page needs. Read off the engine rather than retyped into a template."""
        current = derived.current_season()
        return {
            "keeper_tax": KEEPER_TAX,
            "max_keepers": MAX_KEEPERS,
            "max_prospects": MAX_PROSPECTS,
            "fee_help": FEE_HELP,
            "limits_help": LIMITS_HELP,
            "slot_choices": SLOT_CHOICES,
            "seasons_available": derived.seasons(),
            "current_season": current,
            # The header's season control, and the tabs beside it. Both read the year off the
            # *request* rather than off `current_season`: the tabs used to be hard-wired to
            # the current season, so opening Money from a 2025 page silently moved you to
            # 2026. Taken from `view_args` rather than threaded through twenty render calls —
            # every year-taking route names its parameter `year`, so this is exact.
            "picker_year": (request.view_args or {}).get("year") or current,
            # Switching season keeps the tab you are on. `team` maps to the keeper board
            # because a manager_id does not carry across years, and everything else (the
            # commit page, the empty page, htmx partials) has no year to begin with.
            "picker_endpoint": (
                request.endpoint
                if request.endpoint in ("season", "settings", "money")
                else "season"
            ),
            # The badge in the nav. Without it the only way to learn that something is
            # unsaved is to go looking on the Commit tab, and a change recorded but never
            # committed reaches nobody. Failure-tolerant on purpose: a git problem must not
            # take down every page in the tool, and the Commit tab reports it properly.
            "unsaved": _unsaved_count(app.config["REPO"]),
            # The override form appears on two pages and needs the trade list on both, so it
            # is shared rather than passed by each route that happens to render the form.
            "trade_choices": store.trades(),
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

    def _cash(year: int):
        """This season's draft cash, audited against the whole ledger.

        The store reads are unfiltered on purpose — ``cash_screen`` needs every trade and every
        override to keep ``trade_groups`` whole. See ``CashScreen``.
        """
        all_trades = store.trades()
        all_overrides = store.overrides()
        years = {t.draft_year for t in all_trades} | {o.season for o in all_overrides} | {year}
        seasons = {yr: derived.load(yr) for yr in years}
        roster = [entry for loaded in seasons.values() if loaded for entry in loaded.roster]
        return cash_screen(year, all_trades, all_overrides, seasons, roster)

    @app.get("/season/<int:year>")
    def season(year: int):
        current = season_or_404(year)
        screen = build_season_screen(
            year,
            current,
            derived.load(year - 1),
            store,
            now=now(),
            first_nfl_season=derived.first_nfl_seasons(),
        )
        return render_template(
            "season.html",
            screen=screen,
            source=current,
            cash=_cash(year),
            year=year,
            # The franchise pickers on the trade table belong to the season being edited, not
            # to whatever season happens to be current. Editing a 2025 trade used to offer
            # 2026's names.
            managers=[(mid, current.name_of(mid)) for mid in current.manager_ids],
        )

    @app.get("/season/<int:year>/team/<manager_id>")
    def team(year: int, manager_id: str):
        current = season_or_404(year)
        if manager_id not in current.manager_ids:
            abort(404, f"{manager_id} has no roster in {year}")
        screen = build_team_screen(
            year,
            manager_id,
            current,
            derived.load(year - 1),
            store,
            first_nfl_season=derived.first_nfl_seasons(),
            keeper_deadline=keeper_deadline_fact(current, now=now()),
        )
        return render_template("team.html", screen=screen, source=current)

    @app.post("/season/<int:year>/team/<manager_id>/preview")
    def preview(year: int, manager_id: str):
        """Live salary math. Prices and validates, writes nothing.

        Nothing gates it and nothing gates the save either — see ``KeeperDeadline``. The
        deadline is still passed through so the fragment renders the same state as the page it
        replaces.
        """
        current = season_or_404(year)
        posted = split_league_form(request.form.to_dict()).get(manager_id, {})
        claims, problems = claims_from_form(year, manager_id, posted)
        screen = build_team_screen(
            year,
            manager_id,
            current,
            derived.load(year - 1),
            store,
            claims=claims,
            first_nfl_season=derived.first_nfl_seasons(),
            keeper_deadline=keeper_deadline_fact(current, now=now()),
        )
        return render_template(
            "_claim_form.html", screen=screen, source=current, problems=problems
        )

    @app.post("/season/<int:year>/team/<manager_id>")
    def save(year: int, manager_id: str):
        """Record the claims. An ERROR blocks the save; a REVIEW never does.

        **The deadline does not block it.** It used to, until 2026-09-01 — see ``KeeperDeadline``.
        A claim recorded before the deadline is reported as provisional on the card rather than
        refused, because manual entry is the only way selections reach this tool and the
        deadline is exactly when that entry happens.
        """
        current = season_or_404(year)
        claims, problems = claims_from_form(
            year,
            manager_id,
            split_league_form(request.form.to_dict()).get(manager_id, {}),
            now=now(),
            price_with=(current, store),
        )
        screen = build_team_screen(
            year,
            manager_id,
            current,
            derived.load(year - 1),
            store,
            claims=claims,
            first_nfl_season=derived.first_nfl_seasons(),
            keeper_deadline=keeper_deadline_fact(current, now=now()),
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
            year,
            manager_id,
            current,
            derived.load(year - 1),
            store,
            saved=True,
            first_nfl_season=derived.first_nfl_seasons(),
            keeper_deadline=keeper_deadline_fact(current, now=now()),
        )
        return render_template("_claim_form.html", screen=saved, source=current, problems=[])

    @app.post("/season/<int:year>/record")
    def record_league(year: int):
        """Record every franchise on the board in one action.

        **Team by team, not all-or-nothing.** Twelve franchises are entered in one sitting, so
        recording them is one button — but a single illegal fee spread must not discard the
        other eleven teams' work, and neither must it record itself. Each team is priced,
        validated and written on its own; the ones that cannot be are skipped and named.

        The deadline does not gate this, and has not since 2026-09-01 — see ``KeeperDeadline``.
        """
        current = season_or_404(year)
        posted = split_league_form(request.form.to_dict())
        recorded: list[str] = []
        skipped: list[tuple[str, str]] = []

        for manager_id in current.manager_ids:
            # **A franchise absent from the request is left as it was.** Only one whose
            # fields arrived, and arrived empty, is cleared. The board posts all twelve
            # every time, so "missing" means this submission was not about that team — and
            # writing an empty claim list for it would delete a record nobody touched. The
            # same rule ``save_settings`` follows, for the same reason.
            if manager_id not in posted:
                continue
            claims, problems = claims_from_form(
                year,
                manager_id,
                posted[manager_id],
                now=now(),
                price_with=(current, store),
            )
            screen = build_team_screen(
                year,
                manager_id,
                current,
                derived.load(year - 1),
                store,
                claims=claims,
                first_nfl_season=derived.first_nfl_seasons(),
                keeper_deadline=keeper_deadline_fact(current, now=now()),
            )
            if problems:
                skipped.append((current.name_of(manager_id), problems[0]))
            elif screen.blocked:
                skipped.append(
                    (current.name_of(manager_id), screen.errors[0].message)
                )
            else:
                store.save_team_claims(year, manager_id, claims)
                recorded.append(current.name_of(manager_id))

        screen = build_season_screen(
            year,
            current,
            derived.load(year - 1),
            store,
            now=now(),
            first_nfl_season=derived.first_nfl_seasons(),
        )
        return render_template(
            "_board.html",
            screen=screen,
            source=current,
            recorded=recorded,
            skipped=skipped,
        )

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

        if "draft_doodle_url" in form:
            doodle_url = (form.get("draft_doodle_url") or "").strip() or None
        else:
            doodle_url = existing.draft_doodle_url if existing else None

        store.save_season(
            Season(
                year=year,
                season_start=when("season_start"),
                trade_deadline=when("trade_deadline"),
                draft_doodle_url=doodle_url,
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

    def _back_to():
        """Return to the season page the form was posted from.

        ``return_year`` is parsed as an int and fed to ``url_for`` rather than used as a URL,
        so a hand-edited field can only ever name a season route — there is nowhere for it to
        redirect to that this app does not already serve.

        No endpoint default any more: draft cash lives on the season page, and ``url_for`` on
        that route needs a year nobody has at this point. ``home`` already resolves to the
        current season, or renders ``empty.html`` when nothing is synced.
        """
        raw = (request.form.get("return_year") or "").strip()
        try:
            return redirect(url_for("season", year=int(raw)))
        except ValueError:
            return redirect(url_for("home"))

    @app.get("/overrides")
    def overrides():
        """Kept as a permanent redirect target, not a page. See ``trades``."""
        return redirect(url_for("home"))

    @app.post("/overrides")
    def add_override():
        # `to_dict()` keeps only the first value of a multi-select, so the trade links are
        # re-joined from `getlist` before the form sees them.
        posted = request.form.to_dict()
        posted["trade_ids"] = ",".join(request.form.getlist("trade_ids"))
        override, problems = override_form(
            posted, now=now(), known_trades=store.trade_ids()
        )
        if override is None:
            for problem in problems:
                flash(problem, "error")
            return _back_to()
        store.add_override(override)
        flash(
            f"Recorded an override for player {override.espn_player_id} in {override.season}. "
            f"ESPN still holds the distorted value until you change it back and mark this "
            f"reverted.",
            "ok",
        )
        return _back_to()

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
            return _back_to()

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
        return _back_to()

    @app.post("/overrides/edit")
    def edit_override():
        """Amend a recorded override in place.

        The row is identified by where it is now — draft, player and creation stamp — and the
        stamp is carried onto the replacement rather than reissued, because that stamp is the
        row's identity and is how a leg is told apart from a second override on the same player
        in the same draft.

        ``reverted`` is carried over too: it is flipped by its own button, and letting an edit
        of the salary silently un-revert a row would hand ESPN's value back without anybody
        saying so.
        """
        try:
            orig_season = int(request.form.get("orig_season", ""))
            orig_player = int(request.form.get("orig_espn_player_id", ""))
            created = datetime.fromisoformat(request.form.get("created_at", ""))
        except ValueError:
            flash("could not identify which override to edit", "error")
            return _back_to()

        current = next(
            (
                o
                for o in store.overrides(orig_season)
                if (o.espn_player_id, o.created_at) == (orig_player, created)
            ),
            None,
        )
        if current is None:
            flash("no matching override", "error")
            return _back_to()

        posted = request.form.to_dict()
        posted["trade_ids"] = ",".join(request.form.getlist("trade_ids"))
        posted.setdefault("reason", current.reason)
        replacement, problems = override_form(
            posted, now=created, known_trades=store.trade_ids()
        )
        if replacement is None:
            for problem in problems:
                flash(problem, "error")
        elif store.update_override(
            orig_season,
            orig_player,
            created,
            replacement.model_copy(update={"reverted": current.reverted}),
        ):
            flash(f"Updated the override for player {replacement.espn_player_id}.", "ok")
        else:
            flash("no matching override", "error")
        return _back_to()

    # -- cash trades ------------------------------------------------------------

    @app.get("/trades")
    def trades():
        """Kept as a permanent redirect target, not a page.

        Draft cash moved onto the keeper board (commissioner, 2026-09-01): a salary edit is
        made in the same sitting as the keeper salaries it distorts, so it is entered on the
        same screen. The season switcher there reaches every year the ledger can name.

        The endpoint stays because bookmarks and old links name it. It cannot carry a year, so
        it lands on ``home`` — the current season, which is where the work is.
        """
        return redirect(url_for("home"))

    @app.post("/trades")
    def add_trade():
        current_year = derived.current_season()
        current = derived.load(current_year) if current_year else None
        trade, problems = trade_form(
            request.form.to_dict(),
            now=now(),
            taken=store.trade_ids(),
            known_managers=current.manager_ids if current else (),
        )
        if trade is None:
            for problem in problems:
                flash(problem, "error")
            return _back_to()
        store.add_trade(trade)
        flash(
            f"Recorded ${trade.amount} from {trade.from_manager_id} to "
            f"{trade.to_manager_id} at the {trade.draft_year} draft. It stays unbalanced until "
            f"both legs are attached — the salary overrides that express it in ESPN.",
            "ok",
        )
        return _back_to()

    @app.post("/trades/<trade_id>/delete")
    def delete_trade(trade_id: str):
        """Remove a trade entered in error.

        Its legs are deliberately left alone. An override is a real ESPN edit that happened,
        and deleting the trade it was filed under must not quietly erase the record of money
        moving — the legs are reported as naming a trade that is not on file, which is a
        finding somebody has to answer rather than a silence.
        """
        if store.delete_trade(trade_id):
            orphaned = sum(1 for o in store.overrides() if trade_id in o.trade_ids)
            flash(
                f"Deleted {trade_id}."
                + (
                    f" {orphaned} override(s) still name it and now report as unattached — "
                    f"re-attach or delete them too."
                    if orphaned
                    else ""
                ),
                "ok",
            )
        else:
            flash(f"no trade {trade_id} on file", "error")
        return _back_to()

    @app.post("/overrides/delete")
    def delete_override():
        """Remove an override entered in error.

        Not the same as reverting. A reverted row stays on file because it explains why a base
        moved and ``check_base_continuity`` sends people looking for it; this is for a row that
        should never have been recorded at all.
        """
        try:
            season = int(request.form.get("season", ""))
            player_id = int(request.form.get("espn_player_id", ""))
            created = datetime.fromisoformat(request.form.get("created_at", ""))
        except ValueError:
            flash("could not identify which override to delete", "error")
            return _back_to()
        if store.delete_override(season, player_id, created):
            flash(f"Deleted the {season} override for player {player_id}.", "ok")
        else:
            flash("no matching override", "error")
        return _back_to()

    @app.post("/trades/<trade_id>/edit")
    def edit_trade(trade_id: str):
        """Amend a recorded trade in place, keeping its id.

        The id is what every leg names, so it survives the edit even when the season or the two
        parties change. Changing the parties can legitimately make an attached leg belong to
        neither of them — that is reported rather than prevented, because the fix might be the
        trade or might be the leg and only a human knows which.
        """
        if trade_id not in store.trade_ids():
            abort(404, f"no trade {trade_id} on file")
        current_year = derived.current_season()
        current = derived.load(current_year) if current_year else None
        trade, problems = trade_form(
            request.form.to_dict(),
            now=now(),
            taken=store.trade_ids() - {trade_id},
            known_managers=current.manager_ids if current else (),
            existing_id=trade_id,
        )
        if trade is None:
            for problem in problems:
                flash(problem, "error")
        elif store.update_trade(trade):
            flash(f"Updated {trade.id}.", "ok")
        else:
            flash(f"no trade {trade_id} on file", "error")
        return _back_to()

    # -- money: dues in, prizes out ---------------------------------------------

    @app.get("/season/<int:year>/money")
    def money(year: int):
        """One season's real dollars, both directions.

        Dues are collected at the start of a season and prizes handed out at the end of that
        same season, so they belong on one screen keyed on one year. A season still being
        played has live dues and no prizes yet; that is the true state of it, and the prize
        half fills in on this same page once the season derives.
        """
        season_or_404(year)
        dues_rows, dues_totals = _dues_rows(derived, store, year)
        payout_rows, payout_totals = _payout_rows(derived, store, year)
        settle_rows, settle_totals = _settlement_rows(derived, store, year)
        return render_template(
            "money.html",
            year=year,
            dues_rows=dues_rows,
            dues_totals=dues_totals,
            rows=payout_rows,
            totals=payout_totals,
            settle_rows=settle_rows,
            settle_totals=settle_totals,
            ledger_from=PAYOUT_LEDGER_FROM,
            schedule=store.prize_schedule(year),
        )

    @app.post("/season/<int:year>/money/dues")
    def toggle_dues_paid(year: int):
        loaded = season_or_404(year)
        manager_id = request.form.get("manager_id", "")
        paid = request.form.get("paid") == "1"
        if not manager_id:
            abort(400, "a dues record needs a franchise")
        # The season's own franchise list is the league's own record, so a manager id it does
        # not contain is refused here rather than written and reported later. An unrecognised
        # id would sit in the file reading as somebody's payment while the public panel went
        # on showing that franchise as owing.
        if manager_id not in loaded.names:
            abort(400, f"{manager_id!r} has no franchise in {year}")
        store.set_dues_paid(year, manager_id, paid, now=now())
        dues_rows, dues_totals = _dues_rows(derived, store, year)
        return render_template(
            "_dues_table.html", year=year, dues_rows=dues_rows, dues_totals=dues_totals
        )

    @app.post("/season/<int:year>/money/payouts")
    def toggle_payout_paid(year: int):
        """Mark one franchise settled for the season. Refused before the ledger begins."""
        loaded = season_or_404(year)
        if year < PAYOUT_LEDGER_FROM:
            abort(400, f"{year} was settled before this ledger began — nothing to record")
        manager_id = request.form.get("manager_id", "")
        paid = request.form.get("paid") == "1"
        if not manager_id:
            abort(400, "a payout needs a franchise")
        if manager_id not in loaded.names:
            abort(400, f"{manager_id!r} has no franchise in {year}")
        # A franchise that won nothing is owed nothing, and is not a line on the settlement
        # sheet. Recording a payout to one writes a row the screen cannot show and therefore
        # cannot undo — it would sit in the file asserting a payment that never happened.
        settle_rows, settle_totals = _settlement_rows(derived, store, year)
        if manager_id not in {row["manager_id"] for row in settle_rows}:
            abort(400, f"{manager_id!r} won nothing in {year} — there is nothing to pay")
        store.set_paid(year, manager_id, paid, now=now())
        settle_rows, settle_totals = _settlement_rows(derived, store, year)
        return render_template(
            "_settlement_table.html", year=year,
            settle_rows=settle_rows, settle_totals=settle_totals,
            ledger_from=PAYOUT_LEDGER_FROM,
        )

    @app.get("/season/<int:year>/payouts")
    def payouts(year: int):
        """Kept as a permanent redirect target, not a page.

        Payouts and dues merged onto one Money tab — they are one season's two ends. This
        endpoint stays because bookmarks still name it, the same way ``/overrides`` does.
        """
        return redirect(url_for("money", year=year))

    # -- verification against ESPN ----------------------------------------------

    @app.post("/season/<int:year>/verify")
    def verify_espn(year: int):
        """Read ESPN's live rosters and compare. **Writes nothing, ever.**

        POST rather than GET, and never on the way into the season page: this is the only screen
        that touches the network, and an unreachable ESPN must not take down the page the whole
        offseason is entered on.

        The field compared against is the derived season's own ``base_salary_field`` —
        ``keeperValue`` before a season's auction and ``keeperValueFuture`` after. It is read
        from the file rather than decided here so the verify cannot disagree with the pipeline
        that produced the numbers it is checking.
        """
        current = season_or_404(year)
        team_ids = sorted(int(mid.removeprefix("t")) for mid in current.manager_ids)
        salaries, sizes, error = fetch_roster_salaries(
            year, current.base_salary_field, team_ids
        )
        # Secondary, and only meaningful once the draft has run. Kept because it catches the
        # direction a roster read cannot, and it costs one request.
        picks, pick_error = fetch_keeper_picks(year)
        result = verify(
            year,
            store.claims(year),
            salaries,
            sizes,
            current,
            draft_picks=len(picks),
            error=error or pick_error if error else None,
        )
        return render_template("_verify.html", result=result, year=year)

    # -- the commit button ------------------------------------------------------

    @app.post("/discard")
    def discard():
        """Throw away working-tree changes to data/manual/. Destroys work, so it confirms."""
        paths = request.form.getlist("path")
        try:
            log = git().discard(paths)
        except GitError as exc:
            flash(str(exc), "error")
            return redirect(url_for("commit"))
        for line in log:
            flash(line, "ok")
        return redirect(url_for("commit"))

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


def _unsaved_count(repo: Path) -> int:
    """How many files in ``data/manual/`` differ from the last commit.

    Returns 0 rather than raising when git cannot answer — no repo, no git, a broken index.
    The nav badge is a convenience; the Commit tab is where a git problem is reported in full.
    """
    try:
        return sum(1 for change in Git(repo=repo).changes() if change.owned)
    except (GitError, OSError):
        return 0


def _dues_rows(derived: Derived, store: ManualStore, year: int):
    """Join the season's franchises to recorded dues. Counts are added here, not in a template.

    Every franchise in the season gets a row whether or not it has a dues record: the screen
    is a list of who still owes, and a franchise that has paid nothing would otherwise be the
    one team missing from it.
    """
    paid = {row.manager_id: row for row in store.dues(year) if row.paid}
    loaded = derived.load(year)
    names = loaded.names if loaded else {}

    rows = []
    for manager_id, name in sorted(names.items(), key=lambda item: item[1].strip().lower()):
        record = paid.get(manager_id)
        rows.append(
            {
                "manager_id": manager_id,
                "name": name,
                "paid": record is not None,
                "paid_at": record.paid_at if record else None,
            }
        )

    totals = {
        "teams": len(rows),
        "paid": sum(1 for row in rows if row["paid"]),
    }
    totals["outstanding"] = totals["teams"] - totals["paid"]
    return rows, totals


def _settlement_rows(derived: Derived, store: ManualStore, year: int):
    """What each franchise is owed for the season, and whether it has been handed over.

    One row per franchise that won something. A franchise that won nothing is owed nothing and
    is not a line on a settlement sheet.

    The amount is summed from the derived payout rows rather than stored: it is what ``stats``
    already computed, and a second copy could disagree with the first.
    """
    paid = {row.manager_id: row for row in store.payments(year) if row.paid}
    loaded = derived.load(year)
    names = loaded.names if loaded else {}

    won: dict[str, int] = {}
    for payout in derived.payouts(year):
        if payout.winner_manager_id:
            won[payout.winner_manager_id] = won.get(payout.winner_manager_id, 0) + payout.amount

    rows = []
    for manager_id, amount in sorted(won.items(), key=lambda item: (-item[1], item[0])):
        record = paid.get(manager_id)
        rows.append(
            {
                "manager_id": manager_id,
                "name": names.get(manager_id, manager_id),
                "amount": amount,
                "paid": record is not None,
                "paid_at": record.paid_at if record else None,
            }
        )

    totals = {
        "owed": sum(row["amount"] for row in rows),
        "paid": sum(row["amount"] for row in rows if row["paid"]),
        "teams": len(rows),
        "settled": sum(1 for row in rows if row["paid"]),
    }
    totals["outstanding"] = totals["owed"] - totals["paid"]
    return rows, totals


def _payout_rows(derived: Derived, store: ManualStore, year: int):
    """The season's derived prizes, named. Totals are added here, not in a template.

    Carries nothing about who has been handed their money: that is not tracked on this screen
    (commissioner, 2026-08-31). Dues are what the league chases, and that lives above.
    """
    loaded = derived.load(year)
    names = loaded.names if loaded else {}

    rows = [
        {
            "label": payout.label,
            "amount": payout.amount,
            "winner_manager_id": payout.winner_manager_id,
            "winner": names.get(payout.winner_manager_id or "", payout.winner_manager_id),
        }
        for payout in derived.payouts(year)
    ]

    totals = {
        "pot": sum(row["amount"] for row in rows),
        "unawarded": sum(row["amount"] for row in rows if not row["winner_manager_id"]),
    }
    return rows, totals
