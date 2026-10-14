import json
import os
import shutil
import sqlite3
import time

from flask import Flask, Response, g, jsonify, request, send_from_directory

# Percorso del database: punta alla cartella montata del tuo HDD
DB_PATH = os.environ.get("DB_PATH", "/mnt/hdd/sensori/sensori.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

FIELDS = ["temp_aht", "hum", "temp_bmp", "press", "pm1", "pm25", "pm10"]
LED_MODES = {"off", "on", "tx", "threshold", "blink"}
DEFAULT_LED = {"mode": "tx", "metric": "pm25", "cmp": "gt", "threshold": 25.0, "blink_ms": 500}
DEFAULT_INTERVAL = 5

app = Flask(__name__)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS readings ("
        "ts INTEGER NOT NULL, device TEXT, "
        + ", ".join(f"{f} REAL" for f in FIELDS) + ")"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
    conn.execute("CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT)")
    conn.commit()
    conn.close()


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
    db().execute(
        f"INSERT INTO readings(ts, device, {', '.join(FIELDS)}) VALUES (?, ?, {', '.join('?' * len(FIELDS))})",
        [int(time.time()), str(d.get("device", "esp32"))] + vals,
    )
    db().commit()
    return jsonify(config_payload())  # l'ESP32 riceve qui la configurazione LED


@app.get("/api/latest")
def latest():
    row = db().execute("SELECT * FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
    return jsonify({"now": int(time.time()), "reading": dict(row) if row else None})


@app.get("/api/history")
def history():
    minutes = max(1, min(int(request.args.get("minutes", 60)), 60 * 24 * 365))
    since = int(time.time()) - minutes * 60
    bucket = max(1, (minutes * 60) // 400)  # ~400 punti per grafico
    cols = ", ".join(f"AVG({f}) AS {f}" for f in FIELDS)
    rows = db().execute(
        f"SELECT (ts / ?) * ? AS ts, {cols} FROM readings WHERE ts >= ? GROUP BY 1 ORDER BY 1",
        (bucket, bucket, since),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


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


@app.get("/api/stats")
def stats():
    r = db().execute("SELECT COUNT(*) AS c, MIN(ts) AS f FROM readings").fetchone()
    du = shutil.disk_usage(os.path.dirname(DB_PATH))
    return jsonify(rows=r["c"], first=r["f"], db_bytes=os.path.getsize(DB_PATH),
                   disk_free=du.free, disk_total=du.total)


@app.get("/api/export.csv")
def export_csv():
    minutes = max(1, min(int(request.args.get("minutes", 60)), 60 * 24 * 3650))
    since = int(time.time()) - minutes * 60
    cur = db().execute(f"SELECT ts, device, {', '.join(FIELDS)} FROM readings WHERE ts >= ? ORDER BY ts", (since,))

    def gen():
        yield "timestamp,device," + ",".join(FIELDS) + "\n"
        for r in cur:
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))
            yield f"{t},{r['device']}," + ",".join("" if r[f] is None else f"{r[f]:.2f}" for f in FIELDS) + "\n"

    return Response(gen(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sensori.csv"})


@app.get("/")
def index():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")


init_db()

if __name__ == "__main__":
    print(f"Database: {DB_PATH}")
    app.run(host="0.0.0.0", port=5000, threaded=True)
