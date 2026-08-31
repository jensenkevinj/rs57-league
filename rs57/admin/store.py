"""The only writer of ``data/manual/``.

Every write in this package goes through :meth:`ManualStore.write`, which refuses any path
outside ``data/manual/``. That is the mirror image of the guard in
``.github/workflows/nightly.yml``: the Action fails if anything wrote ``data/manual/`` or
``data/history/``, and this fails if anything here is ever pointed at ``data/derived/`` or
``data/history/``. NO FILE HAS TWO WRITERS, checked on both sides rather than remembered.

Why a guard instead of care: the tool reads ``data/derived/`` on every screen to price a claim,
so a path variable holding a derived file is always within arm's reach of a write call. The
failure mode is not a typo, it is a plausible refactor.

Preserving prose
----------------

``payouts.json`` and ``prospects.json`` carry ``_about`` keys holding the only written
explanation of why 2023 has no payouts and why prospects have to be recorded by hand. Writes
here **merge into the loaded document** rather than replacing it, so every underscore-prefixed
key survives a round trip. There is a test.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rs57.history import HistoryStore
from rs57.models import (
    CashTrade,
    Dues,
    KeeperClaim,
    KeeperSlot,
    Payment,
    PrizeSchedule,
    SalaryOverride,
    Season,
    dump_json,
)

ROOT = Path(__file__).resolve().parent.parent.parent
DATA = ROOT / "data"

CLAIMS = "claims.json"
OVERRIDES = "overrides.json"
TRADES = "trades.json"
SEASONS = "seasons.json"
PAYMENTS = "payments.json"
PAYOUTS = "payouts.json"
DUES = "dues.json"
PROSPECTS = "prospects.json"

ABOUT: dict[str, list[str]] = {
    CLAIMS: [
        "Keeper claims, by the season they are kept in. Written by the admin tool",
        "(python -m rs57.admin) and by nothing else.",
        "",
        "A claim is the RECORD of what a manager declared and what they owe for it. ESPN is",
        "downstream: the commissioner enters each keeper into ESPN's auction at the",
        "computed_salary below, and next season ESPN reports that number back as the player's",
        "base. check_base_continuity is what audits that hand-entry a year later, so a claim",
        "that disagrees with ESPN is a real finding, not a rounding difference.",
        "",
        "computed_salary is frozen at submission: it is what the manager was told they owed.",
        "The tool recomputes it on every screen and flags a disagreement rather than quietly",
        "rewriting the recorded figure.",
        "",
        "slot is K1/K2/K3 or PROSPECT, and it cannot come from ESPN: draftSettings.keeperCount",
        "is 4 and ESPN marks all four picks keeper=True with nothing recording which slot each",
        "filled. That gap is what taxed a prospect $5 in the old spreadsheet.",
    ],
    OVERRIDES: [
        "Salary overrides, by season. Written by the admin tool and by nothing else.",
        "",
        "An override is a DRAFT-CASH TRADE, not a correction. Managers trade draft cash, ESPN",
        "has no native support, so the commissioner hand-edits a few player salaries on the two",
        "teams involved and changes them back before the next draft. actual_salary is the TRUE",
        "value and ESPN holds the distorted one for as long as reverted is false.",
        "",
        "This is NOT the place to patch an ESPN value somebody thinks is wrong.",
        "",
        "Live overrides should net to zero league-wide, because a cash trade moves money",
        "between two teams -- check_override_balance audits that. One historical row has an",
        "unrecoverable counterparty and is flagged unpaired_ok.",
        "",
        "'reason' is free text typed by a human and rendered on a PUBLIC site. Autoescaping is",
        "on and no template may pass it through the safe filter.",
        "",
        "trade_id names the row in trades.json this override is a leg of. Optional: rows that",
        "predate that file have none, and a leg with no recoverable counterparty never will.",
    ],
    TRADES: [
        "Draft-cash trades, keyed by DRAFT YEAR. Written by the admin tool and by nothing else.",
        "",
        "draft_year is the auction the cash moves at, NOT the season the deal was struck in.",
        "A trade agreed 25 Nov 2025 spends its money at the 2026 auction: draft_year 2026,",
        "agreed_at in 2025. This is the only file where those two years come apart -- for a",
        "roster or a claim, season Y and draft Y are the same integer.",
        "",
        "ESPN cannot move auction budget between teams, so a cash trade is EXPRESSED as",
        "hand-edited player salaries -- the overrides.json rows that name this trade's id in",
        "their trade_id. Those rows are the legs; these rows are the trades themselves.",
        "",
        "amount dollars move FROM from_manager_id TO to_manager_id. The receiving team is",
        "under-charged by ESPN, which frees that much budget, so its leg's (actual_salary -",
        "espn base) is POSITIVE and the paying team's is negative by the same amount.",
        "check_cash_trades audits each side against the declared amount -- per side, not",
        "netted, because a net of zero is also what two legs on the same team would give and",
        "that moves no budget at all.",
        "",
        "This exists because netting the whole league cannot tell one balanced trade from two",
        "unrelated mistakes that happen to cancel.",
        "",
        "'note' is free text typed by a human and rendered on a PUBLIC site, exactly like an",
        "override's reason. Autoescaping is on and no template may pass it through the safe",
        "filter. No manager real names -- franchise ids only.",
    ],
    SEASONS: [
        "Per-season settings, keyed by year. Written by the admin tool and by nothing else.",
        "",
        "consolation_winner_id is the manager who won THIS season's consolation bracket, and",
        "their keeper fees are waived in year + 1. Read it off by one year and every waiver",
        "lands on the wrong team. Recording it here is what turns a derived guess into a",
        "decision -- until a year is recorded, the tool prices that year's keepers with fees ON",
        "and says the waiver is unconfirmed.",
        "",
        "keeper_deadline and draft_date do NOT live here (commissioner, 2026-08-26). Both are",
        "ESPN facts -- draftSettings.keeperDeadlineDate and .date -- and a hand-typed copy of",
        "one had already drifted from what ESPN actually held. They are read straight off the",
        "nightly sync in data/derived/{year}.json (DerivedSeason.keeper_deadline / .draft_date)",
        "and nothing in this tool can override them.",
        "",
        "The keeper deadline is enforced UNTIL it passes and never after (commissioner,",
        "2026-08-04). No salary is entered before the deadline, so holding claims until then",
        "costs nothing and stops a number being recorded while managers can still change their",
        "minds. Nothing ever re-locks: after the deadline the console stays open for good.",
        "",
        "A season not yet synced, or one ESPN has not given a keeperDeadlineDate, is NOT",
        "locked. An unrecorded deadline and a future one are different facts -- collapsing",
        "them would freeze a freshly synced season with no way out on screen.",
    ],
    PAYMENTS: [
        "Which franchises have been paid out for a season. Written by the admin tool and by",
        "nothing else.",
        "",
        "The mirror of dues.json and keyed the same way, on (season, manager_id). Dues come IN",
        "at the start of a season; the prize money goes OUT at the end of it.",
        "",
        "PER FRANCHISE, NOT PER PRIZE. Prizes accrue all season and are settled in one payment",
        "at the end, so 'has the Week 6 high score been paid?' is a question nobody asks. The",
        "money moves once, in aggregate (commissioner, 2026-08-31).",
        "",
        "Absence means unsettled. A franchise that has not been paid has no row here rather",
        "than one saying paid: false.",
        "",
        "The AMOUNT is deliberately not here. What a franchise is owed is the sum of what it",
        "won, which stats already derives; a copy here could disagree with it.",
        "",
        "Seasons before 2026 were settled outside this repo and are not tracked here at all.",
        "See PAYOUT_LEDGER_FROM in rs57/models.py.",
        "",
        "No payment method, no handles, no notes: this file is committed to a PUBLIC repo.",
    ],
    DUES: [
        "Which franchises have paid their buy-in, by season. Written by the admin tool and by",
        "nothing else.",
        "",
        "This is money paid IN. payments.json is the other direction -- a prize handed OUT to a",
        "winner. They are the same season's two ends, dues at the start and prizes at the",
        "finish, which is why one admin screen shows both.",
        "",
        "Neither is a keeper fee. Fees and the $5 tax are auction budget and never change",
        "hands; these are real dollars.",
        "",
        "Absence means unpaid. An unpaid franchise has no row here rather than one saying",
        "paid: false, so a mistyped manager id cannot linger in the file after it is undone.",
        "",
        "The AMOUNT is deliberately not here. The buy-in is one figure the whole league knows",
        "and nothing in data/ records it today; putting it on twelve rows would be twelve",
        "copies of a number nobody has written down once.",
        "",
        "No payment method, no handles, no notes: this file is committed to a PUBLIC repo.",
    ],
}
"""The ``_about`` prose written into each file this tool creates.

Only used when creating a file that does not exist yet; an existing ``_about`` is preserved
verbatim, including the two files this tool did not write.
"""


class OwnershipError(RuntimeError):
    """A write was aimed outside ``data/manual/``.

    Raised rather than logged. The nightly Action fails its run over the same mistake in the
    other direction, and a tool that merely warned would leave the repo with two writers on one
    file — which is the one invariant everything else here is built on.
    """


@dataclass(frozen=True)
class ManualStore:
    """Reads and writes ``data/manual/``. Reads nothing else, writes nothing else.

    ``data_dir`` is injectable so tests get a real store over a temporary tree rather than a
    mocked one — the guard is part of what is under test.
    """

    data_dir: Path = DATA

    # -- paths ------------------------------------------------------------------

    @property
    def manual(self) -> Path:
        return self.data_dir / "manual"

    def path(self, name: str) -> Path:
        """The full path of a file in ``data/manual/``, refusing anything that escapes it."""
        candidate = (self.manual / name).resolve()
        manual = self.manual.resolve()
        if candidate == manual or manual not in candidate.parents:
            raise OwnershipError(
                f"{candidate} is not inside {manual} — the admin tool writes data/manual/ and "
                f"nothing else. data/derived/ belongs to the nightly Action and data/history/ "
                f"is frozen."
            )
        return candidate

    # -- raw document IO --------------------------------------------------------

    def load(self, name: str) -> dict[str, Any]:
        """The file as a plain dict, or ``{}`` when it does not exist yet."""
        path = self.path(name)
        if not path.exists():
            return {}
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"data/manual/{name} is not a JSON object")
        return loaded

    def write(self, name: str, doc: dict[str, Any]) -> Path:
        """Serialize ``doc`` into ``data/manual/{name}`` with ``dump_json``.

        ``dump_json`` sorts keys and ends with a newline. A commit button that produced an
        unreadable diff would be worse than no commit button — the diff is the only review step
        between this tool and a public repo.
        """
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(doc, path)
        return path

    def _merge(self, name: str, **sections: Any) -> Path:
        """Replace only the named sections, keeping ``_about`` and every other key intact."""
        doc = self.load(name)
        if not doc and name in ABOUT:
            doc["_about"] = ABOUT[name]
        doc.update(sections)
        return self.write(name, doc)

    # -- claims -----------------------------------------------------------------

    def claims(self, season: int | None = None) -> list[KeeperClaim]:
        """Recorded claims, for one season or for every season on file.

        The season key and each row's own ``season`` field must agree. They are redundant on
        purpose — a row stays loadable into ``KeeperClaim`` on its own — and a disagreement is
        a corrupted file rather than something to guess at.
        """
        doc = self.load(CLAIMS)
        out: list[KeeperClaim] = []
        for year, rows in sorted((doc.get("seasons") or {}).items()):
            if season is not None and int(year) != season:
                continue
            for row in rows:
                claim = KeeperClaim(**row)
                if claim.season != int(year):
                    raise ValueError(
                        f"claims.json: a row under season {year} says season {claim.season}"
                    )
                out.append(claim)
        return out

    def save_team_claims(self, season: int, manager_id: str, claims: list[KeeperClaim]) -> Path:
        """Replace one team's claims for one season, leaving every other team alone.

        Whole-team replacement rather than per-player upsert: un-declaring a keeper is a normal
        thing to do, and a merge that could only add rows would make it impossible.
        """
        doc = self.load(CLAIMS)
        seasons = dict(doc.get("seasons") or {})
        kept = [
            row
            for row in seasons.get(str(season), [])
            if row.get("manager_id") != manager_id
        ]
        rows = kept + [claim.model_dump(mode="json") for claim in claims]
        seasons[str(season)] = sorted(
            rows, key=lambda row: (row["manager_id"], row["slot"], row["espn_player_id"])
        )
        return self._merge(CLAIMS, seasons=seasons)

    def prior_prospect_ids(self, before_season: int) -> tuple[set[int], bool]:
        """Players already kept in the PROSPECT slot in an earlier season.

        Two sources, unioned: claims recorded here for the season being decided, and the
        frozen claims in ``data/history/``. Both are real ``KeeperClaim`` rows carrying a
        slot, so the answer is **derived** from ``slot == PROSPECT``.

        This used to union a hand-maintained ``data/manual/prospects.json`` instead, which
        existed only because no completed season recorded a slot — ESPN marks all four keeper
        picks identically. The history backfill supplies those slots, which is what that
        file's own header said had to happen before it could go. Deleting it any earlier
        would have silently re-taxed every prospect $5.

        Returns the ids and whether ``history/`` contributed any, so a screen can say where
        the answer came from.
        """
        from_claims = {
            claim.espn_player_id
            for claim in self.claims()
            if claim.slot is KeeperSlot.PROSPECT and claim.season < before_season
        }
        # Read-only, and the only place this package looks outside data/manual/. HistoryStore
        # owns that directory; nothing here may write it.
        frozen = HistoryStore(data_dir=self.data_dir).prospect_ids(before_season)
        return from_claims | frozen, bool(frozen - from_claims)

    # -- overrides --------------------------------------------------------------

    def overrides(self, season: int | None = None) -> list[SalaryOverride]:
        doc = self.load(OVERRIDES)
        out: list[SalaryOverride] = []
        for year, rows in sorted((doc.get("seasons") or {}).items()):
            if season is not None and int(year) != season:
                continue
            out += [SalaryOverride(**row) for row in rows]
        return out

    def save_overrides(self, season: int, overrides: list[SalaryOverride]) -> Path:
        doc = self.load(OVERRIDES)
        seasons = dict(doc.get("seasons") or {})
        rows = sorted(
            (override.model_dump(mode="json") for override in overrides),
            key=lambda row: (row["espn_player_id"], row["created_at"]),
        )
        # An emptied year is removed, not left as `"2026": []`. A bucket holding nothing says
        # nothing, and it shows up in the diff of a public repo as if something were there.
        if rows:
            seasons[str(season)] = rows
        else:
            seasons.pop(str(season), None)
        return self._merge(OVERRIDES, seasons=seasons)

    def add_override(self, override: SalaryOverride) -> Path:
        return self.save_overrides(
            override.season, self.overrides(override.season) + [override]
        )

    def update_override(
        self,
        season: int,
        espn_player_id: int,
        created_at: datetime,
        replacement: SalaryOverride,
    ) -> bool:
        """Replace an override, **including when its draft or its player changed**.

        The three arguments identify the row as it is on file now; ``replacement`` is what it
        becomes. Rows are stored under a draft-year key, so an edit that moves one to a
        different year has to be removed from the old year as well as written to the new — the
        same trap ``update_trade`` has, and the same fix.

        ``created_at`` is carried over by the caller, not reissued: it is the row's identity and
        re-stamping it on every save would break the link a leg holds to it.
        """
        rows = self.overrides(season)
        kept = [
            row
            for row in rows
            if (row.espn_player_id, row.created_at) != (espn_player_id, created_at)
        ]
        if len(kept) == len(rows):
            return False
        if replacement.season == season:
            self.save_overrides(season, kept + [replacement])
        else:
            self.save_overrides(season, kept)
            self.save_overrides(
                replacement.season, self.overrides(replacement.season) + [replacement]
            )
        return True

    # -- cash trades ------------------------------------------------------------

    def trades(self, draft_year: int | None = None) -> list[CashTrade]:
        doc = self.load(TRADES)
        out: list[CashTrade] = []
        for year, rows in sorted((doc.get("drafts") or {}).items()):
            if draft_year is not None and int(year) != draft_year:
                continue
            for row in rows:
                trade = CashTrade(**row)
                if trade.draft_year != int(year):
                    raise ValueError(
                        f"trades.json: a row under draft {year} says draft {trade.draft_year}"
                    )
                out.append(trade)
        return out

    def save_trades(self, draft_year: int, trades: list[CashTrade]) -> Path:
        doc = self.load(TRADES)
        drafts = dict(doc.get("drafts") or {})
        rows = sorted(
            (trade.model_dump(mode="json") for trade in trades), key=lambda row: row["id"]
        )
        # Emptied drafts are dropped rather than left as `"2025": []` — see save_overrides.
        if rows:
            drafts[str(draft_year)] = rows
        else:
            drafts.pop(str(draft_year), None)
        return self._merge(TRADES, drafts=drafts)

    def add_trade(self, trade: CashTrade) -> Path:
        return self.save_trades(trade.draft_year, self.trades(trade.draft_year) + [trade])

    def update_trade(self, trade: CashTrade) -> bool:
        """Replace a trade by id, **including when its season changed**.

        Rows are stored under a season key, so an edit that moves a trade from 2025 to 2026 has
        to be removed from the old year as well as written to the new one. Writing only the new
        year would leave the id present twice, and a leg names its trade by id alone — it would
        then be ambiguous which of the two it balanced against.

        Returns whether anything matched, so the caller can say "no such trade" rather than
        reporting a successful write that created a row nobody asked for.
        """
        existing = self.trades()
        if not any(row.id == trade.id for row in existing):
            return False
        touched = {row.draft_year for row in existing if row.id == trade.id} | {trade.draft_year}
        kept = [row for row in existing if row.id != trade.id] + [trade]
        for year in sorted(touched):
            self.save_trades(year, [row for row in kept if row.draft_year == year])
        return True

    def delete_trade(self, trade_id: str) -> bool:
        """Remove a trade entirely. For a row entered in error — nothing else.

        Its legs are **not** touched: an override is a real ESPN edit that happened, and
        deleting the trade it was filed under must not quietly erase the record of the money
        moving. They are left pointing at an id that is now absent, which
        ``check_cash_trades`` reports as a dangling reference rather than passing over.
        """
        existing = self.trades()
        if not any(row.id == trade_id for row in existing):
            return False
        for year in {row.draft_year for row in existing if row.id == trade_id}:
            self.save_trades(
                year, [r for r in existing if r.draft_year == year and r.id != trade_id]
            )
        return True

    def delete_override(
        self, season: int, espn_player_id: int, created_at: datetime
    ) -> bool:
        """Remove an override entirely. For a row entered in error — nothing else.

        Reverting is the tool for one that has served its purpose: a reverted row stays on file
        because it is the explanation for why a base moved, and ``check_base_continuity`` sends
        people looking for it. Deleting is for a row that should never have existed.
        """
        rows = self.overrides(season)
        kept = [
            row
            for row in rows
            if (row.espn_player_id, row.created_at) != (espn_player_id, created_at)
        ]
        if len(kept) == len(rows):
            return False
        self.save_overrides(season, kept)
        return True

    def trade_ids(self) -> set[str]:
        """Every id in use, across every draft — ids are unique league-wide, not per draft.

        A leg names a trade by id alone, so two seasons reusing one id would make that
        reference ambiguous and quietly let a leg balance against the wrong trade.
        """
        return {trade.id for trade in self.trades()}

    # -- season settings --------------------------------------------------------

    def season(self, year: int) -> Season | None:
        row = (self.load(SEASONS).get("seasons") or {}).get(str(year))
        return Season(**row) if row else None

    def seasons(self) -> list[Season]:
        rows = (self.load(SEASONS).get("seasons") or {}).values()
        return sorted((Season(**row) for row in rows), key=lambda season: season.year)

    def save_season(self, season: Season) -> Path:
        doc = self.load(SEASONS)
        seasons = dict(doc.get("seasons") or {})
        seasons[str(season.year)] = season.model_dump(mode="json")
        return self._merge(SEASONS, seasons=seasons)

    def fees_waived_for(self, season: int) -> tuple[str | None, bool]:
        """Who has their fees waived in ``season``, and whether that was actually recorded.

        The waiver belongs to the manager who won the **previous** season's consolation
        bracket. Off by one and it lands on the wrong team for a whole year.

        Returns ``(manager_id, recorded)``. ``recorded`` is False when the prior season has no
        settings row at all — the caller prices with fees ON and says the waiver is
        unconfirmed, because a derived guess must never quietly waive a real team's fees.
        """
        prior = self.season(season - 1)
        if prior is None:
            return None, False
        return prior.consolation_winner_id, True

    # -- payments ---------------------------------------------------------------

    def payments(self, season: int | None = None) -> list[Payment]:
        rows = self.load(PAYMENTS).get("payments") or []
        payments = [Payment(**row) for row in rows]
        if season is None:
            return payments
        return [payment for payment in payments if payment.season == season]

    def set_paid(self, season: int, manager_id: str, paid: bool, *, now: datetime) -> Path:
        """Record (or un-record) that one franchise has been paid out for a season.

        Keyed on the franchise and the season, not on a prize: the money moves once, at the
        end, for everything that franchise won.

        Un-marking **removes** the row rather than storing ``paid: false``, exactly as
        ``set_dues_paid`` does. Absence already means unsettled, so a negative row carries no
        information and would leave a mistyped manager id in the file for good.
        """
        kept = [
            payment
            for payment in self.payments()
            if (payment.season, payment.manager_id) != (season, manager_id)
        ]
        if paid:
            kept.append(
                Payment(season=season, manager_id=manager_id, paid=True, paid_at=now)
            )
        rows = sorted(
            (payment.model_dump(mode="json") for payment in kept),
            key=lambda row: (row["season"], row["manager_id"]),
        )
        return self._merge(PAYMENTS, payments=rows)

    # -- dues -------------------------------------------------------------------

    def dues(self, season: int | None = None) -> list[Dues]:
        rows = self.load(DUES).get("dues") or []
        dues = [Dues(**row) for row in rows]
        if season is None:
            return dues
        return [row for row in dues if row.season == season]

    def set_dues_paid(
        self, season: int, manager_id: str, paid: bool, *, now: datetime
    ) -> Path:
        """Record (or un-record) that one franchise paid its buy-in.

        Un-marking **removes** the row rather than storing ``paid: false``, exactly as
        ``set_paid`` does. Absence already means unpaid — the join reads only rows where
        ``paid`` is true — so a negative row carries no information and would leave a mistyped
        manager id in the file for good.
        """
        kept = [
            row
            for row in self.dues()
            if (row.season, row.manager_id) != (season, manager_id)
        ]
        if paid:
            kept.append(
                Dues(season=season, manager_id=manager_id, paid=True, paid_at=now)
            )
        rows = sorted(
            (row.model_dump(mode="json") for row in kept),
            key=lambda row: (row["season"], row["manager_id"]),
        )
        return self._merge(DUES, dues=rows)

    # -- prize schedule (read-only here; amounts are transcribed by hand) --------

    def prize_schedule(self, season: int) -> PrizeSchedule | None:
        row = (self.load(PAYOUTS).get("seasons") or {}).get(str(season))
        return PrizeSchedule(season=season, **row) if row else None
