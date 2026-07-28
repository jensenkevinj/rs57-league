# RS57 League App

*Rebuilding Since 1957* — a 12-team ESPN keeper/auction fantasy football league.

This app owns the keeper/salary/prize domain that ESPN doesn't model: keeper salaries, fee
allocations, prospect eligibility, and prize tracking. **Live scoring, matchups, and
transactions stay on ESPN.**

## Status

**Phase 0** — Pydantic models and the keeper rules engine. See
[`rs57-league-app-plan.md`](rs57-league-app-plan.md) for the full build plan and phase list.

## Layout

| Path | Written by | Contents |
|---|---|---|
| `data/manual/` | Laptop only | Salary overrides, keeper claims, payouts, season settings |
| `data/derived/<year>.json` | Nightly Action only | Current-season rosters, scores, transactions |
| `data/history/<year>.json` | Written once, then frozen | Completed seasons |
| `data/private/` | Laptop only, **gitignored** | Manager emails and anything identifying |
| `site/` | Nightly Action only | Generated HTML |

**No file has two writers.** That's the rule that keeps the nightly job and the laptop from
colliding.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

`rs57/keeper_rules.py` is pure — no I/O, no web imports, no ESPN. It's the tested core, and it
stays that way.

## Security

This repo and the site it generates are **public**. Manager emails live only in
`data/private/` (gitignored) and are never imported by the site generator. ESPN credentials come
from the environment or Actions secrets and are never logged — Action logs on a public repo are
public too.

## License

MIT — see [LICENSE](LICENSE).
