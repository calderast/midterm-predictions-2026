"""Scoring for the prediction pool.

Every question becomes a probability forecast, so log score and Brier work
the same way for all of them.

Pick-one questions (races, House, ballot measures, "closest race"):
  A pick plus a confidence c puts probability c on the pick and spreads
  1 - c evenly over the other n - 1 options. Confidence runs from 1/n
  (no idea) to 99%.

  Log score:  log2(n * p) / log2(n)     p = probability given to the outcome
    This is the KL divergence from the outcome to the forecast, in bits,
    measured against a know-nothing uniform forecast and scaled so 0 means
    "no better than uniform" and ~1 is a perfect call. For a two-way race it
    reduces to 1 + log2(p): 99% right +0.99, 50% 0, 99% wrong -5.64.

  Brier:  half the multi-class Brier score, sum((p_i - o_i)^2) / 2. For a
    two-way race this is (1 - p)^2: lower is better and a coin flip is 0.25.

Senate seat count (a point estimate):
  It's converted to an implied probability that's 99% for an exact guess and
  halves every SEAT_HALF_LIFE seats off, with a 1% floor. It's then scored
  like a two-way call: exact +0.99, 1 off +0.49, 2 off ~0, 4 off -1.0.
  "Seats off" is (|D - D*| + |R - R*|) / 2, so moving one seat from R to D
  counts as 1.

Vote-share bonus (separate leaderboard):
  For each Senate race you predict the first-listed candidate's share of the
  two-way vote (the D candidate, or Osborn in Nebraska). It's ranked by total
  points off across called races. A race you left unset counts as 50.
  Also reported: lean = mean signed error (+ means you predicted bluer than
  reality) and noise = the standard deviation of your errors around your
  own lean.

Skipped questions count as a uniform forecast (log 0). A skipped seat count
counts as p = 0.5.
"""

import math

MAX_CONFIDENCE = 0.99
MIN_PROB = 0.01
SEAT_HALF_LIFE = 2


def min_confidence(n):
    """Lowest allowed confidence for an n-option question (1/n, rounded up to a whole %)."""
    return math.ceil(100 / n) / 100


def forecast(prediction, options):
    n = len(options)
    if not prediction:
        return {o: 1 / n for o in options}
    c = prediction["confidence"]
    rest = (1 - c) / (n - 1)
    return {o: (c if o == prediction["pick"] else rest) for o in options}


def log_score(p, n=2):
    return math.log2(n * p) / math.log2(n)


def brier(dist, outcome):
    return sum((p - (o == outcome)) ** 2 for o, p in dist.items()) / 2


def seat_error(predicted, actual):
    """Seats off, or None if either side is missing. Both are {"D": int, "R": int}."""
    if not predicted or not actual:
        return None
    return (abs(predicted["D"] - actual["D"]) + abs(predicted["R"] - actual["R"])) / 2


def seat_prob(predicted, actual, half_life=SEAT_HALF_LIFE):
    off = seat_error(predicted, actual)
    if off is None:
        return 0.5
    return max(MIN_PROB, MAX_CONFIDENCE * 0.5 ** (off / half_life))


def score_entry(predictions, results, options_by_race, seats=None, seat_result=None):
    """predictions: {race_id: {pick, confidence}}; results: {race_id: outcome_id};
    options_by_race: {race_id: [option ids]}; seats / seat_result: {"D", "R"} or None."""
    per_race = {}
    for race_id, outcome in results.items():
        options = options_by_race.get(race_id)
        if not options or outcome not in options:
            continue
        dist = forecast(predictions.get(race_id), options)
        p = dist[outcome]
        per_race[race_id] = {"p": p, "score": log_score(p, len(options)), "brier": brier(dist, outcome)}
    if seat_result:
        p = seat_prob(seats, seat_result)
        per_race["SEATS"] = {"p": p, "score": log_score(p), "brier": (1 - p) ** 2}
    n = len(per_race)
    return {
        "total": sum(r["score"] for r in per_race.values()),
        "brier": sum(r["brier"] for r in per_race.values()) / n if n else None,
        "resolved": n,
        "per_race": per_race,
    }


def margin_stats(predicted, actual, race_ids):
    """predicted / actual: {race_id: share 0-100}. Only races with an actual result count."""
    errors = [predicted.get(r, 50.0) - actual[r] for r in race_ids if r in actual]
    if not errors:
        return None
    n = len(errors)
    lean = sum(errors) / n
    return {
        "total_off": sum(abs(e) for e in errors),
        "lean": lean,
        "noise": math.sqrt(sum((e - lean) ** 2 for e in errors) / n),
        "called": n,
    }
