# Open reconciliations — needs the commissioner

Generated from `python -m rs57.validate` on 2026-07-30. Every row below is a **REVIEW** item:
unverified, not wrong. They do not block CI.

Each row is one keeper where **what the record says he owed** and **what ESPN actually charged
at the auction** disagree. The record is reconstructed independently — last season's price, plus
the fee split typed into the `Keepers` workbook, plus the $5 tax — so a disagreement means the
hand-entry into ESPN differs from the declaration, or a draft-cash trade was never recorded.

Under the ratchet a difference does not stay a one-year problem: it becomes the player's base and
is charged again every season after.

| Claim season | Franchise | Player | Record says owed | ESPN charged | Difference | Already known? |
|---|---|---|---|---|---|---|
| 2024 | Bijan's Mustard (`t10`) | Bijan Robinson | $68 | $88 | **+20** | no |
| 2024 | Bijan's Mustard (`t10`) | Michael Pittman Jr. | $15 | $0 | **-15** | no |
| 2024 | Cooking Rice (`t4`) | Kyren Williams | $28 | $23 | **-5** | no |
| 2025 | Too Much Moxie (`t12`) | A.J. Brown | $12 | $25 | **+13** | no |
| 2025 | Bijan's Mustard (`t10`) | Michael Pittman Jr. | $20 | $30 | **+10** | no |
| 2025 | 100yd Reverse Hurdles (`t2`) | Saquon Barkley | $71 | $68 | **-3** | yes — `Manually Changed Salaries` |
| 2025 | Bijan's Mustard (`t10`) | Jonathan Taylor | $32 | $33 | **+1** | yes — `Manually Changed Salaries` |
| 2025 | Jaxian McJigberson (`t3`) | Jaxon Smith-Njigba | $19 | $18 | **-1** | yes — `Manually Changed Salaries` |

## What to check

- **The three "already known" rows** are the workbook's `Manually Changed Salaries` entries
  (Saquon Barkley, Jaxon Smith-Njigba, Jonathan Taylor — all 2025, all still `changed back? FALSE`).
  They have never been recorded as `SalaryOverride` rows anywhere in `data/`, which is why they
  appear here. Entering them in the admin tool's overrides screen clears all three and leaves only
  the genuinely unexplained ones.
- **The rest have no recorded explanation.** Likely unrecorded draft-cash trades — the league group
  chat is the place those were agreed.
- **Michael Pittman Jr. appears twice**, in consecutive seasons. A mispricing that never got
  corrected compounds, so he is worth starting with.

## What is deliberately NOT here

2024's claims for thirteen players carry no `computed_salary` and are **not audited**: 2023 has no
`Fee Allocations` tab, so it is unknown which of its keeps was the PROSPECT, and a prospect keep
sets no $5 tax. Assuming taxed over-priced every one of them by exactly $5 and produced three
false rows in an earlier draft of this table. They are recorded with their real slot and fee and
left unpriced rather than audited against a guess. The affected players are listed in
`data/history/2024.json` under `review.notes`.
