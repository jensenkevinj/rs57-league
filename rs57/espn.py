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
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

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
)

HOST = "https://lm-api-reads.fantasy.espn.com"
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
"""Below this a team's roster is treated as a degraded response, not a small roster. Real
rosters here run 15-16. A sync that "succeeds" with a short roster would blank a season."""

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

        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
        if self._cookies:
            headers["Cookie"] = self._cookies
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Report the URL, never the headers — the cookie lives in there.
            raise EspnError(f"ESPN returned HTTP {exc.code} for {url}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise EspnError(f"could not reach ESPN for {url}: {exc.reason}") from None
        except json.JSONDecodeError:
            raise EspnError(f"ESPN returned non-JSON for {url}") from None

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
    warnings: tuple[str, ...] = ()
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

    franchises: list[FranchiseName] = []
    players: dict[int, Player] = {}
    roster: list[RosterEntry] = []
    warnings: list[str] = []
    verified_waivers = 0
    mismatched_waivers: list[int] = []

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
        if len(entries) < MIN_ROSTER_SIZE:
            raise EspnError(
                f"team {espn_team_id} returned {len(entries)} roster entries "
                f"(minimum {MIN_ROSTER_SIZE}) — refusing to write a degraded season"
            )

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

            if source is AcquisitionSource.WAIVER and faab_bids is not None:
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
    if faab_bids is None:
        warnings.append(
            "waiver bases were not checked against the FAAB record — pass faab_bids so a "
            "waiver add's salary has a witness"
        )
    if mismatched_waivers:
        warnings.append(
            f"{len(mismatched_waivers)} waiver adds disagree with the FAAB actually bid; "
            f"under the ratchet a wrong waiver base carries forward every season after"
        )
    if not drafted:
        warnings.append(
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
        warnings=tuple(warnings),
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
