import json
import os
import shutil
import sqlite3
import time

from flask import Flask, Response, g, jsonify, request, send_from_directory

try:  # optional: pip install flask-compress  (gzip for JSON/HTML/CSV)
    from flask_compress import Compress
except ImportError:
    Compress = None

# Percorso del database: punta alla cartella montata del tuo HDD
DB_PATH = os.environ.get("DB_PATH", "/mnt/hdd/sensori/sensori.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

FIELDS = ["temp_aht", "hum", "temp_bmp", "press", "pm1", "pm25", "pm10"]
LED_MODES = {"off", "on", "tx", "threshold", "blink"}
DEFAULT_LED = {"mode": "tx", "metric": "pm25", "cmp": "gt", "threshold": 25.0, "blink_ms": 500}
DEFAULT_INTERVAL = 5

# Pre-aggregated buckets (seconds). Charts read these instead of scanning raw rows.
RES = (60, 300, 1800, 7200, 43200)
MAX_POINTS = 500      # max points returned per chart
RAW_MAX_MIN = 60      # up to this range, serve raw readings

app = Flask(__name__)
if Compress:
    Compress(app)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")   # with WAL: no fsync on every commit (big win on HDD)
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-20000")    # ~20 MB page cache
    return conn


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = connect()
    conn.execute("PRAGMA journal_mode=WAL")     # persistent; readers no longer block the writer
    conn.execute(
        "CREATE TABLE IF NOT EXISTS readings ("
        "ts INTEGER NOT NULL, device TEXT, "
        + ", ".join(f"{f} REAL" for f in FIELDS) + ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")

    # rollup: per bucket we keep sum + count for each field (so NULLs are handled correctly)
    cols = ", ".join(f"s_{f} REAL NOT NULL DEFAULT 0, c_{f} INTEGER NOT NULL DEFAULT 0" for f in FIELDS)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS rollup (res INTEGER NOT NULL, ts INTEGER NOT NULL, {cols}, "
        "PRIMARY KEY(res, ts)) WITHOUT ROWID"
    )

    # one-time backfill of existing data
    has_rollup = conn.execute("SELECT 1 FROM rollup LIMIT 1").fetchone()
    has_rows = conn.execute("SELECT 1 FROM readings LIMIT 1").fetchone()
    if has_rows and not has_rollup:
        print("Building rollup tables from existing readings (one time only)...")
        sel = ", ".join(f"COALESCE(SUM({f}), 0), COUNT({f})" for f in FIELDS)
        for res in RES:
            conn.execute(
                f"INSERT INTO rollup SELECT ?, (ts / ?) * ?, {sel} FROM readings GROUP BY 2",
                (res, res, res),
            )
    conn.commit()
    conn.close()


UPSERT = (
    f"INSERT INTO rollup(res, ts, {', '.join(f's_{f}, c_{f}' for f in FIELDS)}) "
    f"VALUES ({', '.join(['?'] * (2 + 2 * len(FIELDS)))}) "
    "ON CONFLICT(res, ts) DO UPDATE SET "
    + ", ".join(f"s_{f}=s_{f}+excluded.s_{f}, c_{f}=c_{f}+excluded.c_{f}" for f in FIELDS)
)


def get_setting(key, default):
    row = db().execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_setting(key, value):
    db().execute("INSERT OR REPLACE INTO settings(k, v) VALUES (?, ?)", (key, json.dumps(value)))
    db().commit()


def config_payload():
    return {"led": get_setting("led", DEFAULT_LED), "interval": get_setting("interval", DEFAULT_INTERVAL)}


def num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


@app.post("/api/data")
def post_data():
    d = request.get_json(force=True, silent=True) or {}
    vals = [num(d.get(f)) for f in FIELDS]
    now = int(time.time())
    conn = db()
    conn.execute(
        f"INSERT INTO readings(ts, device, {', '.join(FIELDS)}) VALUES (?, ?, {', '.join('?' * len(FIELDS))})",
        [now, str(d.get("device", "esp32"))] + vals,
    )
    sc = []
    for v in vals:
        sc += [0 if v is None else v, 0 if v is None else 1]
    for res in RES:
        conn.execute(UPSERT, [res, now // res * res] + sc)
    conn.commit()  # single commit for raw row + all rollups
    return jsonify(config_payload())  # l'ESP32 riceve qui la configurazione LED


@app.get("/api/latest")
def latest():
    row = db().execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    return jsonify({"now": int(time.time()), "reading": dict(row) if row else None})


@app.get("/api/history")
def history():
    """Columnar response: {"ts":[...], "f":{"temp_aht":[...], ...}} -> far smaller than row objects."""
    minutes = max(1, min(int(request.args.get("minutes", 60)), 60 * 24 * 365))
    fields = [f for f in request.args.get("fields", "").split(",") if f in FIELDS] or FIELDS
    now = int(time.time())
    since = now - minutes * 60

    if minutes <= RAW_MAX_MIN:
        rows = db().execute(
            f"SELECT ts, {', '.join(fields)} FROM readings WHERE ts >= ? ORDER BY ts", (since,)
        ).fetchall()
    else:
        res = next((r for r in RES if minutes * 60 / r <= MAX_POINTS), RES[-1])
        cols = ", ".join(f"CASE WHEN c_{f} > 0 THEN s_{f} / c_{f} END" for f in fields)
        rows = db().execute(
            f"SELECT ts, {cols} FROM rollup WHERE res = ? AND ts >= ? ORDER BY ts",
            (res, since // res * res),
        ).fetchall()

    columns = list(zip(*rows)) if rows else [()] * (len(fields) + 1)
    out = {
        "ts": list(columns[0]),
        "f": {f: [None if v is None else round(v, 2) for v in columns[i + 1]] for i, f in enumerate(fields)},
    }
    resp = jsonify(out)
    resp.headers["Cache-Control"] = "private, max-age=5"
    return resp


@app.get("/api/led")
def led_get():
    return jsonify(config_payload())


@app.post("/api/led")
def led_set():
    d = request.get_json(force=True, silent=True) or {}
    led = dict(get_setting("led", DEFAULT_LED))
    if d.get("mode") in LED_MODES:
        led["mode"] = d["mode"]
    if d.get("metric") in FIELDS:
        led["metric"] = d["metric"]
    if d.get("cmp") in ("gt", "lt"):
        led["cmp"] = d["cmp"]
    if num(d.get("threshold")) is not None:
        led["threshold"] = num(d["threshold"])
    if num(d.get("blink_ms")) is not None:
        led["blink_ms"] = int(max(100, min(num(d["blink_ms"]), 5000)))
    set_setting("led", led)
    if num(d.get("interval")) is not None:
        set_setting("interval", int(max(2, min(num(d["interval"]), 300))))
    return jsonify(config_payload())


_stats_cache = {"t": 0.0, "rows": 0, "first": None}


@app.get("/api/stats")
def stats():
    # COUNT(*) scans the whole table: refresh it at most every 5 minutes
    if time.time() - _stats_cache["t"] > 300:
        r = db().execute("SELECT COUNT(*) AS c, MIN(ts) AS f FROM readings").fetchone()
        _stats_cache.update(t=time.time(), rows=r["c"], first=r["f"])
    size = os.path.getsize(DB_PATH)
    for ext in ("-wal",):
        p = DB_PATH + ext
        if os.path.exists(p):
            size += os.path.getsize(p)
    du = shutil.disk_usage(os.path.dirname(DB_PATH))
    return jsonify(rows=_stats_cache["rows"], first=_stats_cache["first"], db_bytes=size,
                   disk_free=du.free, disk_total=du.total)


@app.get("/api/export.csv")
def export_csv():
    minutes = max(1, min(int(request.args.get("minutes", 60)), 60 * 24 * 3650))
    since = int(time.time()) - minutes * 60

    def gen():
        conn = connect()
        try:
            cur = conn.execute(
                f"SELECT ts, device, {', '.join(FIELDS)} FROM readings WHERE ts >= ? ORDER BY ts",
                (since,),
            )
            yield "timestamp,device," + ",".join(FIELDS) + "\n"
            for r in cur:
                t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
                yield f"{t},{r['device']}," + ",".join(
                    "" if r[f] is None else f"{r[f]:.2f}" for f in FIELDS
                ) + "\n"
        finally:
            conn.close()

    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sensori.csv"})


@app.get("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


init_db()

if __name__ == "__main__":
    print(f"Database: {DB_PATH}")
    try:
        from waitress import serve  # pip install waitress: HTTP keep-alive, much faster than Flask dev server
    except ImportError:
        app.run(host="0.0.0.0", port=5000, threaded=True)
    else:
        serve(app, host="0.0.0.0", port=5000, threads=8)
