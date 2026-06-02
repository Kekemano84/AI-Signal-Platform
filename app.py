import os, sqlite3, requests
from datetime import datetime
from functools import wraps
from flask import Flask, request, redirect, session, render_template_string, url_for, flash
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")

DB = "crypto_signal_app.db"
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "your@email.com").lower()

PLANS = {
    "free": {"name": "Free", "daily_limit": 1},
    "basic": {"name": "Basic", "daily_limit": 5},
    "pro": {"name": "Pro", "daily_limit": 20},
    "lifetime": {"name": "Lifetime", "daily_limit": 999},
}

BASE_HTML = """
<!doctype html>
<html>
<head>
<title>AI Crypto Signal App</title>
<style>
body{font-family:Arial;background:#07111f;color:white;margin:0}
nav{background:#0d1b2f;padding:15px;display:flex;gap:15px}
a{color:#4dd4ff;text-decoration:none}
.container{max-width:1000px;margin:30px auto;padding:20px}
.card{background:#101d33;padding:20px;border-radius:12px;margin-bottom:20px}
input,select,button{padding:12px;margin:6px 0;width:100%;border-radius:8px;border:0}
button{background:#16c784;color:white;font-weight:bold;cursor:pointer}
.danger{background:#ff4d4d}
.signal-long{color:#16c784;font-weight:bold}
.signal-short{color:#ff4d4d;font-weight:bold}
.signal-hold{color:#ffd166;font-weight:bold}
.small{color:#aaa;font-size:13px}
table{width:100%;border-collapse:collapse}
td,th{padding:10px;border-bottom:1px solid #26354f}
</style>
</head>
<body>
<nav>
<a href="/">Home</a>
{% if session.get("user_id") %}
<a href="/dashboard">Dashboard</a>
<a href="/settings">Settings</a>
{% if session.get("is_admin") %}<a href="/admin">Admin</a>{% endif %}
<a href="/logout">Logout</a>
{% else %}
<a href="/login">Login</a>
<a href="/register">Register</a>
{% endif %}
</nav>
<div class="container">
{% with messages = get_flashed_messages() %}
{% for m in messages %}<div class="card">{{m}}</div>{% endfor %}
{% endwith %}
{{content|safe}}
</div>
</body>
</html>
"""

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password TEXT,
            plan TEXT DEFAULT 'free',
            discord_webhook TEXT,
            is_admin INTEGER DEFAULT 0,
            created_at TEXT
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS signals(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            market TEXT,
            signal TEXT,
            confidence INTEGER,
            analysis TEXT,
            created_at TEXT
        )
        """)

def render(content):
    return render_template_string(BASE_HTML, content=content, session=session)

def current_user():
    if not session.get("user_id"):
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Admin access only.")
            return redirect("/dashboard")
        return f(*args, **kwargs)
    return wrapper

def today_usage(user_id):
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        r = conn.execute(
            "SELECT COUNT(*) c FROM signals WHERE user_id=? AND created_at LIKE ?",
            (user_id, today + "%")
        ).fetchone()
    return r["c"]

def get_binance_prices(symbol="BTCUSDT", interval="1h", limit=100):
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol.upper(), "interval": interval, "limit": limit}
    r = requests.get(url, params=params, timeout=10)
    data = r.json()
    return [float(candle[4]) for candle in data]


def ema(values, period):
    k = 2 / (period + 1)
    ema_values = [values[0]]
    for price in values[1:]:
        ema_values.append(price * k + ema_values[-1] * (1 - k))
    return ema_values[-1]


def rsi(values, period=14):
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(values):
    ema12 = ema(values, 12)
    ema26 = ema(values, 26)
    return ema12 - ema26


def generate_ai_signal(market):
    symbol = market.upper()

    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"

    try:
        prices = get_binance_prices(symbol)
        current_price = prices[-1]

        rsi_value = round(rsi(prices), 2)
        ema20 = round(ema(prices, 20), 4)
        ema50 = round(ema(prices, 50), 4)
        macd_value = round(macd(prices), 4)

        score = 0
        reasons = []

        if rsi_value < 30:
            score += 2
            reasons.append("RSI is oversold, possible bounce zone.")
        elif rsi_value > 70:
            score -= 2
            reasons.append("RSI is overbought, possible correction risk.")
        else:
            reasons.append("RSI is neutral.")

        if current_price > ema20 > ema50:
            score += 2
            reasons.append("Price is above EMA20 and EMA50, trend looks bullish.")
        elif current_price < ema20 < ema50:
            score -= 2
            reasons.append("Price is below EMA20 and EMA50, trend looks bearish.")
        else:
            reasons.append("EMA trend is mixed.")

        if macd_value > 0:
            score += 1
            reasons.append("MACD is positive.")
        else:
            score -= 1
            reasons.append("MACD is negative.")

        if score >= 2:
            signal = "LONG"
            confidence = min(90, 65 + score * 5)
        elif score <= -2:
            signal = "SHORT"
            confidence = min(90, 65 + abs(score) * 5)
        else:
            signal = "HOLD"
            confidence = 60

        analysis = f"""Market: {symbol}
Current price: {current_price}

Signal: {signal}
Confidence: {confidence}%

Indicators:
RSI: {rsi_value}
EMA20: {ema20}
EMA50: {ema50}
MACD: {macd_value}

AI Analysis:
{chr(10).join(reasons)}

Risk note:
This is not financial advice. Always use your own risk management."""

        return signal, confidence, analysis

    except Exception as e:
        signal = "HOLD"
        confidence = 50
        analysis = f"""Market: {symbol}

Signal: HOLD
Confidence: 50%

AI Analysis:
Could not fetch live Binance market data.

Error:
{str(e)}

Risk note:
This is not financial advice."""
        return signal, confidence, analysis

    if signal == "LONG":
        reason = "Momentum looks positive, market structure is improving, and risk appetite appears stronger."
    elif signal == "SHORT":
        reason = "Market weakness is visible, downside pressure is present, and volatility risk is elevated."
    else:
        reason = "The market is unclear, price action is mixed, and waiting for confirmation is safer."

    return signal, confidence, f"""
Market: {market.upper()}
Signal: {signal}
Confidence: {confidence}%

AI Analysis:
{reason}

Risk note:
This is not financial advice. Always use your own risk management.
"""

def send_discord(webhook, text):
    if not webhook:
        return False
    try:
        r = requests.post(webhook, json={"content": text}, timeout=10)
        return r.status_code in [200, 204]
    except Exception:
        return False

@app.route("/")
def home():
    return render("""
    <div class="card">
    <h1>AI Crypto Signal App</h1>
    <p>Generate AI-based crypto and stock market signals with your own Discord webhook.</p>
    <p><b>Plans:</b> Free, Basic, Pro, Lifetime</p>
    <a href="/register"><button>Start Now</button></a>
    </div>
    """)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        password = request.form["password"]
        is_admin = 1 if email == ADMIN_EMAIL else 0

        try:
            with db() as conn:
                conn.execute(
                    "INSERT INTO users(email,password,is_admin,created_at) VALUES(?,?,?,?)",
                    (email, generate_password_hash(password), is_admin, datetime.now().isoformat())
                )
            flash("Registration successful. Please login.")
            return redirect("/login")
        except sqlite3.IntegrityError:
            flash("Email already registered.")

    return render("""
    <div class="card">
    <h2>Register</h2>
    <form method="post">
    <input name="email" type="email" placeholder="Email" required>
    <input name="password" type="password" placeholder="Password" required>
    <button>Create Account</button>
    </form>
    </div>
    """)

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        password = request.form["password"]

        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        if user and check_password_hash(user["password"], password):
            session["user_id"] = user["id"]
            session["email"] = user["email"]
            session["is_admin"] = bool(user["is_admin"])
            return redirect("/dashboard")

        flash("Invalid login.")

    return render("""
    <div class="card">
    <h2>Login</h2>
    <form method="post">
    <input name="email" type="email" placeholder="Email" required>
    <input name="password" type="password" placeholder="Password" required>
    <button>Login</button>
    </form>
    </div>
    """)

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

@app.route("/dashboard", methods=["GET", "POST"])
@login_required
def dashboard():
    user = current_user()
    plan = PLANS[user["plan"]]
    used = today_usage(user["id"])
    message = ""

    if request.method == "POST":
        market = request.form["market"].upper().strip()

        if used >= plan["daily_limit"]:
            flash("Daily analysis limit reached. Upgrade your plan.")
            return redirect("/dashboard")

        signal, confidence, analysis = generate_ai_signal(market)

        with db() as conn:
            conn.execute(
                "INSERT INTO signals(user_id,market,signal,confidence,analysis,created_at) VALUES(?,?,?,?,?,?)",
                (user["id"], market, signal, confidence, analysis, datetime.now().isoformat())
            )

        discord_text = f"📊 AI Signal\\nMarket: {market}\\nSignal: {signal}\\nConfidence: {confidence}%\\n\\n{analysis}"
        sent = send_discord(user["discord_webhook"], discord_text)

        message = f"<div class='card'><h3>New Signal Generated</h3><pre>{analysis}</pre><p>Discord sent: {'Yes' if sent else 'No / webhook missing'}</p></div>"

    with db() as conn:
        signals = conn.execute(
            "SELECT * FROM signals WHERE user_id=? ORDER BY id DESC LIMIT 20",
            (user["id"],)
        ).fetchall()

    rows = ""
    for s in signals:
        cls = "signal-long" if s["signal"] == "LONG" else "signal-short" if s["signal"] == "SHORT" else "signal-hold"
        rows += f"""
        <tr>
        <td>{s["created_at"][:19]}</td>
        <td>{s["market"]}</td>
        <td class="{cls}">{s["signal"]}</td>
        <td>{s["confidence"]}%</td>
        </tr>
        """

    return render(f"""
    <div class="card">
    <h2>Dashboard</h2>
    <p>Email: {user["email"]}</p>
    <p>Plan: <b>{plan["name"]}</b></p>
    <p>Today usage: {used} / {plan["daily_limit"]}</p>
    </div>

    <div class="card">
    <h3>Generate Analysis</h3>
    <form method="post">
    <input name="market" placeholder="BTC, ETH, SOL, TSLA, NVDA..." required>
    <button>Generate Analysis</button>
    </form>
    </div>

    {message}

    <div class="card">
    <h3>Signal History</h3>
    <table>
    <tr><th>Date</th><th>Market</th><th>Signal</th><th>Confidence</th></tr>
    {rows}
    </table>
    </div>
    """)

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    user = current_user()

    if request.method == "POST":
        webhook = request.form.get("discord_webhook", "").strip()
        with db() as conn:
            conn.execute("UPDATE users SET discord_webhook=? WHERE id=?", (webhook, user["id"]))
        flash("Settings saved.")
        return redirect("/settings")

    return render(f"""
    <div class="card">
    <h2>Settings</h2>
    <form method="post">
    <label>Your Discord Webhook URL</label>
    <input name="discord_webhook" value="{user["discord_webhook"] or ""}" placeholder="https://discord.com/api/webhooks/...">
    <button>Save</button>
    </form>
    <p class="small">Each user can use their own Discord webhook.</p>
    </div>
    """)

@app.route("/pricing")
@login_required
def pricing():
    return render("""
    <div class="card">
    <h2>Plans</h2>
    <p>Stripe payment will be connected here later.</p>
    <ul>
    <li>Free - 1 analysis/day</li>
    <li>Basic - 5 analyses/day</li>
    <li>Pro - 20 analyses/day</li>
    <li>Lifetime - unlimited style access</li>
    </ul>
    </div>
    """)

@app.route("/admin")
@login_required
@admin_required
def admin():
    with db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY id DESC").fetchall()
        total_signals = conn.execute("SELECT COUNT(*) c FROM signals").fetchone()["c"]

    rows = ""
    for u in users:
        rows += f"""
        <tr>
        <td>{u["id"]}</td>
        <td>{u["email"]}</td>
        <td>{u["plan"]}</td>
        <td>{u["created_at"][:19]}</td>
        <td>
        <form method="post" action="/admin/update-plan">
        <input type="hidden" name="user_id" value="{u["id"]}">
        <select name="plan">
        <option value="free">Free</option>
        <option value="basic">Basic</option>
        <option value="pro">Pro</option>
        <option value="lifetime">Lifetime</option>
        </select>
        <button>Update</button>
        </form>
        </td>
        </tr>
        """

    return render(f"""
    <div class="card">
    <h2>Admin Panel</h2>
    <p>Total users: {len(users)}</p>
    <p>Total signals: {total_signals}</p>
    </div>

    <div class="card">
    <h3>Users</h3>
    <table>
    <tr><th>ID</th><th>Email</th><th>Plan</th><th>Created</th><th>Change Plan</th></tr>
    {rows}
    </table>
    </div>
    """)

@app.route("/admin/update-plan", methods=["POST"])
@login_required
@admin_required
def update_plan():
    user_id = request.form["user_id"]
    plan = request.form["plan"]

    if plan not in PLANS:
        flash("Invalid plan.")
        return redirect("/admin")

    with db() as conn:
        conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))

    flash("Plan updated.")
    return redirect("/admin")

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
