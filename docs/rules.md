# RS57 League Rules

The keeper and prize rules, written for league members. This file is the source for the
site's rules page — edit it here and the nightly build picks it up.

`CLAUDE.md` in this repo says the same things in the form the code needs them. Where the two
disagree, `CLAUDE.md` is the one the engine follows and this file is the one that needs fixing.

## Keepers

You may keep up to **three keepers plus one prospect**. Nobody is required to keep anyone.

There is **no salary cap**. Keep whoever you can afford to keep; the only limit is the count.

### What a keeper costs

A keeper's salary is three things added together:

`salary = base + your allocated fee + $5 if you kept him last year`

**Base** is what the player cost you *this* season — not what you originally paid for him
years ago. Keepers go back into the auction at their keeper price, so ESPN's per-season value
already carries every fee and tax you have ever paid on that player. Next season's base is
this season's salary.

Puka Nacua is the clearest example. Picked up off waivers for $0. Kept for $0 the following
season, because a first keep is untaxed. Kept for $5 the season after that, once the tax
applied. He goes for $10 next season.

That compounding is the point of the system, and it is why a player you have held for four
years is expensive even though you got him for nothing.

### The keeper fee

The fee depends on **how many keepers you declare**, not on who they are:

- 1 keeper — **$0**
- 2 keepers — **$5** total
- 3 keepers — **$15** total

The total is for your whole keeper group, and you split it across your keepers however you
like. Put all $15 on one player or spread it evenly; the total is what matters.

Prospects never carry a fee and never count toward the keeper count.

### The $5 tax

If you kept a player last season, he costs $5 more this season. Three things about it catch
people out:

- **Trading a player does not clear the tax.** It follows him to his new team, because it is a
  fact about the player's history and not about who currently holds him.
- **Dropping him does clear it** — completely. If you drop a player and pick him back up, the
  tax is gone *and* his base resets to whatever you paid to re-acquire him.
- **A prospect keep never sets the tax.** Keep someone in the prospect slot and he starts the
  next season untaxed.

### Prospects

A prospect must meet both of these:

- **He must be a rookie.**
- **Rostered before the trade deadline** — the site calls this the *keeper deadline*, since it
  is the date by which you must already hold a player to keep him. It is the same date.

He is kept at his acquisition value, with **no fee** and **no tax**.

A player can only ever be kept as a prospect once — he has one rookie season, so the second
claim is never legal. After that he is an ordinary keeper.

**Prospects may be started.** The old rule that a prospect must never have been started by any
team in the league was dropped, and so was the allowance for second-year players. Both changes
are already in force; this page was simply behind.

**The keeper page marks who is eligible.** "Rookie" means his first NFL season was the one just
finished, taken from ESPN's draft class, and the mark covers all three rules — rookie, rostered
before the deadline, and never prospected before.

It shows as a small **P** beside the player's name: filled blue for eligible, dashed amber for a
player ESPN has no first season for at all. No badge means checked and not eligible. Team
defences never get one — a D/ST has no rookie season and can never be a prospect. Hover it for
the reason, or tick **Prospects only** to see nothing else.

**Every row shows when that manager acquired the player**, and the keeper deadline it is measured
against is in red above the grid. A player picked up *after* that deadline is shown in red too —
his date and his whole row, and both are greyed so they recede whether or not you can see the
colour.

The grid arrives sorted dearest first, so the expensive end of the league reads at a glance.

Note what the red mark is and is not: being rostered before the deadline is the **prospect**
rule's third test. Whether it also bars an ordinary keeper is a separate question, and nothing
here decides it.

### The consolation bracket winner

The consolation bracket winner has their **keeper fees waived for one year**. Salaries are
still owed in full — it is the fee on top that goes away, not the price of the players.

The winner is the **top finisher among the teams that missed the playoffs**. It is not
whoever won the last game of the consolation ladder, and it is not whoever went undefeated in
it; in most seasons those questions have several answers and this one has exactly one.

## Prizes

Prize amounts are set per season, so the figures on the site are the ones actually recorded
for that season rather than fixed constants.

- **Champion, 2nd, 3rd** — final standings.
- **Most Points (Season)** — most total points across the **regular season**, weeks 1-14.
- **Weekly High Score** — top score of the week, for each of the **14 regular-season weeks**.
  Playoff weeks do not pay a weekly high score.
- **Positional Stud** — the single best week by a **started** player at each of QB, RB, WR and
  TE, across the **whole season including the playoff weeks**. It is one great week, not a
  season total, and a player who put it up on your bench wins nothing. Defenses do not
  compete for it.
- **Survivor** — the lowest score among the teams still alive is eliminated each week. Last
  team standing wins.
- **Unlucky** — the **single highest score in the regular season that still lost its
  matchup**, awarded once for the whole season. A tie is not a loss.

If two teams tie for a prize, the money is **split evenly** between them. Prizes are whole
dollars, so a split that does not divide evenly pays the floor and the remainder is left for
the commissioner to place.

## How the site gets its numbers

Everything on this site is read from ESPN each night and recomputed from the rules above.
Nothing is typed in by hand except the prize amounts for each season and the record of which
players were kept in a prospect slot, neither of which ESPN stores.

Teams are tracked by their ESPN team id, not by their team name — names change every year.

Where a number could not be verified, the site says so rather than showing it as though it
had been checked.
