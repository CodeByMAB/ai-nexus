# file: /opt/ai/auth/validate_service.py
import os, time, sqlite3
from flask import Flask, request, jsonify, abort

DB_PATH      = os.getenv("DB_PATH", "/opt/ai/auth/keys.sqlite")
PREFIX_LEN   = int(os.getenv("PREFIX_LEN", "24"))   # only store/compare this many chars
BIND_HOST    = os.getenv("BIND_HOST", "0.0.0.0")
BIND_PORT    = int(os.getenv("BIND_PORT", "9090"))

app = Flask(__name__)

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _init():
    with _db() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
              prefix TEXT PRIMARY KEY,
              sd_host TEXT NOT NULL,
              sd_user TEXT,
              sd_pass TEXT,
              sd_auth_header TEXT,
              active INTEGER DEFAULT 1,
              created_at INTEGER
            )
        """)
_init()

def _secret_from_token(token: str) -> str:
    # Accept either "user:pass" or bare tokens. Use the "pass" (secret) part for prefixing.
    t = token.strip()
    if ":" in t:
        t = t.split(":", 1)[1].strip()
    return t

@app.route("/healthz", methods=["GET"])
def health():
    return {"ok": True, "time": int(time.time())}

@app.route("/validate", methods=["POST"])
def validate():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        abort(400, description="invalid json")
    token = (data.get("token") or "").strip()
    if not token:
        abort(401, description="missing token")

    secret = _secret_from_token(token)
    if not secret:
        abort(401, description="invalid token")

    pref = secret[:PREFIX_LEN]
    with _db() as c:
        row = c.execute("SELECT * FROM api_keys WHERE prefix=? AND active=1 LIMIT 1", (pref,)).fetchone()

    if not row:
        abort(401, description="unknown/disabled token prefix")

    sd = {"host": row["sd_host"]}
    if row["sd_auth_header"]:
        sd["auth_header"] = row["sd_auth_header"]
    else:
        if row["sd_user"]:
            sd["user"] = row["sd_user"]
        if row["sd_pass"]:
            sd["pass"] = row["sd_pass"]

    return jsonify({
        "ok": True,
        "client_id": row["prefix"],
        "sd": sd
    })

if __name__ == "__main__":
    app.run(host=BIND_HOST, port=BIND_PORT)
