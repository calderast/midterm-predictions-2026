#!/usr/bin/env python3
"""Senate prediction pool: a zero-dependency web app (Python stdlib only).

Run:  ADMIN_TOKEN=some-secret python3 app.py
Env:  PORT (8000), HOST (127.0.0.1), DB_PATH (./data.db),
      CLOSES_AT (ISO 8601, overrides races.json), ADMIN_TOKEN (enables result entry)
"""

import json
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from scoring import MAX_CONFIDENCE, margin_stats, min_confidence, score_entry, seat_error

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "races.json").read_text())
for _q in CONFIG["races"]:
    # e.g. "closest race": one option per race in the named group.
    if "options_from" in _q:
        _q["candidates"] = [
            {"id": r["id"], "name": r["state"], "short": r["id"], "color": "X"}
            for r in CONFIG["races"] if r.get("group") == _q["options_from"]
        ]
    _q["min_confidence"] = min_confidence(len(_q["candidates"]))
RACES = {r["id"]: r for r in CONFIG["races"]}
# Each question's on-time deadline is when its polls close. Later changes are saved but marked late.
LOCKS = {r["id"]: datetime.fromisoformat(r.get("locks_at", CONFIG["closes_at"])) for r in CONFIG["races"]}
LOCKS["SEATS"] = datetime.fromisoformat(CONFIG["seats"].get("locks_at", CONFIG["closes_at"]))
OPTIONS = {r["id"]: [c["id"] for c in r["candidates"]] for r in CONFIG["races"]}
MARGIN_CFG = CONFIG["margins"]
MARGIN_RACES = [r["id"] for r in CONFIG["races"] if r.get("group") == MARGIN_CFG["group"]]
TOTAL_SEATS = CONFIG["seats"]["total"]
DB_PATH = os.environ.get("DB_PATH", str(ROOT / "data.db"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN")
CLOSES_AT = datetime.fromisoformat(os.environ.get("CLOSES_AT", CONFIG["closes_at"]))
ON_TIME_UNTIL = datetime.fromisoformat(os.environ.get("ON_TIME_UNTIL", CONFIG.get("on_time_until", CONFIG["closes_at"])))
if CLOSES_AT.tzinfo is None or ON_TIME_UNTIL.tzinfo is None:
    raise SystemExit("closes_at must include a timezone, e.g. 2026-11-04T00:00:00Z")
INDEX_HTML = ROOT / "static" / "index.html"


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def now():
    return datetime.now(timezone.utc)


def is_closed():
    return now() >= CLOSES_AT


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    # On a fresh server volume, start from seed.db (a snapshot of the local data.db) if one was shipped.
    db_file, seed = Path(DB_PATH), ROOT / "seed.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    if not db_file.exists() and seed.exists():
        shutil.copyfile(seed, db_file)
        print(f"Seeded {db_file} from {seed}")
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                name TEXT UNIQUE NOT NULL COLLATE NOCASE,
                predictions TEXT NOT NULL,
                seats TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS results (
                race_id TEXT PRIMARY KEY,
                winner TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS seat_results (
                party TEXT PRIMARY KEY,
                seats INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entry_versions (
                id INTEGER PRIMARY KEY,
                entry_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                predictions TEXT NOT NULL,
                seats TEXT,
                margins TEXT,
                saved_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS margin_results (
                race_id TEXT PRIMARY KEY,
                share REAL NOT NULL
            );
        """)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
        if "margins" not in cols:
            conn.execute("ALTER TABLE entries ADD COLUMN margins TEXT")


def get_results(conn):
    return {r["race_id"]: r["winner"] for r in conn.execute("SELECT race_id, winner FROM results")}


def get_seat_result(conn):
    rows = {r["party"]: r["seats"] for r in conn.execute("SELECT party, seats FROM seat_results")}
    return rows if set(rows) == {"D", "R"} else None


def get_margin_results(conn):
    return {r["race_id"]: r["share"] for r in conn.execute("SELECT race_id, share FROM margin_results")}


# ---------- validation ----------

def clean_name(raw):
    name = " ".join(str(raw or "").split())
    if not 1 <= len(name) <= 40:
        raise ApiError(400, "Name must be 1–40 characters")
    return name


def clean_predictions(raw):
    if not isinstance(raw, dict):
        raise ApiError(400, "predictions must be an object")
    out = {}
    for race_id, pred in raw.items():
        race = RACES.get(race_id)
        if race is None:
            raise ApiError(400, f"Unknown race {race_id!r}")
        if not isinstance(pred, dict):
            raise ApiError(400, f"Bad prediction for {race_id}")
        pick = pred.get("pick")
        if pick not in {c["id"] for c in race["candidates"]}:
            raise ApiError(400, f"Bad pick for {race_id}")
        try:
            conf = round(float(pred.get("confidence")), 2)
        except (TypeError, ValueError):
            raise ApiError(400, f"Bad confidence for {race_id}")
        lo = race["min_confidence"]
        if not lo <= conf <= MAX_CONFIDENCE:
            raise ApiError(400, f"Confidence for {race['state']} must be between {lo:.0%} and {MAX_CONFIDENCE:.0%}")
        out[race_id] = {"pick": pick, "confidence": conf}
    return out


def clean_seats(raw):
    """{"D": int, "R": int} with D + R <= total, or None if not answered."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiError(400, "seats must be an object")
    out = {}
    for party in ("D", "R"):
        v = raw.get(party)
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= TOTAL_SEATS:
            raise ApiError(400, f"Seat counts must be whole numbers from 0 to {TOTAL_SEATS}")
        out[party] = v
    if out["D"] + out["R"] > TOTAL_SEATS:
        raise ApiError(400, f"D + R seats can't exceed {TOTAL_SEATS}")
    return out


def clean_margins(raw):
    """{race_id: first candidate's share of the two-way vote, 0-100}, or None if not answered."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiError(400, "margins must be an object")
    out = {}
    for race_id, v in raw.items():
        if race_id not in MARGIN_RACES:
            raise ApiError(400, f"Unknown race {race_id!r}")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 100:
            raise ApiError(400, f"Vote share for {race_id} must be between 0 and 100")
        out[race_id] = round(float(v), 1)
    return out or None


def late_for(qid, ts):
    return datetime.fromisoformat(ts) >= LOCKS[qid]


def require_open():
    if is_closed():
        raise ApiError(403, "Predictions are closed")


# ---------- handlers ----------

def public_config():
    closed = is_closed()
    with db() as conn:
        results = get_results(conn) if closed else {}
        seat_result = get_seat_result(conn) if closed else None
        margin_results = get_margin_results(conn) if closed else {}
    return {
        "title": CONFIG["title"],
        "closes_at": CLOSES_AT.isoformat(),
        "on_time_until": ON_TIME_UNTIL.isoformat(),
        "closed": closed,
        "races": [{**r, "winner": results.get(r["id"])} for r in CONFIG["races"]],
        "seats": {**CONFIG["seats"], "result": seat_result},
        "default_scoring": CONFIG.get("default_scoring", "log"),
        "margins": {**MARGIN_CFG, "races": MARGIN_RACES, "results": margin_results},
    }


def entry_json(token, name, preds, seats, margins):
    return {"token": token, "name": name, "predictions": preds, "seats": seats, "margins": margins}


def create_entry(body):
    require_open()
    name = clean_name(body.get("name"))
    preds = clean_predictions(body.get("predictions", {}))
    seats = clean_seats(body.get("seats"))
    margins = clean_margins(body.get("margins"))
    token = secrets.token_urlsafe(16)
    ts = now().isoformat()
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO entries (token, name, predictions, seats, margins, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (token, name, json.dumps(preds), json.dumps(seats), json.dumps(margins), ts, ts),
            )
            save_version(conn, cur.lastrowid, name, preds, seats, margins, ts)
    except sqlite3.IntegrityError:
        raise ApiError(409, "That name is taken. If it's you, open your edit link instead.")
    return entry_json(token, name, preds, seats, margins)


def save_version(conn, entry_id, name, preds, seats, margins, ts):
    conn.execute(
        "INSERT INTO entry_versions (entry_id, name, predictions, seats, margins, saved_at) VALUES (?,?,?,?,?,?)",
        (entry_id, name, json.dumps(preds), json.dumps(seats), json.dumps(margins), ts),
    )


def is_late(ts):
    return datetime.fromisoformat(ts) >= ON_TIME_UNTIL


def load_entry(conn, token):
    row = conn.execute("SELECT * FROM entries WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise ApiError(404, "Entry not found")
    return row


def get_entry(token):
    with db() as conn:
        row = load_entry(conn, token)
    return entry_json(token, row["name"], json.loads(row["predictions"]),
                      json.loads(row["seats"] or "null"), json.loads(row["margins"] or "null"))


def update_entry(token, body):
    require_open()
    name = clean_name(body.get("name"))
    preds = clean_predictions(body.get("predictions", {}))
    seats = clean_seats(body.get("seats"))
    margins = clean_margins(body.get("margins"))
    try:
        with db() as conn:
            row = load_entry(conn, token)
            ts = now().isoformat()
            conn.execute(
                "UPDATE entries SET name = ?, predictions = ?, seats = ?, margins = ?, updated_at = ? WHERE token = ?",
                (name, json.dumps(preds), json.dumps(seats), json.dumps(margins), ts, token),
            )
            save_version(conn, row["id"], name, preds, seats, margins, ts)
    except sqlite3.IntegrityError:
        raise ApiError(409, "That name is taken")
    return entry_json(token, name, preds, seats, margins)


def crowd_summary(rows):
    """Anonymous, sorted values only (no names, no per-person linkage across questions),
    so the page can chart the crowd before close without revealing anyone's entry."""
    probs = {r["id"]: [] for r in CONFIG["races"] if len(r["candidates"]) == 2}
    margins = {rid: [] for rid in MARGIN_RACES}
    for row in rows:
        preds = json.loads(row["predictions"])
        for rid, p in preds.items():
            if rid in probs:
                first = RACES[rid]["candidates"][0]["id"]
                probs[rid].append(p["confidence"] if p["pick"] == first else 1 - p["confidence"])
        for rid, v in (json.loads(row["margins"] or "null") or {}).items():
            if rid in margins:
                margins[rid].append(v)
    return {
        "probs": {k: sorted(round(v, 2) for v in vs) for k, vs in probs.items()},
        "margins": {k: sorted(vs) for k, vs in margins.items()},
    }


def leaderboard():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, predictions, seats, margins, created_at, updated_at FROM entries ORDER BY created_at"
        ).fetchall()
        versions = conn.execute("SELECT * FROM entry_versions ORDER BY saved_at, id").fetchall()
        results = get_results(conn)
        seat_result = get_seat_result(conn)
        margin_results = get_margin_results(conn)
    by_entry = {}
    for v in versions:
        by_entry.setdefault(v["entry_id"], []).append(v)

    def snapshots(r):
        """(on-time, latest) picks. On-time = for each question, its value in the last save
        before that question's deadline; latest = the current row."""
        hist = by_entry.get(r["id"]) or [{**dict(r), "saved_at": r["updated_at"]}]
        load = lambda v: (json.loads(v["predictions"]), json.loads(v["seats"] or "null"),
                          json.loads(v["margins"] or "null") or {})
        def as_of(qid):
            ok = [v for v in hist if not late_for(qid, v["saved_at"])]
            return load(ok[-1]) if ok else ({}, None, {})
        cur_p, cur_s, cur_m = load(r)
        on_p, on_m, late = {}, {}, []
        for q in CONFIG["races"]:
            qid = q["id"]
            vp, _, vm = as_of(qid)
            if qid in vp:
                on_p[qid] = vp[qid]
            if vp.get(qid) != cur_p.get(qid):
                late.append(qid)
            if qid in MARGIN_RACES:
                if qid in vm:
                    on_m[qid] = vm[qid]
                if vm.get(qid) != cur_m.get(qid) and qid not in late:
                    late.append(qid)
        _, on_s, _ = as_of("SEATS")
        if on_s != cur_s:
            late.append("SEATS")
        return (on_p, on_s, on_m or None), (cur_p, cur_s, cur_m or None), late

    if not is_closed():
        # Keep everyone's picks private until the pool closes.
        late_names = [r["name"] for r in rows if snapshots(r)[2]]
        out = {"closed": False, "entrants": [r["name"] for r in rows], "late": late_names}
        if CONFIG.get("crowd_preview", True):
            out["crowd"] = crowd_summary(rows)
        return out

    def build(name, snap, late, saved_at):
        preds, seats, margins = snap
        return {
            "name": name, "predictions": preds, "seats": seats, "saved_at": saved_at,
            "late": bool(late), "late_questions": late,
            "seat_error": seat_error(seats, seat_result),
            "margins": margins,
            "margin_stats": margin_stats(margins, margin_results, MARGIN_RACES) if margins else None,
            **score_entry(preds, results, OPTIONS, seats, seat_result),
        }

    latest, on_time = [], []
    for r in rows:
        on, cur, late = snapshots(r)
        on_time.append(build(r["name"], on, [], r["updated_at"]))
        latest.append(build(r["name"], cur, late, r["updated_at"]))
    # Default order is log score with Brier tiebreak; the page can re-sort by Brier.
    key = lambda e: (-e["total"], e["brier"] if e["brier"] is not None else 1, e["name"].lower())
    latest.sort(key=key)
    on_time.sort(key=key)
    return {"closed": True, "results": results, "seat_result": seat_result,
            "margin_results": margin_results, "entries": on_time, "entries_with_late": latest}


def set_results(headers, body):
    if not ADMIN_TOKEN:
        raise ApiError(403, "Admin is disabled (start the server with ADMIN_TOKEN set)")
    auth = headers.get("Authorization", "")
    if not secrets.compare_digest(auth.encode(), f"Bearer {ADMIN_TOKEN}".encode()):
        raise ApiError(401, "Bad admin token")
    if not is_closed():
        raise ApiError(403, "Predictions are still open; results can be entered after close")
    results = body.get("results", {})
    if not isinstance(results, dict):
        raise ApiError(400, "results must be an object")
    with db() as conn:
        if "seats" in body:
            seats = clean_seats(body["seats"])
            conn.execute("DELETE FROM seat_results")
            if seats:
                conn.executemany("INSERT INTO seat_results (party, seats) VALUES (?, ?)", seats.items())
        if "margins" in body:
            margins = body["margins"]
            if not isinstance(margins, dict):
                raise ApiError(400, "margins must be an object")
            for race_id, share in margins.items():
                if share is None:
                    conn.execute("DELETE FROM margin_results WHERE race_id = ?", (race_id,))
                else:
                    share = clean_margins({race_id: share})[race_id]
                    conn.execute("INSERT OR REPLACE INTO margin_results (race_id, share) VALUES (?, ?)", (race_id, share))
        for race_id, winner in results.items():
            race = RACES.get(race_id)
            if race is None:
                raise ApiError(400, f"Unknown race {race_id!r}")
            if winner is None:
                conn.execute("DELETE FROM results WHERE race_id = ?", (race_id,))
            elif winner in {c["id"] for c in race["candidates"]}:
                conn.execute("INSERT OR REPLACE INTO results (race_id, winner) VALUES (?, ?)", (race_id, winner))
            else:
                raise ApiError(400, f"Bad winner for {race_id}")
        return {"results": get_results(conn), "seats": get_seat_result(conn), "margins": get_margin_results(conn)}


class Handler(BaseHTTPRequestHandler):
    server_version = "SenatePool/1.0"

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_PUT(self):
        self.route("PUT")

    def route(self, method):
        path = urlparse(self.path).path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path == "/api/config":
                return self.send_json(200, public_config())
            if method == "GET" and path == "/api/leaderboard":
                return self.send_json(200, leaderboard())
            if method == "POST" and path == "/api/entries":
                return self.send_json(201, create_entry(self.read_json()))
            if path.startswith("/api/entries/"):
                token = path.removeprefix("/api/entries/")
                if method == "GET":
                    return self.send_json(200, get_entry(token))
                if method == "PUT":
                    return self.send_json(200, update_entry(token, self.read_json()))
            if method == "POST" and path == "/api/admin/results":
                return self.send_json(200, set_results(self.headers, self.read_json()))
            raise ApiError(404, "Not found")
        except ApiError as e:
            self.send_json(e.status, {"error": e.message})

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 64 * 1024:
            raise ApiError(413, "Request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "Invalid JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "Expected a JSON object")
        return body

    def send_json(self, status, obj):
        self.send(status, json.dumps(obj).encode(), "application/json")

    def send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    init_db()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"Serving on http://{host}:{port}  (closes {CLOSES_AT.isoformat()}, admin {'on' if ADMIN_TOKEN else 'off'})")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
