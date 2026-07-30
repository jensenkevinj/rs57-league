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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rs57.history import HistoryStore
from rs57.models import (
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
SEASONS = "seasons.json"
PAYMENTS = "payments.json"
PAYOUTS = "payouts.json"
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
        "keeper_deadline is shown and stamped against submissions, never enforced. A tool that",
        "locks the commissioner out at 5:01pm is a tool that gets worked around by editing this",
        "file by hand.",
    ],
    PAYMENTS: [
        "Which prizes have actually been handed over. Written by the admin tool and by nothing",
        "else.",
        "",
        "Joined to the derived payout rows on (season, label, winner_manager_id). All three",
        "parts are needed: a tie splits one label across two winners, so (season, label) alone",
        "would mark both halves paid when only one had been.",
        "",
        "The AMOUNT is deliberately not here -- it lives in payouts.json and the split is",
        "stats.award_prizes' to compute. Two copies of a number are two numbers.",
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
        seasons[str(season)] = sorted(
            (override.model_dump(mode="json") for override in overrides),
            key=lambda row: (row["espn_player_id"], row["created_at"]),
        )
        return self._merge(OVERRIDES, seasons=seasons)

    def add_override(self, override: SalaryOverride) -> Path:
        return self.save_overrides(
            override.season, self.overrides(override.season) + [override]
        )

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

    def set_paid(
        self, season: int, label: str, winner_manager_id: str, paid: bool, *, now: datetime
    ) -> Path:
        """Record (or un-record) that one prize was handed over.

        Keyed on all three of season, label and winner: a tie splits one label between two
        managers, and marking one paid must not mark the other.

        Un-marking **removes** the row rather than storing ``paid: false``. Absence already means
        unpaid — the join reads only rows where ``paid`` is true — so a negative row carries no
        information and would leave a mistyped winner in the file for good.
        """
        kept = [
            payment
            for payment in self.payments()
            if (payment.season, payment.label, payment.winner_manager_id)
            != (season, label, winner_manager_id)
        ]
        if paid:
            kept.append(
                Payment(
                    season=season,
                    label=label,
                    winner_manager_id=winner_manager_id,
                    paid=True,
                    paid_at=now,
                )
            )
        rows = sorted(
            (payment.model_dump(mode="json") for payment in kept),
            key=lambda row: (row["season"], row["label"], row["winner_manager_id"]),
        )
        return self._merge(PAYMENTS, payments=rows)

    # -- prize schedule (read-only here; amounts are transcribed by hand) --------

    def prize_schedule(self, season: int) -> PrizeSchedule | None:
        row = (self.load(PAYOUTS).get("seasons") or {}).get(str(season))
        return PrizeSchedule(season=season, **row) if row else None
