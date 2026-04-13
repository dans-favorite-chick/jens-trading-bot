"""
Phoenix Trading Bot — Central Configuration

Edit these values to configure the system. Dashboard sliders override
STRATEGY_DEFAULTS at runtime (session-only unless "Save to Config" is clicked).
"""

# ─── Instrument & Account ───────────────────────────────────────────
INSTRUMENT = "MNQM6 06-26"
ACCOUNT = "Sim101"
LIVE_TRADING = False  # Flip to True for real money (requires ATI enabled in NT8)
TICK_SIZE = 0.25

# ─── Network Ports ──────────────────────────────────────────────────
NT8_WS_PORT = 8765       # Bridge listens, NT8 indicator connects as client
BOT_WS_PORT = 8766       # Bridge listens, bots connect as clients
HEALTH_HTTP_PORT = 8767   # Bridge health endpoint (GET /health)
DASHBOARD_PORT = 5000     # Flask dashboard

# ─── NT8 File Paths (OneDrive — do NOT change) ─────────────────────
OIF_INCOMING = r"C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\incoming"
OIF_OUTGOING = r"C:\Users\Trading PC\OneDrive\Documents\NinjaTrader 8\outgoing"
FILE_FALLBACK_PATH = r"C:\temp\mnq_data.json"

# ─── Connection Thresholds ──────────────────────────────────────────
HEARTBEAT_INTERVAL_S = 3        # NT8 indicator sends heartbeat every N seconds
STALE_THRESHOLD_S = 10          # Yellow warning after N seconds without data
DISCONNECT_THRESHOLD_S = 30     # Red / switch to file fallback after N seconds
FILE_POLL_INTERVAL_S = 0.25     # Poll file fallback every N seconds
TICK_BUFFER_SIZE = 500          # Ring buffer — ~1.5 min of bars on reconnect (2000 overwhelmed event loop)

# ─── Risk Limits ────────────────────────────────────────────────────
MAX_LOSS_PER_TRADE = 20.0       # Hard limit per trade ($)
DAILY_LOSS_LIMIT = 45.0         # Stop trading for the day ($)
WEEKLY_LOSS_LIMIT = 150.0       # Stop trading for the week ($)
RECOVERY_MODE_TRIGGER = 30.0    # At -$30 daily: cut size 50%, raise thresholds
MAX_TRADES_PER_SESSION = 999  # Uncapped — don't limit winning days
COOLOFF_AFTER_CONSECUTIVE_LOSSES = 3   # Pause 5 min after N consecutive losses
COOLOFF_DURATION_MIN = 5

# ─── VIX Thresholds ────────────────────────────────────────────────
VIX_LOW = 15.0       # Below: can be aggressive
VIX_NORMAL = 25.0    # 15-25: standard risk
VIX_HIGH = 30.0      # 30-40: 50% size reduction
VIX_EXTREME = 40.0   # Above: NO TRADE

# ─── Dynamic Risk Sizing (MNQ v5 Elite Upgrade #1) ─────────────────
RISK_TIER_A_PLUS = 15.0   # Entry score 50-60/60: max risk per contract
RISK_TIER_B = 12.0        # Entry score 40-49/60
RISK_TIER_C = 8.0         # Entry score 30-39/60

# ─── Volatility Regime (ATR-based) ─────────────────────────────────
ATR_LOW = 100        # Target 1.5:1, time stop 15min, more trades
ATR_NORMAL = 160     # Target 1.5:1, time stop 12min, standard
ATR_HIGH = 200       # Target 1.75:1, time stop 10min, selective
# Above 200: Target 2.0:1, time stop 8min, A++ only

# ─── Session Windows (CST) ─────────────────────────────────────────
SESSION_WINDOWS = {
    "OVERNIGHT_RANGE":  {"start": "22:00", "end": "07:00"},
    "PREMARKET_DRIFT":  {"start": "07:00", "end": "08:30"},
    "OPEN_MOMENTUM":    {"start": "08:30", "end": "09:30"},  # Best window
    "MID_MORNING":      {"start": "09:30", "end": "11:00"},
    "AFTERNOON_CHOP":   {"start": "11:00", "end": "13:00"},  # Death zone
    "LATE_AFTERNOON":   {"start": "13:00", "end": "15:00"},
    "CLOSE_CHOP":       {"start": "15:00", "end": "16:15"},
    "AFTERHOURS":       {"start": "16:15", "end": "22:00"},
}

# ─── Production Bot Session (when prod_bot trades) ──────────────────
PROD_PRIMARY_START = "08:30"
PROD_PRIMARY_END = "10:00"

# ─── Phase 4: AI Agents ────────────────────────────────────────────
# Requires GEMINI_API_KEY in .env or environment variable
AGENT_COUNCIL_ENABLED = True        # 7-voter bias consensus at session open
AGENT_PRETRADE_FILTER_ENABLED = True  # Fast AI sanity check before entry
AGENT_DEBRIEF_ENABLED = True        # End-of-session coaching debrief
AGENT_MODEL = "gemini-2.5-flash"    # Model for all agents

# ─── Logging ────────────────────────────────────────────────────────
LOG_DIR = "logs"
BRIDGE_LOG = "logs/bridge.log"
TRADES_LOG = "logs/trades.log"
CONNECTION_LOG = "logs/connection.log"
