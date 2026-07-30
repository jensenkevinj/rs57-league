# RS57 League App

*Rebuilding Since 1957* — a 12-team ESPN keeper/auction fantasy football league.

This app owns the keeper/salary/prize domain that ESPN doesn't model: keeper salaries, fee
allocations, prospect eligibility, and prize tracking. **Live scoring, matchups, and
transactions stay on ESPN.**

## Status

**Phase 3** — the static site and the nightly deploy. Phases 0-2 built the models, the keeper
rules engine, the ESPN pipeline, and the derived stats and prizes. See
[`rs57-league-app-plan.md`](rs57-league-app-plan.md) for the full build plan and phase list.

## Layout

| Path | Written by | Contents |
|---|---|---|
| `data/manual/` | Laptop only | Prize amounts, prospect keeps, salary overrides |
| `data/derived/<year>.json` | Nightly Action only | Rosters, players, franchise names |
| `data/derived/<year>-stats.json` | Nightly Action only | Standings, prizes, payouts |
| `data/history/<year>.json` | Written once, then frozen | Completed seasons |
| `site/` | Nightly Action only | Generated HTML |

**No file has two writers.** That's the rule that keeps the nightly job and the laptop from
colliding. The two derived files per season are separate on purpose: they read different ESPN
views and fail for different reasons, so a broken box score cannot blank a season of salaries.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

Locally, everything runs read-only. The syncs report without writing, and the site renders to
a gitignored scratch directory rather than to `site/`:

```bash
.venv/bin/python -m rs57.sync --year 2026 --dry-run
.venv/bin/python -m rs57.stats_sync --year 2025 --dry-run
.venv/bin/python -m rs57.validate
.venv/bin/python -m rs57.site --preview
```

`rs57/keeper_rules.py` and `rs57/stats.py` are pure — no I/O, no web imports, no ESPN, no
Jinja. They are the tested core, and they stay that way; `tests/test_purity.py` walks their
imports to keep it true.

## The nightly build

[`.github/workflows/nightly.yml`](.github/workflows/nightly.yml) pulls ESPN, writes
`data/derived/`, runs `rs57.validate`, regenerates `site/`, commits, and publishes to GitHub
Pages. It syncs **the current season only** — a completed season is static, so it is populated
once by running the workflow from the Actions tab with a season list, and then left alone.

`site/` is published as an uploaded artifact rather than from a branch. Branch-based Pages can
only serve the repository root or `/docs`, and neither works here: the root would mix generated
HTML in with human-owned files and make a clean rebuild impossible, and `/docs` is where the
generator *reads* `rules.md` from. The artifact leaves `site/` committed and diffable, so each
night's commit still shows exactly what changed on the site.

The first run enables Pages itself. If org policy blocks that, set Settings → Pages → Source:
GitHub Actions once by hand.

**Scheduled Actions are disabled automatically after roughly 60 days without a commit** — which
is what this repo looks like every June and July. Expect to re-enable the nightly each August.
A disabled schedule does not take the site down; it leaves the last deployment up, which looks
identical to a current one.

## Security

This repo and the site it generates are **public**, and so are the Action's logs.

The site publishes franchise names and NFL player names. It publishes **no manager names and
no emails** — there is no mapping from a franchise to a person anywhere in this repo, by
design, and `data/private/` was considered and deliberately not created. Franchises are keyed
on `espn_team_id`.

ESPN needs no credentials: the league is public and every endpoint the pipeline uses answers
unauthenticated. `ESPN_S2`/`SWID` are read from the environment or Actions secrets if present,
purely as insurance, and are never logged.

## License

MIT — see [LICENSE](LICENSE).
