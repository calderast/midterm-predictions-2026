#!/usr/bin/env python3
"""Export the pool's SQLite data as tidy CSVs for analysis (pandas, R, Sheets...).

    python3 export.py                 # reads ./data.db, writes ./exports/
    python3 export.py --db path/to/data.db --out some/dir

One row per (person, question) in long format, with scores filled in for
anything that has a result:

  entries.csv      name, created_at, updated_at
  picks.csv        one row per person × pick-one question (Senate, House, CA/SF, closest race)
  seats.csv        each person's Senate seat-count guess and its score
  vote_shares.csv  one row per person × Senate race vote-share guess, with the signed error
  results.csv      every result you've entered
"""

import argparse
import csv
import json
import sqlite3
from pathlib import Path

from scoring import brier, forecast, log_score, seat_error, seat_prob

ROOT = Path(__file__).resolve().parent


def load_config():
    cfg = json.loads((ROOT / "races.json").read_text())
    for q in cfg["races"]:
        if "options_from" in q:
            q["candidates"] = [{"id": r["id"], "name": r["state"]} for r in cfg["races"]
                               if r.get("group") == q["options_from"]]
    return cfg


def write(path, header, rows):
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / "data.db"))
    ap.add_argument("--out", default=str(ROOT / "exports"))
    args = ap.parse_args()

    cfg = load_config()
    races = {r["id"]: r for r in cfg["races"]}
    margin_races = [r["id"] for r in cfg["races"] if r.get("group") == cfg["margins"]["group"]]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    entries = conn.execute("SELECT * FROM entries ORDER BY created_at").fetchall()
    results = {r["race_id"]: r["winner"] for r in conn.execute("SELECT * FROM results")}
    seat_rows = {r["party"]: r["seats"] for r in conn.execute("SELECT * FROM seat_results")}
    seat_result = seat_rows if set(seat_rows) == {"D", "R"} else None
    shares = {r["race_id"]: r["share"] for r in conn.execute("SELECT * FROM margin_results")}

    write(out / "entries.csv", ["name", "created_at", "updated_at"],
          [[e["name"], e["created_at"], e["updated_at"]] for e in entries])

    picks = []
    for e in entries:
        preds = json.loads(e["predictions"])
        for q in cfg["races"]:
            opts = [c["id"] for c in q["candidates"]]
            names = {c["id"]: c["name"] for c in q["candidates"]}
            p = preds.get(q["id"])
            outcome = results.get(q["id"])
            dist = forecast(p, opts)
            picks.append([
                e["name"], q["id"], q.get("group", ""), q["state"], len(opts),
                p["pick"] if p else "", names.get(p["pick"], "") if p else "", p["confidence"] if p else "",
                # probability this person gave the first-listed option (D / Osborn / Yes / Wiener)
                round(dist[opts[0]], 4),
                outcome or "", names.get(outcome, "") if outcome else "",
                round(dist[outcome], 4) if outcome else "",
                round(log_score(dist[outcome], len(opts)), 4) if outcome else "",
                round(brier(dist, outcome), 4) if outcome else "",
            ])
    write(out / "picks.csv",
          ["name", "question_id", "group", "question", "n_options", "pick_id", "pick_name", "confidence",
           "p_first_option", "outcome_id", "outcome_name", "p_outcome", "log_score", "brier"], picks)

    seats = []
    for e in entries:
        s = json.loads(e["seats"] or "null")
        off = seat_error(s, seat_result)
        p = seat_prob(s, seat_result) if seat_result else None
        seats.append([e["name"], s["D"] if s else "", s["R"] if s else "",
                      seat_result["D"] if seat_result else "", seat_result["R"] if seat_result else "",
                      off if off is not None else "",
                      round(log_score(p), 4) if p else "", round((1 - p) ** 2, 4) if p else ""])
    write(out / "seats.csv", ["name", "pred_D", "pred_R", "actual_D", "actual_R", "seats_off", "log_score", "brier"], seats)

    vs = []
    for e in entries:
        m = json.loads(e["margins"] or "null") or {}
        for rid in margin_races:
            r = races[rid]
            pred, act = m.get(rid), shares.get(rid)
            vs.append([e["name"], rid, r["state"], r["candidates"][0]["name"], r["candidates"][1]["name"],
                       pred if pred is not None else "", act if act is not None else "",
                       round(pred - act, 2) if pred is not None and act is not None else ""])
    write(out / "vote_shares.csv",
          ["name", "race_id", "state", "first_candidate", "second_candidate",
           "pred_first_share", "actual_first_share", "error_bluer_positive"], vs)

    res = [[k, "winner", v] for k, v in results.items()]
    res += [[k, "first_candidate_share", v] for k, v in shares.items()]
    if seat_result:
        res += [["SEATS", "D", seat_result["D"]], ["SEATS", "R", seat_result["R"]]]
    write(out / "results.csv", ["question_id", "field", "value"], res)


if __name__ == "__main__":
    main()
