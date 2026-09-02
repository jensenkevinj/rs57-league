"""ESPN read client and the mapping from ESPN's payloads onto ``rs57.models``.

**Impure by design.** This module does the network I/O that ``keeper_rules`` refuses to do.
``keeper_rules`` must never import it; the dependency only ever points this way.

Nothing here applies a league rule. It reads what ESPN says, maps it onto the models, and
reports what it could not vouch for. Pricing is the engine's job.

The whole module rests on one resolved question — which ESPN field is ``base_salary`` — and
the answer is in ``docs/espn-field-semantics.md``. The short version::

    season Y's keeperValue        = the value carried IN from Y-1
    season Y's keeperValueFuture  = the value established IN Y (auction bid / FAAB / $0)

so ``base_salary`` for season Y is ``keeperValueFuture`` once Y has been drafted, and
``keeperValue`` before that. ``draftDetail.drafted`` decides, which is why this needs no
"before or during the season?" toggle — see ``base_salary_field``.

No third-party HTTP dependency. The endpoints below are plain unauthenticated GETs and
``urllib`` serves them fine; ``espn-api`` would add a dependency, and a layer that hides
exactly the keeper fields this pipeline turns on, for no gain. Phase 1 left that decision open
to revisit at Phase 2, on the grounds that box scores are ``espn-api``'s strength — revisited,
and the answer is still no: ``fetch_boxscore`` is nine lines, and the wrapper's ``BoxPlayer``
hides ``statSourceId``, which is the difference between what a player scored and what he was
projected to score. Awarding a stud prize off a projection is exactly the silent-wrong-answer
failure this repo keeps guarding against.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from rs57.keeper_rules import MAX_KEEPERS, MAX_PROSPECTS, charges_in_base
from rs57.models import (
    AcquisitionSource,
    FranchiseName,
    Matchup,
    Player,
    PlayerWeek,
    PlayoffTier,
    Position,
    RosterEntry,
    WeeklyScore,
    utc_now,
)

HOST = "https://lm-api-reads.fantasy.espn.com"
CORE_HOST = "https://sports.core.api.espn.com"
"""ESPN's core API — a different host and a different API from the fantasy one above.

It is here for exactly one field, ``draft.year``, which is the only place ESPN publishes when
a player's NFL career began. The fantasy API does not carry it in any view. Public, no auth,
and read by ``rs57.origins_sync`` alone — never by the keeper sync, so a core-API outage can
never touch a season of salaries."""

LEAGUE_ID = 535631
LEAGUE_SIZE = 12

_USER_AGENT = "rs57-league/0.1 (+https://github.com/rs57-league)"

POSITION_BY_ID: Mapping[int, Position] = {
    1: Position.QB,
    2: Position.RB,
    3: Position.WR,
    4: Position.TE,
    16: Position.DEF,
}
"""ESPN ``defaultPositionId``. The league rosters only these five; an unknown id is schema
drift and raises rather than guessing a position."""

MIN_ROSTER_SIZE = 10
"""A full roster is at least this deep. Real ones here run 14-17.

Not a per-team floor on its own — see ``check_roster_sizes``. Between the keeper deadline and
the auction, ESPN prunes every roster down to the kept players, and a season read in that window
is legitimately four deep."""

KEEPERS_ONLY_ROSTER_SIZE = MAX_KEEPERS + MAX_PROSPECTS
"""The deepest a pruned keeper-window roster can legally be. Read off the rules rather than
typed as a 4, so that raising the keeper limit cannot leave this behind."""

BENCH_SLOT_IDS = frozenset({20, 21})
"""``lineupSlotId`` 20 is the bench and 21 is IR. Everything else is a started slot, including
23 (FLEX), which is why this is a deny-list rather than a list of the starting positions.

The positional stud prize follows the manager who *started* the player, so this set is the
whole basis of that award. ``stats.check_lineup_totals`` is its witness: started points must
sum to the team's score for the week, and they do not if this set is wrong."""

ACTUAL_STAT_SOURCE = 0
"""``statSourceId`` 0 is what a player actually scored; 1 is ESPN's projection. Reading the
projection would quietly award the stud prizes on the strength of a forecast."""

TIER_BY_NAME: Mapping[str, PlayoffTier] = {
    "NONE": PlayoffTier.NONE,
    "WINNERS_BRACKET": PlayoffTier.WINNERS_BRACKET,
    "WINNERS_CONSOLATION_LADDER": PlayoffTier.WINNERS_CONSOLATION_LADDER,
    "LOSERS_CONSOLATION_LADDER": PlayoffTier.LOSERS_CONSOLATION_LADDER,
}


class EspnError(RuntimeError):
    """A fetch failed, or a response was too degraded to write a season from."""


class AthleteNotFound(LookupError):
    """ESPN has no athlete record for an id — a 404, which is an answer, not an outage.

    Kept distinct from ``EspnError`` because the two must never be conflated: a 404 means
    "ESPN does not know this player", which is recorded and moved past, while a 503 means
    "we do not know what ESPN says", which must stop the run before it writes anything.
    Collapsing them would turn an outage into a league-wide "no draft class on record".
    """


def _get_json(url: str, *, timeout: int, cookies: str | None = None) -> Any:
    """GET one URL and parse it as JSON.

    Shared by both clients so the credential guard below exists exactly once. Two copies of
    "report the URL, never the headers" is how a cookie eventually lands in an Actions log on
    a public repo.

    Raises ``AthleteNotFound`` on 404 and ``EspnError`` on anything else.
    """
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    if cookies:
        headers["Cookie"] = cookies
    try:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Report the URL, never the headers — the cookie lives in there.
        if exc.code == 404:
            raise AthleteNotFound(url) from None
        raise EspnError(f"ESPN returned HTTP {exc.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise EspnError(f"could not reach ESPN for {url}: {exc.reason}") from None
    except json.JSONDecodeError:
        raise EspnError(f"ESPN returned non-JSON for {url}") from None


def _epoch_ms(value: int | None) -> datetime | None:
    """ESPN epoch milliseconds to a naive UTC datetime.

    Naive on purpose: the models and fixtures are naive throughout, and mixing an aware
    ``acquired_at`` with a naive ``trade_deadline`` would raise from inside the prospect
    deadline comparison in ``keeper_rules``. Converted through UTC so it is never local time.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None)


@dataclass(frozen=True)
class EspnClient:
    """Read-only ESPN client.

    The league is public (``isPublic: true``, ``restrictionType: NONE``) and every endpoint
    used here answers unauthenticated, so ``espn_s2``/``SWID`` are optional. They are read
    from the environment when present, for the historical ``leagueHistory`` endpoints where
    auth does start to matter.

    Credentials are **never** logged, never included in an exception message, and never put
    in a URL. Action logs on a public repo are public.
    """

    year: int
    league_id: int = LEAGUE_ID
    host: str = HOST
    timeout: int = 30
    _cookies: str | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_env(cls, year: int, **kwargs: Any) -> EspnClient:
        s2, swid = os.environ.get("ESPN_S2"), os.environ.get("SWID")
        cookie = f"espn_s2={s2}; SWID={swid}" if s2 and swid else None
        return cls(year=year, _cookies=cookie, **kwargs)

    @property
    def authenticated(self) -> bool:
        """Whether cookies are in play. Deliberately a boolean — never expose the values."""
        return self._cookies is not None

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.host}{path}"
        if params:
            parts = []
            for key, value in params.items():
                for item in value if isinstance(value, (list, tuple)) else [value]:
                    parts.append(f"{key}={item}")
            url = f"{url}?{'&'.join(parts)}"

        try:
            return _get_json(url, timeout=self.timeout, cookies=self._cookies)
        except AthleteNotFound:
            # On the league endpoints a 404 is a broken path, not a fact worth recording —
            # ``leagueHistory`` answers 404 for exactly that reason. Only the core athlete
            # endpoint treats it as an answer, so it stays an error here, as it always was.
            raise EspnError(f"ESPN returned HTTP 404 for {url}") from None

    def _league(self, **params: Any) -> Any:
        return self._get(
            f"/apis/v3/games/ffl/seasons/{self.year}/segments/0/leagues/{self.league_id}",
            **params,
        )

    def fetch_league(self) -> dict[str, Any]:
        return self._league(view=["mSettings", "mTeam"])

    def fetch_roster(self, team_id: int) -> dict[str, Any]:
        return self._league(forTeamId=team_id, view="mRoster")

    def fetch_draft_detail(self) -> dict[str, Any]:
        return self._league(view="mDraftDetail").get("draftDetail") or {}

    def fetch_pro_teams(self) -> dict[int, str]:
        data = self._get(f"/apis/v3/games/ffl/seasons/{self.year}/", view="proTeamSchedules")
        return {
            team["id"]: team.get("abbrev", "FA")
            for team in data.get("settings", {}).get("proTeams", [])
        }

    def fetch_matchups(self) -> list[dict]:
        """Every matchup in the season, regular and playoff, with both sides' scores."""
        return self._league(view=["mMatchupScore", "mScoreboard"]).get("schedule") or []

    def fetch_boxscore(self, week: int) -> list[dict]:
        """Per-player scoring for one week, with the lineup slot each player filled.

        ``scoringPeriodId`` is load-bearing here for the same reason it is on
        ``mTransactions2``: without it ESPN answers ``200`` and the roster entries come back
        for the wrong week. The response is checked for shape rather than trusted on status.
        """
        payload = self._league(scoringPeriodId=week, view="mBoxscore")
        schedule = payload.get("schedule")
        if schedule is None:
            raise EspnError(f"no schedule in the week {week} boxscore — schema drift")
        return schedule

    def fetch_transactions(self, scoring_periods: Iterable[int] = range(0, 19)) -> list[dict]:
        """Every transaction for the season, de-duplicated.

        ``scoringPeriodId`` is load-bearing and easy to miss: without it ESPN answers ``200``
        and simply omits the ``transactions`` array, which reads exactly like a permissions
        problem and sends you off configuring cookies you do not need. Scoped to a scoring
        period the same request returns the full list unauthenticated.

        A transaction can surface under more than one scoring period, hence the de-dupe on id.
        """
        found: dict[str, dict] = {}
        for period in scoring_periods:
            payload = self._league(scoringPeriodId=period, view="mTransactions2")
            for transaction in payload.get("transactions") or []:
                found[transaction["id"]] = transaction
        return list(found.values())


@dataclass(frozen=True)
class EspnCoreClient:
    """Read-only client for ESPN's **core** API, which is a different host and a different API.

    It exists for one fact the fantasy API does not carry: the season a player's NFL career
    began. The fantasy player object has eighteen keys and none of them is a draft class or a
    rookie flag, and ``view=kona_player_info`` adds nothing on that front either.

    **This client never sends credentials.** ``espn_s2``/``SWID`` are cookies for
    ``lm-api-reads.fantasy.espn.com``; this is a different host, and the endpoint is public.
    Sending them here would leak a credential to gain nothing, so there is no ``from_env`` and
    no cookie parameter — the absence is the guard, and ``test_the_core_client_never_sends_
    credentials`` is its witness.
    """

    timeout: int = 30
    host: str = CORE_HOST

    def athlete_url(self, espn_player_id: int, season: int) -> str:
        return (
            f"{self.host}/v2/sports/football/leagues/nfl/seasons/{season}"
            f"/athletes/{espn_player_id}?lang=en&region=us"
        )

    def fetch_athlete(self, espn_player_id: int, season: int) -> dict[str, Any] | None:
        """One athlete record, or ``None`` when ESPN has no such athlete.

        ``None`` is only ever returned for a 404. Everything else — a timeout, a 503, a
        non-JSON body — raises ``EspnError`` and stops the run, because "we could not ask"
        must never be recorded as "ESPN says he has no draft class".

        D/ST ids are negative and 404 by construction. Callers filter them out beforehand
        rather than collecting a dozen meaningless misses every run.
        """
        try:
            return _get_json(self.athlete_url(espn_player_id, season), timeout=self.timeout)
        except AthleteNotFound:
            return None

    def statistics_log_url(self, espn_player_id: int) -> str:
        return f"{self.host}/v2/sports/football/leagues/nfl/athletes/{espn_player_id}/statisticslog"

    def fetch_first_stats_season(self, espn_player_id: int) -> int | None:
        """The earliest season ESPN has statistics for, or ``None`` if it has none.

        Only ever a **bound** on when a career began, never a statement of it — a player who
        was rostered but recorded nothing appears a season late. Checked against the league:
        it matched the draft class 159 times of 162, and all three misses were late by one.
        Read ``OriginSource`` before using it for anything.

        Same 404 rule as ``fetch_athlete``: no log is an answer, anything else is an outage.
        """
        try:
            payload = _get_json(self.statistics_log_url(espn_player_id), timeout=self.timeout)
        except AthleteNotFound:
            return None
        seasons: list[int] = []
        for entry in (payload or {}).get("entries") or []:
            ref = ((entry.get("season") or {}).get("$ref")) or ""
            match = re.search(r"/seasons/(\d{4})", ref)
            if match:
                seasons.append(int(match.group(1)))
        return min(seasons) if seasons else None


def first_nfl_season(athlete: Mapping[str, Any]) -> tuple[int, str] | None:
    """The season a player's NFL career began, and which field said so.

    ``draft.year`` is the draft class: immutable, authoritative, and present for 162 of the
    league's 174 rostered non-DEF players. ``debutYear`` is the fallback for the undrafted —
    sparser, but where both are present they agreed in all 54 cases checked. Returns ``None``
    when ESPN carries neither, which is recorded as unresolved rather than guessed.

    **``experience.years`` is not read here and must never be.** It is *accrued* seasons, not
    a draft class, and the two come apart exactly where it matters: Jawhar Jordan was drafted
    in 2024 and reports ``experience.years == 1``, so reading it would make a third-year
    player prospect-eligible. It is also unscoped — the same value comes back for
    ``seasons/2023`` through ``seasons/2026`` regardless of the season in the URL — so it
    cannot answer a question about any season but today's. ``test_experience_years_is_never_
    read`` replays Jordan's real payload to keep it that way.
    """
    draft_year = (athlete.get("draft") or {}).get("year")
    if isinstance(draft_year, int):
        return draft_year, "draft_year"
    debut = athlete.get("debutYear")
    if isinstance(debut, int):
        return debut, "debut_year"
    return None


def check_roster_sizes(sizes: Mapping[int, int]) -> str:
    """Refuse a degraded response without refusing a legitimately pruned league.

    The old guard was a per-team floor: fewer than ``MIN_ROSTER_SIZE`` entries and the sync
    raised. That encodes an assumption which is only true for most of the year. Between the
    keeper deadline and the auction, ESPN prunes every roster to the kept players, so a season
    read in that window comes back four deep on all twelve teams and the floor would fail the
    nightly every night until draft day.

    **The tell for a degraded response is disagreement, not size.** A truncated response hits
    one team; a pruned league hits all twelve at once. So the league has to land wholly in one
    of two regimes, and a mix is what raises:

    * ``"full"`` — every team at ``MIN_ROSTER_SIZE`` or deeper. The ordinary season.
    * ``"keepers"`` — every team at ``KEEPERS_ONLY_ROSTER_SIZE`` or shallower, and somebody
      kept somebody. The window between the keeper deadline and the auction.

    Anything else raises: one short team among eleven full ones is the truncation the old guard
    was built for, and a league sitting uniformly between the two regimes is a shape nobody can
    account for, which is not a thing to write a season from.

    An individual team may legitimately be empty in the keeper window — keeping nobody is a
    legal choice — but **all twelve empty is refused**. Nobody in league history has kept
    nothing, and an empty league is exactly what a dead API looks like. Returning "keepers"
    there would blank a season of salaries, which is the failure this whole guard exists for.

    Returns the regime name so the caller can say which one it read.
    """
    counts = sizes.values()
    if all(n >= MIN_ROSTER_SIZE for n in counts):
        return "full"
    if all(n <= KEEPERS_ONLY_ROSTER_SIZE for n in counts) and any(counts):
        return "keepers"

    shape = ", ".join(f"team {tid}: {n}" for tid, n in sorted(sizes.items()))
    raise EspnError(
        f"roster sizes do not describe one league — refusing to write a degraded season. "
        f"A full roster is {MIN_ROSTER_SIZE}+ and a pruned keeper-window roster is "
        f"{KEEPERS_ONLY_ROSTER_SIZE} or fewer; every team has to be in the same regime, "
        f"because a short response hits one team and a pruned league hits all twelve. "
        f"Got {shape}"
    )


def base_salary_field(drafted: bool) -> str:
    """Which ESPN field holds ``base_salary`` for a season, given whether it has drafted.

    The resolution of the old script's ``TODO: Can this be fixed programmatically?`` — yes,
    and this is it. Before a season's auction, nothing has been paid *in* that season and
    ``keeperValueFuture`` is 0 league-wide, so the live number is what carried in from last
    season: ``keeperValue``. Afterwards the season has its own prices in
    ``keeperValueFuture``.

    Verified against the 2025 auction record: of the players where the fields disagree,
    ``keeperValueFuture`` matched the actual ``bidAmount`` 73 times and ``keeperValue`` zero
    times. See ``docs/espn-field-semantics.md``.
    """
    return "keeperValueFuture" if drafted else "keeperValue"


def keeper_pick_ids(draft_detail: Mapping[str, Any]) -> frozenset[int]:
    """Player ids that entered a season's auction as declared keepers.

    ESPN flags these on the draft pick itself, which is how ``kept_prior_year`` gets derived
    from data instead of from the old script's hand-maintained list of names. That list is
    what under-charged James Cook $5 when ESPN started returning ``James Cook III``.

    **This set includes prospects.** ``draftSettings.keeperCount`` is 4 — three keepers plus a
    prospect — and ESPN marks all four the same way, with nothing on the pick to say which slot
    it filled. A prospect keep must not be taxed, so the prospects have to be subtracted from
    this set by the caller; see ``build_season``'s ``prior_prospect_ids``. Taxing them is not a
    hypothetical: it charged Tyjae Spears $5 he did not owe, and the ratchet would have carried
    that forward every season after.
    """
    return frozenset(
        pick["playerId"] for pick in draft_detail.get("picks") or [] if pick.get("keeper")
    )


def winning_bids(transactions: Iterable[Mapping[str, Any]]) -> dict[int, int]:
    """Player id to the FAAB actually paid on the add that put him where he is now.

    Only ``EXECUTED`` transactions count — a losing waiver claim is also recorded, and
    counting it would invent money nobody spent. Where a player was added more than once in a
    season the latest add wins, because that is the one that set his current base.

    Verified across all of 2025: this matches ``keeperValueFuture`` for **all 80** waiver adds
    with no exceptions, which is what confirms the field is money paid rather than a
    projection. Tyrone Tracy Jr.'s $79 is a real bid, not an artefact.
    """
    latest: dict[int, tuple[int, int]] = {}
    for transaction in transactions:
        if transaction.get("status") != "EXECUTED":
            continue
        bid = transaction.get("bidAmount") or 0
        when = transaction.get("proposedDate") or 0
        for item in transaction.get("items") or []:
            if item.get("type") != "ADD" or item.get("playerId") is None:
                continue
            player_id = item["playerId"]
            if player_id not in latest or when >= latest[player_id][1]:
                latest[player_id] = (bid, when)
    return {player_id: bid for player_id, (bid, _) in latest.items()}


def bid_season_for(year: int, drafted: bool) -> int:
    """Which season's transactions explain ``year``'s bases.

    A season's bases come from whichever season actually established them: its own, once it
    has drafted, and otherwise the one before — the same asymmetry as ``base_salary_field``,
    for the same reason.
    """
    return year if drafted else year - 1


def acquisition_source(espn_type: str | None) -> AcquisitionSource:
    """Map ESPN's ``acquisitionType`` onto ``AcquisitionSource``.

    Live data emits only ``DRAFT``, ``ADD`` and ``TRADE`` — the ``WAIVER``/``FAAB`` spellings
    the handoff notes asked about never appear. ``ADD`` covers both, and ESPN does not say
    which on the roster entry, so it maps to ``WAIVER`` as the plainer of the two.

    Nothing in ``keeper_rules`` reads ``source``, so this distinction is descriptive only
    today. It is *not* load-bearing for any salary, and must not become so on this mapping
    alone — the FAAB bid amounts live behind the transactions endpoint, which needs auth.
    """
    match (espn_type or "").upper():
        case "DRAFT":
            return AcquisitionSource.DRAFT
        case "TRADE":
            return AcquisitionSource.TRADE
        case "ADD" | "WAIVER" | "ADD_WAIVER":
            return AcquisitionSource.WAIVER
        case "FAAB":
            return AcquisitionSource.FAAB
        case other:
            raise EspnError(f"unknown ESPN acquisitionType {other!r} — schema drift")


@dataclass(frozen=True)
class SyncedSeason:
    """Everything one season's ESPN read produced, plus what it could not vouch for."""

    season: int
    drafted: bool
    base_field: str
    franchises: tuple[FranchiseName, ...]
    players: tuple[Player, ...]
    roster: tuple[RosterEntry, ...]
    trade_deadline: datetime | None
    draft_date: datetime | None
    """When the auction is scheduled. ESPN's ``draftSettings.date`` — display-only, read fresh
    off the league every sync so the home page can never show a stale hand-typed guess."""
    keeper_deadline: datetime | None
    """ESPN's ``draftSettings.keeperDeadlineDate``. Gates the admin console the same way
    ``trade_deadline`` gates the prospect check — never hand-entered, never overridden."""
    warnings: tuple[str, ...] = ()
    """Things that could not be checked, or were checked and disagreed. Every one names
    something a person can do about it, and every one is REVIEW wherever it is read."""
    phase: tuple[str, ...] = ()
    """What state the season is *in* — pruned rosters, an auction not yet run, a check whose
    premise the calendar has suspended.

    Kept apart from ``warnings`` because they are not the same kind of fact and must not wear
    the same label. Nobody needs to check that a season has not drafted yet: it is the known
    state of every season for most of the year and it clears itself at the auction. Rendering
    it as "nobody has checked this" is how the flags that DO need checking stop being read.
    """
    waiver_bases_verified: int = 0
    """Waiver adds whose base was confirmed against the FAAB actually bid."""
    waiver_base_mismatches: tuple[int, ...] = ()
    """Waiver adds where ESPN's base and the transaction record disagree. Should be empty."""


def _manager_id(espn_team_id: int, managers: Mapping[int, str] | None) -> str:
    """Franchises are keyed on ``espn_team_id``; display names are never an id.

    Without a manager map the id is synthesised from the team id, which is stable across
    seasons in a way team names emphatically are not (one of them carries a double space).
    """
    if managers and espn_team_id in managers:
        return managers[espn_team_id]
    return f"t{espn_team_id}"


def build_season(
    client: EspnClient,
    *,
    prior_keeper_ids: Iterable[int] = (),
    prior_prospect_ids: Iterable[int] | None = None,
    faab_bids: Mapping[int, int] | None = None,
    managers: Mapping[int, str] | None = None,
    now: datetime | None = None,
) -> SyncedSeason:
    """Read one season from ESPN and map it onto models.

    ``prior_keeper_ids`` are the players who entered *last* season's auction as keepers; a
    player still holds the tax this season unless he was dropped in between. A drop shows up
    as an ``ADD`` acquisition — a trade does not, which is exactly the asymmetry ``CLAUDE.md``
    calls out: the tax follows the player across a trade and dies on a drop.

    ``prior_prospect_ids`` are the players last season's keepers list holds who were kept in the
    **PROSPECT** slot. They are subtracted, because a prospect keep never sets the tax flag —
    and ESPN cannot tell you which of its four keeper picks was the prospect. Pass ``None``
    (the default) only when that is genuinely unknown; the season then carries a warning rather
    than quietly taxing players who may owe nothing.

    ``faab_bids`` cross-checks every waiver add's base against the money actually bid for him.
    Drafted players get this for free — ``check_base_continuity`` and the auction record cover
    them — but a waiver base has no other witness, so without it those players are carried as
    unverified rather than assumed correct.

    **That cross-check has a window.** It compares the base ESPN reports against the price the
    player was acquired for, which only means something while ESPN's field still holds an
    acquisition price. Between the keeper deadline and the auction it does not — it holds the
    keeper prices the commissioner has entered, fee and tax inside them — so every waiver add
    carrying a fee "disagrees" with its own FAAB bid by exactly that fee. The check is skipped
    there and says so; ``now`` is only for testing that window.

    Raises ``EspnError`` rather than returning a thin season. A sync that quietly succeeds
    with an empty roster would blank a year of salaries.
    """
    league = client.fetch_league()
    settings = league.get("settings") or {}
    teams = league.get("teams") or []
    if len(teams) != LEAGUE_SIZE:
        raise EspnError(
            f"expected {LEAGUE_SIZE} teams for {client.year}, got {len(teams)} — "
            f"refusing to write a season from a degraded response"
        )

    draft_detail = client.fetch_draft_detail()
    drafted = bool(draft_detail.get("drafted"))
    field_name = base_salary_field(drafted)
    pro_teams = client.fetch_pro_teams()
    # A prospect keep is in ESPN's keeper set but owes no tax, so it comes back out.
    prospects = frozenset(prior_prospect_ids or ())
    prior_keepers = frozenset(prior_keeper_ids) - prospects
    deadline = _epoch_ms((settings.get("tradeSettings") or {}).get("deadlineDate"))
    draft_settings = settings.get("draftSettings") or {}
    draft_date = _epoch_ms(draft_settings.get("date"))
    keeper_deadline = _epoch_ms(draft_settings.get("keeperDeadlineDate"))
    # Decided once, before the roster loop: while ESPN holds the entered keeper prices the
    # FAAB cross-check is comparing a keeper price against an acquisition price, and reports
    # every waiver add carrying a fee as a disagreement with itself.
    entered = charges_in_base(drafted, keeper_deadline, now or utc_now())

    franchises: list[FranchiseName] = []
    players: dict[int, Player] = {}
    roster: list[RosterEntry] = []
    warnings: list[str] = []
    phase: list[str] = []
    verified_waivers = 0
    mismatched_waivers: list[int] = []
    roster_sizes: dict[int, int] = {}

    for team in sorted(teams, key=lambda t: t["id"]):
        espn_team_id = team["id"]
        manager_id = _manager_id(espn_team_id, managers)
        franchises.append(
            FranchiseName(
                manager_id=manager_id,
                season=client.year,
                # Team names arrive with stray whitespace; one has a double space that has
                # already leaked into the spreadsheets. Normalise for display, never key on it.
                name=" ".join((team.get("name") or "").split()),
            )
        )

        payload = client.fetch_roster(espn_team_id)
        entries = ((payload.get("teams") or [{}])[0].get("roster") or {}).get("entries") or []
        # Judged league-wide once every team is in, not here — a pruned keeper window is four
        # deep on all twelve and is not degraded. See check_roster_sizes.
        roster_sizes[espn_team_id] = len(entries)

        for entry in entries:
            pool = entry.get("playerPoolEntry") or {}
            espn_player = pool.get("player") or {}
            player_id = espn_player.get("id")
            if player_id is None:
                raise EspnError(f"roster entry with no player id on team {espn_team_id}")

            position_id = espn_player.get("defaultPositionId")
            if position_id not in POSITION_BY_ID:
                raise EspnError(
                    f"unknown defaultPositionId {position_id!r} for player {player_id} "
                    f"— schema drift"
                )

            players[player_id] = Player(
                espn_player_id=player_id,
                name=espn_player.get("fullName") or f"player {player_id}",
                position=POSITION_BY_ID[position_id],
                nfl_team=pro_teams.get(espn_player.get("proTeamId"), "FA"),
            )

            base = pool.get(field_name)
            if base is None:
                raise EspnError(f"player {player_id} has no {field_name} — schema drift")

            source = acquisition_source(entry.get("acquisitionType"))
            acquired = _epoch_ms(entry.get("acquisitionDate"))
            if acquired is None:
                raise EspnError(f"player {player_id} has no acquisitionDate")

            # The tax survives a trade and dies on a drop. An ADD is the drop's fingerprint:
            # he was kept into last season's auction but is back via the wire, so the base
            # above is already his new waiver value and the tax is gone with it.
            kept_prior = player_id in prior_keepers and source is not AcquisitionSource.WAIVER

            if source is AcquisitionSource.WAIVER and faab_bids is not None and not entered:
                bid = faab_bids.get(player_id)
                if bid is None or bid != base:
                    mismatched_waivers.append(player_id)
                else:
                    verified_waivers += 1

            roster.append(
                RosterEntry(
                    season=client.year,
                    manager_id=manager_id,
                    espn_player_id=player_id,
                    acquired_at=acquired,
                    base_salary=base,
                    kept_prior_year=kept_prior,
                    source=source,
                )
            )

    # Raises on a degraded response. Nothing has been written — the caller does that — so
    # raising here still refuses the season rather than half-writing one.
    regime = check_roster_sizes(roster_sizes)
    if regime == "keepers":
        phase.append(
            f"every roster is {KEEPERS_ONLY_ROSTER_SIZE} players or fewer, so ESPN has pruned "
            f"the league to its keepers: this is the window between the keeper deadline and "
            f"the auction, and the season holds only kept players. Re-sync after the auction."
        )

    if not prior_keepers:
        warnings.append(
            "no prior-season keeper picks supplied, so kept_prior_year is False for every "
            "player and nobody is taxed — pass last season's draft detail"
        )
    elif prior_prospect_ids is None:
        taxed = sum(entry.kept_prior_year for entry in roster)
        warnings.append(
            f"prospect keeps were not supplied, and ESPN's draft flag does not distinguish "
            f"them from keeper slots — up to one player per team among the {taxed} taxed may "
            f"be a prospect owing no $5 tax. Pass prior_prospect_ids from last season's "
            f"keeper claims"
        )
    if faab_bids is None and not entered:
        warnings.append(
            "waiver bases were not checked against the FAAB record — pass faab_bids so a "
            "waiver add's salary has a witness"
        )
    if entered:
        # SKIPPED, never silence — but a phase note, not a warning. The check has not passed
        # here, it has not run, and the reason is a known state with a known end: the auction.
        phase.append(
            "the waiver-base check did not run: between the keeper deadline and the auction "
            "ESPN holds the keeper prices entered for this season, not the price each player "
            "was acquired for, so there is nothing to compare a FAAB bid against. It resumes "
            "once the auction has run"
        )
    elif mismatched_waivers:
        warnings.append(
            f"{len(mismatched_waivers)} waiver adds disagree with the FAAB actually bid; "
            f"under the ratchet a wrong waiver base carries forward every season after"
        )
    if not drafted:
        phase.append(
            f"{client.year} has not been drafted, so base_salary is keeperValue (last "
            f"season's salary carried forward). Re-sync after the auction."
        )

    return SyncedSeason(
        season=client.year,
        drafted=drafted,
        base_field=field_name,
        franchises=tuple(franchises),
        players=tuple(sorted(players.values(), key=lambda p: p.espn_player_id)),
        roster=tuple(sorted(roster, key=lambda r: (r.manager_id, r.espn_player_id))),
        trade_deadline=deadline,
        draft_date=draft_date,
        keeper_deadline=keeper_deadline,
        warnings=tuple(warnings),
        phase=tuple(phase),
        waiver_bases_verified=verified_waivers,
        waiver_base_mismatches=tuple(sorted(mismatched_waivers)),
    )


@dataclass(frozen=True)
class SyncedScoring:
    """One season's scoring side: matchups, weekly totals, and per-player started weeks."""

    season: int
    regular_season_weeks: int
    playoff_team_count: int
    scores: tuple[WeeklyScore, ...]
    matchups: tuple[Matchup, ...]
    player_weeks: tuple[PlayerWeek, ...]
    final_ranks: Mapping[str, int] = field(default_factory=dict)
    playoff_seeds: Mapping[str, int] = field(default_factory=dict)
    espn_points: Mapping[str, float] = field(default_factory=dict)
    """``team.points`` as ESPN reports it — the second witness for the computed standings."""
    warnings: tuple[str, ...] = ()


def _player_points(player: Mapping[str, Any], week: int) -> float:
    """What the player actually scored in ``week``, never what he was projected to score."""
    for stat in player.get("stats") or []:
        if (
            stat.get("scoringPeriodId") == week
            and stat.get("statSourceId") == ACTUAL_STAT_SOURCE
        ):
            return float(stat.get("appliedTotal") or 0.0)
    return 0.0


def build_scoring_season(
    client: EspnClient, *, managers: Mapping[int, str] | None = None
) -> SyncedScoring:
    """Read one season's scoring from ESPN and map it onto models.

    Only **completed** matchups are mapped. ESPN publishes the full 17-week schedule from
    preseason onward with every game ``UNDECIDED`` and every score ``0.0``, and carrying those
    through would hand the weekly high score prize to whoever came first alphabetically at zero
    points. Boxscores are fetched only for weeks that actually have a result, which also keeps
    an offseason sync from making seventeen pointless round trips.

    Applies no rule and awards nothing — ``rs57.stats`` does that, and it does it purely.
    """
    league = client.fetch_league()
    teams = league.get("teams") or []
    if len(teams) != LEAGUE_SIZE:
        raise EspnError(
            f"expected {LEAGUE_SIZE} teams for {client.year}, got {len(teams)} — "
            f"refusing to derive stats from a degraded response"
        )

    settings = league.get("settings") or {}
    regular_weeks = (settings.get("scheduleSettings") or {}).get("matchupPeriodCount")
    if not regular_weeks:
        raise EspnError(
            f"{client.year} has no scheduleSettings.matchupPeriodCount — that number decides "
            f"which weeks the high score, Most Points and Unlucky prizes cover"
        )

    # How many franchises make the playoffs — the rest are the consolation field, and the
    # best-placed of those has their fees waived the following year.
    playoff_team_count = (settings.get("scheduleSettings") or {}).get("playoffTeamCount") or 0

    manager_of = {team["id"]: _manager_id(team["id"], managers) for team in teams}
    warnings: list[str] = []

    matchups: list[Matchup] = []
    scores: list[WeeklyScore] = []
    played_weeks: set[int] = set()
    undecided = 0

    for raw in client.fetch_matchups():
        week = raw.get("matchupPeriodId")
        home, away = raw.get("home"), raw.get("away")
        if week is None or not home:
            continue
        # A playoff bye stays UNDECIDED forever — there is no opponent to beat — but it is a
        # played week with a real score. Distinguishing it from a genuinely unplayed game
        # keeps the top seeds' week 15 out of the "still to come" pile.
        scored_bye = away is None and float(home.get("totalPoints") or 0.0) > 0
        if raw.get("winner") in (None, "UNDECIDED") and not scored_bye:
            undecided += 1
            continue

        tier_name = raw.get("playoffTierType") or "NONE"
        if tier_name not in TIER_BY_NAME:
            raise EspnError(f"unknown playoffTierType {tier_name!r} — schema drift")

        home_id = manager_of[home["teamId"]]
        away_id = manager_of[away["teamId"]] if away else None
        matchups.append(
            Matchup(
                season=client.year,
                week=week,
                tier=TIER_BY_NAME[tier_name],
                home_manager_id=home_id,
                home_points=float(home.get("totalPoints") or 0.0),
                away_manager_id=away_id,
                away_points=float(away.get("totalPoints") or 0.0) if away else None,
            )
        )
        played_weeks.add(week)
        scores.append(
            WeeklyScore(
                season=client.year,
                week=week,
                manager_id=home_id,
                points=float(home.get("totalPoints") or 0.0),
            )
        )
        if away and away_id is not None:
            scores.append(
                WeeklyScore(
                    season=client.year,
                    week=week,
                    manager_id=away_id,
                    points=float(away.get("totalPoints") or 0.0),
                )
            )

    player_weeks: list[PlayerWeek] = []
    unknown_positions: set[int] = set()
    for week in sorted(played_weeks):
        for raw in client.fetch_boxscore(week):
            if raw.get("matchupPeriodId") != week:
                continue
            for side in ("home", "away"):
                team_side = raw.get(side)
                if not team_side:
                    continue
                manager_id = manager_of[team_side["teamId"]]
                roster = team_side.get("rosterForCurrentScoringPeriod") or {}
                for entry in roster.get("entries") or []:
                    pool = entry.get("playerPoolEntry") or {}
                    player = pool.get("player") or {}
                    player_id = player.get("id")
                    position_id = player.get("defaultPositionId")
                    if player_id is None:
                        continue
                    if position_id not in POSITION_BY_ID:
                        # Not fatal: a stud prize covers four positions, and a started player
                        # dropped here shows up as a lineup-total mismatch in stats rather
                        # than vanishing silently.
                        unknown_positions.add(position_id)
                        continue
                    slot = entry.get("lineupSlotId")
                    player_weeks.append(
                        PlayerWeek(
                            season=client.year,
                            week=week,
                            manager_id=manager_id,
                            espn_player_id=player_id,
                            player_name=player.get("fullName") or f"player {player_id}",
                            position=POSITION_BY_ID[position_id],
                            lineup_slot_id=slot,
                            started=slot not in BENCH_SLOT_IDS,
                            points=_player_points(player, week),
                        )
                    )

    if not matchups:
        warnings.append(
            f"{client.year} has no completed matchups yet, so no prize can be awarded. "
            f"{undecided} scheduled games are still UNDECIDED."
        )
    elif len(played_weeks) < regular_weeks:
        warnings.append(
            f"only {len(played_weeks)} of {regular_weeks} regular-season weeks have results; "
            f"the weekly high score prizes for the rest cannot be awarded yet"
        )
    if unknown_positions:
        warnings.append(
            f"skipped players at unmapped defaultPositionId {sorted(unknown_positions)} — if "
            f"any of them started, the lineup totals check will disagree with ESPN's score"
        )

    return SyncedScoring(
        season=client.year,
        regular_season_weeks=regular_weeks,
        playoff_team_count=playoff_team_count,
        scores=tuple(sorted(scores, key=lambda s: (s.week, s.manager_id))),
        matchups=tuple(sorted(matchups, key=lambda m: (m.week, m.home_manager_id))),
        player_weeks=tuple(
            sorted(player_weeks, key=lambda p: (p.week, p.manager_id, p.espn_player_id))
        ),
        final_ranks={
            manager_of[team["id"]]: team["rankCalculatedFinal"]
            for team in teams
            if team.get("rankCalculatedFinal")
        },
        playoff_seeds={
            manager_of[team["id"]]: team["playoffSeed"]
            for team in teams
            if team.get("playoffSeed")
        },
        espn_points={
            manager_of[team["id"]]: float(team["points"])
            for team in teams
            if team.get("points") is not None
        },
        warnings=tuple(warnings),
    )
