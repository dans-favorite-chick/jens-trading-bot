"""
Phoenix Trading Bot — Central Configuration

Edit these values to configure the system. Dashboard sliders override
STRATEGY_DEFAULTS at runtime (session-only unless "Save to Config" is clicked).
"""

import os

# ─── Instrument & Account ───────────────────────────────────────────
# NOTE on contract codes: CME quarterly cycle for NQ/MNQ
#   H = March   (3rd Friday)
#   M = June    (3rd Friday)
#   U = September (3rd Friday)
#   Z = December  (3rd Friday)
# Front-month typically rolls ~8 trading days before expiration.
INSTRUMENT = "MNQM6"                 # Match NT8 chart Data Series (rolling front month)
CONTRACT_EXPIRATION = "2026-06-19"   # 3rd Friday of June 2026
NEXT_CONTRACT = "MNQU6 09-26"        # Roll target: September 2026
NEXT_CONTRACT_EXPIRATION = "2026-09-18"  # 3rd Friday of September 2026
ROLL_DAYS_BEFORE_EXPIRATION = 8      # Auto-switch N trading days before expiration

ACCOUNT = "Sim101"
LIVE_TRADING = False  # Flip to True for real money (requires ATI enabled in NT8)
TICK_SIZE = 0.25

# ─── Network Ports ──────────────────────────────────────────────────
NT8_WS_PORT = 8765       # Bridge listens, NT8 indicator connects as client
BOT_WS_PORT = 8766       # Bridge listens, bots connect as clients
HEALTH_HTTP_PORT = 8767   # Bridge health endpoint (GET /health)
DASHBOARD_PORT = 5000     # Flask dashboard

# ─── NT8 File Paths (local Documents, post 2026-04-18 migration) ───
# NT8 data folder was migrated out of OneDrive on 2026-04-18 (via
# OneDrive Settings → Stop backup of Documents → "Only on my PC").
# Change NT8_DATA_ROOT alone to relocate every NT8-dependent path;
# downstream constants derive from it, so nothing else here should
# need to be re-hardcoded.
NT8_DATA_ROOT = r"C:\Users\Trading PC\Documents\NinjaTrader 8"
OIF_INCOMING = os.path.join(NT8_DATA_ROOT, "incoming")
OIF_OUTGOING = os.path.join(NT8_DATA_ROOT, "outgoing")
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
COOLOFF_AFTER_CONSECUTIVE_LOSSES = 2   # Pause after N consecutive losses (was 3)
COOLOFF_DURATION_MIN = 10              # 10 min cooloff (was 5)
MIN_TRADE_SPACING_MIN = 15             # Minimum minutes between any two trades.
                                       # Prevents cluster-trading on marginal setups.
                                       # Yesterday: 28 trades = ~1/10min. Goal: 2-5/day.

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
# Primary window: open momentum + mid-morning (highest edge)
PROD_PRIMARY_START = "08:30"
PROD_PRIMARY_END   = "11:00"   # Extended from 10:00 — MID_MORNING is a gold regime

# Secondary window: institutional repositioning (late afternoon trend trades)
# Today's example: +300pt move ran 13:00–14:30 CST — prod was completely dark
PROD_SECONDARY_START = "13:00"
PROD_SECONDARY_END   = "14:30"

# C/R Adaptive Session: on strong CONTINUATION days (score >= 4), extend secondary
# window to CR_EXTENDED_END to ride institutional trend flow into close
CR_ADAPTIVE_SESSION = True
CR_EXTENDED_END     = "15:00"  # On score 4+ days, trade until 3 PM CST

# ─── Phase 4: AI Agents ────────────────────────────────────────────
# Requires GEMINI_API_KEY in .env or environment variable
AGENT_COUNCIL_ENABLED = True        # 7-voter bias consensus at session open
AGENT_PRETRADE_FILTER_ENABLED = True  # Fast AI sanity check before entry
AGENT_DEBRIEF_ENABLED = True        # End-of-session coaching debrief
AGENT_MODEL = "gemini-2.5-flash"    # Model for all agents

# ─── Commission & Execution ─────────────────────────────────────────
COMMISSION_PER_SIDE = 0.86      # $ per contract per side (NT8 Sim101 / Rithmic rate)
                                 # Derivation: $531.25 gap / 310 trades / 2 sides ≈ $0.855

# Entry order type: "LIMIT" fills at your price (no slippage), "MARKET" fills immediately
ENTRY_ORDER_TYPE = "LIMIT"       # Recommended: LIMIT reduces slippage to ~0
LIMIT_OFFSET_TICKS = 1           # Ticks beyond current price for aggressive fills
                                 # LONG entry: limit = price + (offset * TICK_SIZE)
                                 # SHORT entry: limit = price - (offset * TICK_SIZE)

# ─── Tick Bar Configuration ──────────────────────────────────────────
# Tick bars complete every N trades (not seconds). Used for entry precision.
# Time bars (1m/5m) still drive TF bias and trend direction.
# Tick bars drive entry timing — faster resolution, noise-filtered.
TICK_BAR_SIZE = 300              # Trades per bar. 233=fast, 300=precise, 512=medium
                                 # 300t matches the user's NT8 chart for DOM precision.
                                 # At MNQ open (~500 trades/min): 300t ≈ 36s per bar
                                 # At MNQ lunch (~100 trades/min): 300t ≈ 3 min per bar
TICK_BAR_ENABLED = True          # Disable to fall back to time-only bars

# ─── ATR-Based Stop Loss ────────────────────────────────────────────
# Instead of fixed stop_ticks per strategy, derive stop from current ATR.
# Adapts automatically: wider stops in fast/volatile markets, tighter in slow.
#
# MNQ ATR reference:
#   ATR_1m typically: 4-15 pts (16-60 ticks) — use for responsive stops
#   ATR_tick typically: similar to ATR_1m scaled to 512 tick bars
#   ATR_5m typically: 15-45 pts (60-180 ticks) — use for swing stops
#
# Stop = max(ATR_STOP_MIN_TICKS, min(ATR_STOP_MAX_TICKS, ATR × multiplier / TICK_SIZE))
ATR_STOP_ENABLED     = True       # If False: use strategy's fixed stop_ticks
ATR_STOP_TF          = "5m"       # ATR timeframe: "5m" validated for intraday futures
                                  # Research: 5m ATR > 1m ATR for stop placement
                                  # (1m too noisy — generates whipsaws on MNQ)
                                  # "1m" = more responsive, "tick" = most adaptive
ATR_STOP_MULTIPLIER  = 1.1        # Stop = 1.1 × ATR. Research-validated for futures:
                                  #   1.0 = at the volatility boundary (tight, high WR needed)
                                  #   1.1 = balanced — avoids most noise without wide exposure
                                  #   1.5+ = too wide for intraday; better for swing
                                  # Note: spring_setup overrides this with wick-anchored ATR
ATR_STOP_MIN_TICKS   = 8          # Floor: never less than 8t ($4 risk/contract)
ATR_STOP_MAX_TICKS   = 40         # Ceiling: never more than 40t ($20 risk/contract)

# ─── Scale-Out / Trend Rider ─────────────────────────────────────────
# On CONTINUATION HIGH days (C/R score >= TREND_RIDER_MIN_SCORE):
#   Exit 1 contract at SCALE_OUT_RR, move stop to BE on remaining, ride until stall.
# On normal days: use fixed target for all contracts (no scale-out).
SCALE_OUT_ENABLED = True          # Enable partial exit at first target
SCALE_OUT_RR = 1.5                # Exit contract 1 when this R:R is reached
                                  # Per-signal override: strategies can set
                                  # Signal.scale_out_rr to a research-backed
                                  # multiple (e.g. ORB=1.0 per Zarattini 2024).
TREND_RIDER_ENABLED = True        # Hold remaining contract until trend stalls
TREND_RIDER_MIN_SCORE = 4         # Only ride trend when daily momentum score >= N
                                  # Score 4 = DEVELOPING, Score 5 = INSTITUTIONAL

# ─── MenthorQ Gamma Integration (B14) ──────────────────────────────
# Daily paste of MenthorQ gamma levels feeds the gamma regime classifier
# and entry-wall filter. See docs/MENTHORQ_USAGE.md.
MENTHORQ_GAMMA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "data", "menthorq", "gamma"
)
MENTHORQ_MAX_DATA_AGE_HOURS = 30        # WARN + treat as missing beyond this
MENTHORQ_HVL_BUFFER_TICKS = 8           # Transition-zone band around HVL
MENTHORQ_WALL_BUFFER_TICKS = 8          # Countertrend proximity buffer
MENTHORQ_NO_TRADE_INTO_WALL_TICKS = 12  # Entry-into-wall rejection radius
MENTHORQ_ENABLE_STOP_OVERRIDE = False   # Strategies opt in to gamma stop

# B27 Net GEX regime classification thresholds (absolute Net GEX magnitude).
# |net_gex| > STRONG  → POSITIVE_STRONG / NEGATIVE_STRONG
# |net_gex| > NORMAL  → POSITIVE_NORMAL / NEGATIVE_NORMAL
# |net_gex| <= NORMAL → NEUTRAL
MENTHORQ_NET_GEX_STRONG_THRESHOLD = 3_000_000
MENTHORQ_NET_GEX_NORMAL_THRESHOLD = 500_000

# ─── Logging ────────────────────────────────────────────────────────
LOG_DIR = "logs"
BRIDGE_LOG = "logs/bridge.log"
TRADES_LOG = "logs/trades.log"
CONNECTION_LOG = "logs/connection.log"
