# 2026 Midterm Prediction Pool

A small web app where friends forecast the 2026 midterms: 13 competitive Senate races, House control, the final Senate seat count, a few California/SF questions (CA-11, Prop 40 billionaire tax, SF Prop G), and a bonus: which Senate race will be closest. Each race and House question is a pick plus a confidence from 50 to 99%. Players then compete on a leaderboard once results come in. It uses only the Python standard library, so there's nothing to install.

## Run

```sh
ADMIN_TOKEN=pick-a-secret python3 app.py      # http://127.0.0.1:8000
```

| Env var       | Default             | Purpose                                         |
|---------------|---------------------|-------------------------------------------------|
| `PORT`/`HOST` | `8000`/`127.0.0.1`  | Use `HOST=0.0.0.0` when deploying               |
| `CLOSES_AT`   | from `races.json`   | ISO time with timezone; submissions lock then   |
| `ADMIN_TOKEN` | unset (admin off)   | Needed to enter results                         |
| `DB_PATH`     | `./data.db`         | SQLite file; put it on a persistent disk        |

## Flow

1. Share the site URL. Each person enters a name and their picks, and gets a private edit link (`/?entry=…`) so they can change picks until close.
2. Before close, entrants' names are public and nobody's individual picks are. If `crowd_preview` is on in `races.json` (the default), the page also shows anonymous crowd charts, with a dot per person and the group median but no names, and these update as entries come in. Set it to `false` if you don't want late entrants to see which way the crowd leans.
3. After close, open `/#admin`, enter the admin token, and mark winners as races are called. Final vote shares can go in once the counts settle. The leaderboard updates live.

## Scoring

Every question becomes a probability forecast, so viewers can rank the leaderboard by either rule. The other rule breaks ties. `default_scoring` in `races.json` sets which one shows first.

- **Log score (KL divergence):** `log2(n·p) / log2(n)` per question, where `p` is the probability you gave the outcome and `n` is the number of options. For yes/no questions that's `1 + log2(p)`. A pure guess scores 0, and a perfect call scores about 1. Higher is better.
- **Brier score:** the mean of half the multi-class Brier score, which for yes/no questions is `(1 − p)²`. Lower is better.
- **Closest race:** a 13-way pick. Confidence ranges from 8% (random) to 99%, and the other races split the remainder.
- **Senate seat count:** a point estimate converted to an implied probability of 99% × 0.5^(seats off / 2). An exact guess counts like a 99% correct call, and 2 seats off is about a coin flip.

Skipped questions count as a pure guess. See `scoring.py`.

**Bonus: vote share.** This one has its own tab and its own leaderboard. For each Senate race, a slider sets the D (or Osborn) share of the two-way vote. People are ranked by total points off, and races left unset count as 50. The leaderboard also shows each person's *lean* (average signed miss, where + means they predicted bluer than the result) and *noise* (the spread of their misses around that lean), plotted against each other. Before results come in, both are measured against the group median. Link straight to it with `/#votes`. The page refreshes every minute, so the charts move as results get entered.

## Data and analysis

Everything is stored in SQLite (`data.db`). To export tidy CSVs for pandas, R or Sheets:

```sh
python3 export.py            # writes exports/{entries,picks,seats,vote_shares,results}.csv
```

`picks.csv` has one row per person × question, with each person's probability for the first-listed option plus their log and Brier scores once results are in. `vote_shares.csv` has the signed error, where + means the guess was bluer than the result. Back up `data.db` before deleting or moving anything.

## Editing races

Races, nominees and the seat-count question live in `races.json`. Nominees were last checked on Nov 3, 2026. Edit `name`/`note` freely, but leave the `id` fields alone once people have submitted picks.
