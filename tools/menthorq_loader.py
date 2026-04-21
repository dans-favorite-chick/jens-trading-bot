"""
Phoenix Bot — Menthor Q Knowledge Loader

Loads comprehensive Menthor Q expertise into the RAG knowledge base.
This makes every AI agent (council, pre-trade filter, debriefer) a
Menthor Q specialist — they understand GEX, HVL, DEX, vanna/charm,
0DTE flows, GEX levels, CTA positioning, and how to apply them to
intraday NQ/MNQ trading.

Run once to seed the knowledge base:
    python -m tools.menthorq_loader

CLI options:
    --stats     Show how many MQ entries are loaded
    --reset     Delete all MQ entries and re-seed
    --query "..."  Test a knowledge query
"""

import argparse
import logging
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("MenthorQLoader")

# ── Menthor Q Knowledge Catalog ───────────────────────────────────────
# Each entry is a rule, pattern, or concept that the AI agents
# will retrieve contextually based on current market conditions.

MENTHORQ_KNOWLEDGE: list[dict] = [

    # ══════════════════════════════════════════════════════════════════
    # CORE CONCEPTS
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_what_is_gex",
        "title": "What is GEX (Gamma Exposure)",
        "category": "core_concept",
        "content": (
            "GEX (Gamma Exposure) measures the net gamma position held by market makers/dealers across "
            "all options strikes and expirations. Calculated as call gamma minus put gamma in dollar terms. "
            "Because dealers take the opposite side of customer trades, customer net long gamma = dealer net short gamma. "
            "GEX tells you HOW dealers will hedge, which creates predictable price behavior. "
            "Key rule: dealers ALWAYS hedge their gamma exposure, creating mechanical, predictable flows. "
            "Positive GEX (green) = dealers long gamma = they SELL into rallies, BUY into drops = price-suppressing. "
            "Negative GEX (red) = dealers short gamma = they BUY into rallies, SELL into drops = price-amplifying. "
            "The larger the absolute GEX value, the stronger the mechanical hedging pressure."
        ),
        "tags": ["gex", "gamma", "dealers", "hedging", "core"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ", "ES", "SPX", "QQQ"],
    },

    {
        "id": "mq_positive_gex_regime",
        "title": "Positive GEX Regime — Mean Reversion Environment",
        "category": "regime",
        "content": (
            "POSITIVE GEX regime: Net dealer gamma is positive (green bars). "
            "Dealers are long gamma. They hedge by SELLING into rallies and BUYING into dips. "
            "This creates a mechanical ceiling and floor — price is suppressed and mean-reverts. "
            "Trading rules in positive GEX: "
            "(1) Fade moves to GEX resistance levels — they are real ceilings. "
            "(2) Buy dips to GEX support levels — dealers provide a mechanical bid. "
            "(3) Use TIGHTER stops — moves are suppressed, large stops waste risk. "
            "(4) Prefer mean-reversion strategies: VWAP bounces, spring setups, range fades. "
            "(5) Breakouts are suspect — they tend to fail and revert. "
            "(6) The market feels 'sticky' — price hangs around key levels. "
            "Session P&L tends to be smoother. Good for option sellers. "
            "Do NOT trade momentum breakouts aggressively in positive GEX — they fail repeatedly."
        ),
        "tags": ["positive_gex", "mean_reversion", "regime", "fade", "range"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING", "AFTERNOON_CHOP"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_negative_gex_regime",
        "title": "Negative GEX Regime — Momentum / Trend-Following Environment",
        "category": "regime",
        "content": (
            "NEGATIVE GEX regime: Net dealer gamma is negative (red bars). "
            "Dealers are short gamma. They hedge by BUYING into rallies and SELLING into drops. "
            "This AMPLIFIES moves — dealers act as accelerants, not stabilizers. "
            "Trading rules in negative GEX: "
            "(1) Follow momentum. Do NOT fade moves — dealers will run you over. "
            "(2) Widen stops by 1.5x minimum — moves are larger and faster than usual. "
            "(3) Prefer breakout/momentum strategies: bias momentum, IB breakout, trend-follow. "
            "(4) GEX levels that break become accelerators, not just S/R. "
            "(5) Expect larger daily ranges — this is a high-octane environment. "
            "(6) LONGs: Only when above HVL or when strong bullish catalyst exists. "
            "(7) SHORTs: Trade freely when below HVL — dealers amplify the selloff. "
            "The market feels 'dangerous' — gaps, fast moves, stops getting blown through. "
            "In deeply negative GEX (e.g., -$5B+), treat every trade as high-volatility. "
            "Critical: never fade a strong directional move in negative GEX — you will get destroyed."
        ),
        "tags": ["negative_gex", "momentum", "trend", "regime", "amplification"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_hvl_the_flip",
        "title": "HVL (High Vol Level) — The Gamma Flip Price",
        "category": "core_concept",
        "content": (
            "The HVL (High Vol Level) is THE most important single Menthor Q number. "
            "It is the exact price where net dealer gamma flips from positive to negative. "
            "Above HVL = positive gamma regime: dealers suppress volatility, fade moves. "
            "Below HVL = negative gamma regime: dealers amplify volatility, follow trend. "
            "Trading rules around HVL: "
            "(1) Price above HVL → mean-reversion strategies. Tight stops. "
            "(2) Price below HVL → momentum strategies. Wide stops. No fading. "
            "(3) HVL itself acts as strong S/R — expect sharp reactions when tested. "
            "(4) A RECLAIM of HVL from below is a high-conviction LONG setup: "
            "    price moves from negative gamma zone back into positive gamma zone, "
            "    dealers flip from sellers to buyers, strong mechanical bid appears. "
            "(5) A LOSS of HVL from above is a high-conviction SHORT setup: "
            "    regime flips to negative gamma, dealers become sellers, amplified drop follows. "
            "(6) Distance to HVL matters: price far below HVL = deeply negative gamma = very wide stops needed. "
            "Check HVL EVERY MORNING before the session. Never trade without knowing which regime you're in."
        ),
        "tags": ["hvl", "gamma_flip", "regime_switch", "key_level", "core"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_hvl_reclaim_setup",
        "title": "HVL Reclaim — High Conviction Long Setup",
        "category": "strategy",
        "content": (
            "HVL Reclaim is one of the strongest Menthor Q trade setups. "
            "Setup conditions: Price was below HVL (negative gamma regime), "
            "then breaks back above and HOLDS above HVL for 2+ bars. "
            "Why it works: The moment price crosses back above HVL, dealers flip from "
            "short gamma to long gamma. They immediately start buying dips instead of selling rallies. "
            "This creates a mechanical bid that supports price. "
            "Entry: Long on the first pullback to HVL after a confirmed reclaim (HVL acts as new support). "
            "Stop: Below HVL (a re-loss of HVL invalidates the setup). "
            "Target: Next GEX resistance level above, or +1x ATR. "
            "Confluence boosters: CVD turning positive on the reclaim, rising bar delta, "
            "bullish TF vote flip, price above VWAP. "
            "Avoid: Do not chase the initial HVL break — wait for the pullback test. "
            "Best time: OPEN_MOMENTUM or MID_MORNING session windows."
        ),
        "tags": ["hvl_reclaim", "long_setup", "regime_flip", "gamma_flip"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "normal_to_high",
        "direction": "LONG",
    },

    {
        "id": "mq_hvl_loss_setup",
        "title": "HVL Loss — High Conviction Short Setup",
        "category": "strategy",
        "content": (
            "HVL Loss (break below HVL) is one of the strongest Menthor Q short setups. "
            "Setup conditions: Price was above HVL (positive gamma regime — stable), "
            "then breaks below HVL and CLOSES below on a 5m bar. "
            "Why it works: Dealers flip from long gamma (suppressing) to short gamma (amplifying). "
            "They immediately start selling into drops, creating a mechanical headwind for longs. "
            "Entry: Short on the first failed retest of HVL from below (HVL now acts as resistance). "
            "Stop: Above HVL (re-reclaim invalidates the setup). "
            "Target: Next GEX support level below, GEX Level 1 or 0DTE put support. "
            "Widen stops 1.5x — you've entered a negative gamma regime, moves are larger. "
            "Confluence boosters: CVD turning negative, bearish bar delta, "
            "bearish TF vote flip, price below VWAP, VIX rising. "
            "Best time: Early session (8:30-10:00 CST) when institutional positioning kicks in."
        ),
        "tags": ["hvl_loss", "short_setup", "regime_flip", "gamma_flip"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "high",
        "direction": "SHORT",
    },

    # ══════════════════════════════════════════════════════════════════
    # DEX (DELTA EXPOSURE)
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_dex_explained",
        "title": "DEX (Delta Exposure) — Directional Dealer Bias",
        "category": "core_concept",
        "content": (
            "DEX (Delta Exposure) measures the net directional delta held by dealers. "
            "Calculated as call delta minus put delta across the options chain. "
            "Positive DEX: dealers net long delta. They are already 'long' via options hedges. "
            "  → Structural buying pressure. Market has an upward drift bias. "
            "  → Pullbacks are shallower — dealers mechanically buy dips to rehedge. "
            "Negative DEX: dealers net short delta. Structural selling pressure. "
            "  → Rallies are weaker — dealers mechanically sell into strength. "
            "  → Good environment for SHORT setups. "
            "GEX + DEX combined tells the complete story: "
            "  Negative GEX + Negative DEX = WORST environment for longs. "
            "    Dealers amplify moves AND have structural short bias. This is a waterfall setup. "
            "  Negative GEX + Positive DEX = Volatile but with upward drift. "
            "    Good for momentum longs on strong days despite high volatility. "
            "  Positive GEX + Negative DEX = Suppressed bearish drift. "
            "    Fade rallies, buy will not stick. "
            "  Positive GEX + Positive DEX = Most bullish stable environment. "
            "    Mean-reversion longs, dip-buying works well. "
            "DEX is less important than GEX on fast intraday moves but crucial for session bias."
        ),
        "tags": ["dex", "delta", "directional_bias", "dealers"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    # ══════════════════════════════════════════════════════════════════
    # GEX LEVELS
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_gex_levels_1_to_10",
        "title": "GEX Levels 1-10 — Intraday Support and Resistance",
        "category": "key_levels",
        "content": (
            "GEX Levels 1-10 are strike clusters with the highest net gamma/delta within "
            "the day's expected move range. Level 1 = strongest, Level 10 = weakest. "
            "How they work: Large open interest at a strike = dealers must hedge aggressively. "
            "This creates magnetic price behavior: price is attracted to these levels. "
            "Trading rules for GEX levels: "
            "(1) In POSITIVE GEX regime: levels act as reliable S/R. "
            "    Fade moves INTO Level 1 (expect rejection). "
            "    Buy bounces FROM Level 1 (dealer buying provides support). "
            "(2) In NEGATIVE GEX regime: a BREAK of Level 1 triggers cascading dealer re-hedging. "
            "    Breaking Level 1 to the downside = dealers forced to sell more = fast drop to Level 2. "
            "    Breaking Level 1 to the upside = dealers forced to buy more = fast ramp to next resistance. "
            "(3) GEX Level 1 is a scalp target: enter before it, take profit at it. "
            "(4) Multiple GEX levels clustered together = stronger zone. "
            "(5) 0DTE GEX levels (same-day expiry) are MOST powerful in last 2 hours. "
            "    0DTE gamma is exponentially higher than longer-dated gamma. "
            "    A 0DTE put support break in the afternoon = very fast, aggressive move down."
        ),
        "tags": ["gex_levels", "support_resistance", "key_levels", "0dte"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_call_wall_put_wall",
        "title": "Call Wall and Put Wall — Session Boundaries",
        "category": "key_levels",
        "content": (
            "Call Resistance (Call Wall): The strike with highest net call gamma. "
            "Acts as an upward ceiling for the session. In positive gamma, very hard to break. "
            "In negative gamma, a break of the call wall triggers a gamma squeeze (fast move up). "
            "Put Support (Put Wall): The strike with highest net put gamma. "
            "Acts as a downward floor. In positive gamma, reliable support. "
            "In negative gamma, a break of the put wall triggers cascading selling. "
            "0DTE versions (same-day options only) are more powerful than all-expiry versions "
            "because 0DTE gamma is extreme and hedging flows happen immediately. "
            "Session planning with walls: "
            "(1) The distance between put wall and call wall = the day's expected range. "
            "(2) If price opens between the walls and GEX is positive, expect a range day. "
            "(3) If price opens outside the walls or GEX is deeply negative, expect trend day. "
            "(4) Use put wall as short target on bear days, call wall as long target on bull days. "
            "(5) A 0DTE call wall break in the morning = strong conviction long, hold until 0DTE expires. "
            "NQ conversion: If using QQQ-based levels, multiply QQQ price by (NQ/QQQ ratio, typically ~42)."
        ),
        "tags": ["call_wall", "put_wall", "0dte", "key_levels", "session_range"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ", "QQQ"],
    },

    # ══════════════════════════════════════════════════════════════════
    # VANNA AND CHARM
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_vanna_flow",
        "title": "Vanna Flow — VIX-Driven Dealer Hedging Cascades",
        "category": "advanced_flow",
        "content": (
            "Vanna measures how dealer delta changes as implied volatility (VIX) changes. "
            "It creates self-reinforcing feedback loops that can cause sharp, fast moves. "
            "BEARISH Vanna (dangerous for longs): "
            "  Triggered when VIX is rising + large short put open interest exists. "
            "  Rising VIX → put deltas increase → dealers must sell more underlying to rehedge. "
            "  More dealer selling → price drops → VIX rises more → more dealer selling → cascade. "
            "  Signature: sharp, seemingly unexplained selloffs that accelerate on each bounce failure. "
            "  Rule: When Menthor Q shows Vanna = BEARISH, do NOT fight the tape with longs. "
            "        Stops will get blown through. The mechanical selling is relentless. "
            "BULLISH Vanna (tailwind for longs): "
            "  Triggered when VIX is falling + large short call open interest or put OI decaying. "
            "  Falling VIX → put deltas decrease → dealers buy back their short delta hedges. "
            "  This creates a 'vanna bid' — sustained mechanical buying as VIX falls. "
            "  Signature: market grinds up even on no news, dips are quickly bought. "
            "  Rule: When Vanna = BULLISH and VIX is falling, buy dips aggressively. "
            "Most impactful: On VIX spike days (up >15-20%), vanna flows dominate all other signals. "
            "After a VIX spike, the vanna unwind (VIX falling) creates the fastest rallies."
        ),
        "tags": ["vanna", "vix", "dealer_hedging", "cascade", "advanced"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING", "LATE_AFTERNOON"],
        "instruments": ["NQ", "MNQ", "VIX"],
    },

    {
        "id": "mq_charm_flow",
        "title": "Charm Flow — Time Decay Driven Dealer Buying/Selling",
        "category": "advanced_flow",
        "content": (
            "Charm measures how dealer delta changes over time (theta decay of delta). "
            "As options approach expiration, their delta changes even without price movement. "
            "BULLISH Charm: OTM puts decaying toward zero delta. "
            "  As put delta decays, dealers no longer need to be short to hedge. "
            "  They buy back their short futures/underlying positions. "
            "  Creates a 'charm bid' — structural buying that lifts price. "
            "  Strongest in the days AFTER monthly OPEX when large put positions expire. "
            "  Rule: Post-OPEX week is often bullish drift even in bearish macro. "
            "BEARISH Charm: OTM calls or deep ITM puts approaching expiry. "
            "  Creates structural selling as dealers unwind long delta hedges. "
            "  Less common but can create persistent selling pressure. "
            "Combined with Vanna: "
            "  Both Vanna and Charm are secondary Greeks. They matter most around: "
            "  (1) Monthly OPEX (options expiration) — largest delta shifts. "
            "  (2) VIX spikes — magnifies vanna. "
            "  (3) 0DTE concentration — largest charm flows on days with high 0DTE OI. "
            "  Post-OPEX week warning: With large open interest expiring, the vanna/charm "
            "  'stabilizers' disappear. Price action becomes MORE volatile and less predictable."
        ),
        "tags": ["charm", "opex", "theta_decay", "dealer_hedging", "advanced"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    # ══════════════════════════════════════════════════════════════════
    # CTA MODEL
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_cta_model",
        "title": "CTA (Commodity Trading Advisor) Positioning Model",
        "category": "institutional_flow",
        "content": (
            "CTA model tracks estimated exposure of systematic trend-following hedge funds. "
            "These are quantitative funds that follow price trends mechanically. "
            "They move in massive size and create self-reinforcing momentum. "
            "Key CTA signals: "
            "(1) CTA BUYING: Systematic funds are adding long exposure. "
            "    → Provides a strong mechanical tailwind for longs. "
            "    → Pullbacks are shallow — CTAs buy dips mechanically. "
            "    → Breakouts above key levels are more likely to hold. "
            "(2) CTA SELLING: Systematic funds liquidating longs or adding shorts. "
            "    → Creates sustained selling pressure independent of news. "
            "    → Rallies fail — CTAs sell into strength mechanically. "
            "    → Each new low triggers more systematic selling. "
            "(3) CTA NEUTRAL/FLAT: Funds waiting for a trend signal. "
            "    → Market is rudderless. Choppy, mean-reverting. "
            "Extreme positioning (Z-score > +2 or < -2): "
            "  CTAs max LONG: Mean reversion risk is high. Any catalyst = sharp selloff. "
            "  CTAs max SHORT: Powerful squeeze potential. Small catalyst = massive squeeze. "
            "The BEST trade setup: CTAs max short + GEX negative + any positive catalyst. "
            "This triggers an exponential short-covering squeeze that can run 2-5% in hours. "
            "Anti-setup: Do NOT fight CTA trends. If CTAs are selling + GEX negative, "
            "short positions have institutional-scale tailwind. Ride, don't fade."
        ),
        "tags": ["cta", "systematic_funds", "institutional", "trend_following"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ", "ES"],
    },

    # ══════════════════════════════════════════════════════════════════
    # 0DTE SPECIFIC STRATEGIES
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_0dte_call_wall_break",
        "title": "0DTE Call Wall Breakout — Gamma Squeeze Long",
        "category": "strategy",
        "content": (
            "0DTE Call Wall Break is a high-conviction long trade powered by gamma mechanics. "
            "Setup: Price approaches and then BREAKS ABOVE the 0DTE call resistance level "
            "on meaningful volume (above average for the time of day). "
            "Why it works: 0DTE call gamma is enormous (exponential near expiry). "
            "Dealers who sold those calls are short massive gamma above the strike. "
            "When price breaks above, dealers must BUY futures aggressively to rehedge. "
            "This forced buying creates a mechanical ramp — a 'gamma squeeze.' "
            "Entry: Buy the break of 0DTE call resistance (market or limit + 1 tick). "
            "Stop: Below the 0DTE call level (if it becomes support, stop is tight). "
            "Target: Next resistance level above or +0.5x ATR from entry. "
            "Time: Most powerful between 9:30 AM - 11:30 AM CST (high 0DTE volume). "
            "Also powerful in last 90 minutes as gamma spikes exponentially. "
            "Avoid: In deeply positive GEX — the call wall is TOO strong, no break expected. "
            "Confirmation: CVD should be positive, bar delta positive on break bar. "
            "GEX regime: Works in NEGATIVE GEX (amplified moves) or transitioning to positive."
        ),
        "tags": ["0dte", "call_wall", "gamma_squeeze", "long_setup", "breakout"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "normal_to_high",
        "direction": "LONG",
    },

    {
        "id": "mq_0dte_put_wall_break",
        "title": "0DTE Put Wall Break — Gamma Cascade Short",
        "category": "strategy",
        "content": (
            "0DTE Put Wall Break is a high-conviction short trade powered by gamma mechanics. "
            "Setup: Price approaches and then BREAKS BELOW the 0DTE put support level. "
            "Why it works: Dealers who sold those puts are short massive gamma below the strike. "
            "When price breaks below, dealers must SELL futures aggressively to rehedge. "
            "This forced selling creates a mechanical drop — a 'gamma cascade.' "
            "Entry: Short the break of 0DTE put support. "
            "Stop: Above the 0DTE put level (a recovery back above = setup failed). "
            "Target: Next GEX support level below. On big days: -1x ATR from entry. "
            "Time: Afternoon (1 PM - 3 PM CST) when 0DTE gamma is most extreme. "
            "In the last 2 hours of trading, 0DTE gamma can be 10x longer-dated gamma. "
            "A put wall break at 2:30 PM can trigger a 50-100 NQ point waterfall. "
            "Confirmation: CVD should be negative, bar delta negative on break bar. "
            "In NEGATIVE GEX regime: put wall breaks are even more aggressive. "
            "In POSITIVE GEX regime: put wall may hold — wait for a second test before shorting. "
            "Avoid: Do NOT short into a put wall in positive GEX — dealers defend it too well."
        ),
        "tags": ["0dte", "put_wall", "gamma_cascade", "short_setup", "breakdown"],
        "regimes": ["MID_MORNING", "LATE_AFTERNOON"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "normal_to_high",
        "direction": "SHORT",
    },

    # ══════════════════════════════════════════════════════════════════
    # COMBINED REGIME PATTERNS
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_negative_gex_negative_dex_bear",
        "title": "Negative GEX + Negative DEX = Bear Waterfall Setup",
        "category": "regime_pattern",
        "content": (
            "The most dangerous environment for LONG positions: negative GEX AND negative DEX. "
            "What it means: "
            "  GEX negative → dealers amplify every move down. "
            "  DEX negative → dealers have structural short bias, mechanically selling strength. "
            "Combined effect: Every bounce is sold by dealers. Every drop is amplified by dealers. "
            "This is a WATERFALL setup — price can fall 2-5% in a single session. "
            "Trading rules: "
            "(1) NO LONGS unless price clearly reclaims HVL. Any long is fighting two mechanical forces. "
            "(2) SHORT every resistance bounce. Use GEX levels and VWAP as entry points. "
            "(3) Widen stops 1.5-2x — the mechanical selling creates large swings before continuation. "
            "(4) Targets: next GEX level down, 0DTE put support, prior day low. "
            "(5) If also seeing: Vanna BEARISH + CTA SELLING = extreme conviction short day. "
            "    This 4-factor alignment (negative GEX, negative DEX, bearish vanna, CTA selling) "
            "    is the setup that produces -3% to -5% NQ days. "
            "Real-world example (April 13 2026): All 4 factors aligned bearishly. "
            "Bot that ignored this lost -$97. Bot that shorted made money."
        ),
        "tags": ["negative_gex", "negative_dex", "waterfall", "bear", "regime_pattern"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "high",
        "direction": "SHORT",
    },

    {
        "id": "mq_negative_gex_positive_dex_squeeze",
        "title": "Negative GEX + Positive DEX = Squeeze Potential",
        "category": "regime_pattern",
        "content": (
            "An interesting and volatile combination: negative GEX but positive DEX. "
            "GEX negative → moves are amplified in both directions. "
            "DEX positive → dealers have structural long delta bias. "
            "This creates a split personality: volatile but with upward drift potential. "
            "Common setup: After a selloff, large put OI remains (DEX positive from put hedges), "
            "but GEX is negative because OTM puts are now closer to the money. "
            "Trading rules: "
            "(1) Both directions are possible — need additional confirmation (CVD, CTA). "
            "(2) If CTA is also buying → strong long setup despite negative GEX. "
            "    Negative GEX just means the LONG will be fast and volatile — wider stops needed. "
            "(3) If CTA is neutral/selling → choppy, whipsaw day. "
            "    Reduce size. Consider sitting out. "
            "(4) Best setup: Price has been below HVL, vanna is turning BULLISH (VIX falling), "
            "    CTA flipping to buy, DEX positive → this is the classic squeeze setup. "
            "(5) Target: HVL reclaim is the first key goal. Above HVL, the regime stabilizes. "
            "Key insight: Negative GEX + Positive DEX days produce the BIGGEST intraday swings "
            "in both directions. Reduce contracts but widen stops if trading."
        ),
        "tags": ["negative_gex", "positive_dex", "squeeze", "volatile", "both_directions"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "high",
        "direction": "BOTH",
    },

    {
        "id": "mq_positive_gex_both_dex",
        "title": "Positive GEX Regime — Range Day Playbook",
        "category": "regime_pattern",
        "content": (
            "Positive GEX = most stable, range-bound environment. "
            "The specific DEX direction shifts the range's lean: "
            "Positive GEX + Positive DEX: Stable and bullish drift. "
            "  → Buy dips to VWAP and GEX levels. Take profit at call wall. "
            "  → Spring setups work well. VWAP pullbacks excellent. "
            "  → Stops can be TIGHTER — moves are suppressed by dealer buying/selling. "
            "Positive GEX + Negative DEX: Stable but bearish lean. "
            "  → Fade rallies to VWAP and GEX resistance. Take profit at put wall. "
            "  → Range fade strategies work. "
            "In BOTH cases: "
            "(1) DO NOT trade breakouts — they fail. GEX levels hold. "
            "(2) Position size can be LARGER — the range is predictable. "
            "(3) Time exits: price tends to revert to VWAP by session midpoint. "
            "(4) IB (Initial Balance) range tends to hold the day better. "
            "Stop guidance: In positive GEX, use 6-8 tick stops (tighter). "
            "The mechanical dealer hedging won't let prices run far against you "
            "before they reverse — smaller stops are actually safer here."
        ),
        "tags": ["positive_gex", "range_day", "mean_reversion", "stable"],
        "regimes": ["AFTERNOON_CHOP", "MID_MORNING", "OPEN_MOMENTUM"],
        "instruments": ["NQ", "MNQ"],
        "atr_preference": "low_to_normal",
        "direction": "BOTH",
    },

    # ══════════════════════════════════════════════════════════════════
    # OPEX AND SEASONAL PATTERNS
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_opex_week_warning",
        "title": "Monthly OPEX Week — Volatility Spike Warning",
        "category": "seasonal",
        "content": (
            "Monthly options expiration (OPEX, 3rd Friday of each month) removes the "
            "large vanna and charm stabilizing flows that support orderly markets. "
            "Week BEFORE OPEX: GEX often at its highest (max positive) as dealers hedge large OI. "
            "  → Best mean-reversion week of the month. Reliable range-bound action. "
            "OPEX day itself: Large gamma unwind as contracts expire. "
            "  → Morning: Pinning behavior (price attracted to largest open interest strike). "
            "  → Afternoon: Potential for large moves as gamma expires and hedges unwind. "
            "  → DO NOT hold overnight into OPEX Friday — positions can gap hard. "
            "Week AFTER OPEX: The dangerous window. "
            "  → Vanna and charm hedges that kept the market stable have expired. "
            "  → Price can move much more freely — and violently. "
            "  → GEX often drops sharply as large OI expires. "
            "  → This is when you see the biggest gap moves and trend days. "
            "  → Widen stops. Reduce size. Expect the unexpected. "
            "  → If any macro catalyst hits in post-OPEX week, expect 2-3x the normal range. "
            "Monthly OPEX dates: Always the 3rd Friday of the month. "
            "Quarterly OPEX (March, June, September, December) is the most powerful — "
            "ES/NQ/QQQ options all expire simultaneously ('Triple Witching'). "
            "The post-quarterly-OPEX week sees the highest volatility of the quarter."
        ),
        "tags": ["opex", "options_expiration", "seasonal", "volatility", "quarterly"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ", "ES", "SPX"],
    },

    {
        "id": "mq_gamma_squeeze_setup",
        "title": "Gamma Squeeze Setup — CTAs Max Short + Negative GEX + Catalyst",
        "category": "strategy",
        "content": (
            "The gamma squeeze is the most powerful mechanical move in modern markets. "
            "Conditions for a gamma squeeze: "
            "(1) CTAs at extreme short positioning (Z-score < -2) "
            "(2) Net GEX deeply negative "
            "(3) Large put OI (bearish positioning) "
            "(4) A positive catalyst (Fed pivot, earnings beat, policy reversal, etc.) "
            "What happens: "
            "  Catalyst hits → price rises → CTA algorithms trigger buy signals (trend reversal) "
            "  → Dealers short gamma must buy to rehedge (amplifies the move) "
            "  → Put sellers become profitable, close positions (buy back) "
            "  → This creates an EXPONENTIAL move: each % gain triggers more buying "
            "  → Typical squeeze: 3-8% in 2-4 hours "
            "Historical example: April 2026 — S&P broke 6,500 in a single session on "
            "-$7.5B GEX squeeze when tariff news reversed. "
            "Trading the squeeze: "
            "(1) You CANNOT short into a squeeze — you will be destroyed. "
            "(2) Buy early on the catalyst — first 5m bar breaking key resistance. "
            "(3) Widen stops significantly — the path up is NOT linear. "
            "(4) Do NOT take profit too early — squeezes run much further than expected. "
            "(5) Exit when CTA positioning normalizes or GEX turns strongly positive. "
            "Pre-squeeze checklist: CTAs max short? GEX negative? Large put OI? Catalyst imminent? "
            "If yes to all 4: LONG bias only, do not short, widen stops, let winners run."
        ),
        "tags": ["gamma_squeeze", "short_squeeze", "cta", "catalyst", "extreme_setup"],
        "regimes": ["OPEN_MOMENTUM"],
        "instruments": ["NQ", "MNQ", "ES"],
        "atr_preference": "high",
        "direction": "LONG",
    },

    # ══════════════════════════════════════════════════════════════════
    # PRE-TRADE FILTER RULES (AI decision-making)
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_pretrade_long_in_negative_gex",
        "title": "Pre-Trade Rule: LONG Signal in Negative GEX Regime",
        "category": "pretrade_rule",
        "content": (
            "When a LONG signal fires and GEX is NEGATIVE: apply elevated scrutiny. "
            "A LONG in negative GEX is fighting against dealer amplification of down moves. "
            "Default action: CAUTION (reduce size to 50%) or SIT_OUT. "
            "SIT_OUT if ALL of these: price below HVL, CVD negative, DEX negative, vanna bearish. "
            "CAUTION if: price near HVL (within 0.5x ATR), CVD turning positive, bar delta positive. "
            "CLEAR only if: price has clearly reclaimed HVL on multiple bars, "
            "CVD is positive, AND there's a strong bullish catalyst. "
            "Key question for AI: 'Is the LONG signal fighting the mechanical dealer flow?' "
            "If dealers are short gamma below HVL AND shorting into strength (negative DEX), "
            "any LONG faces a double mechanical headwind. High SIT_OUT probability. "
            "Exception: If there's a massive positive catalyst (FOMC, presidential tweet), "
            "a squeeze can override negative GEX. In that case: CLEAR but with wider stop."
        ),
        "tags": ["pretrade_rule", "long", "negative_gex", "filter", "risk"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_pretrade_short_in_negative_gex",
        "title": "Pre-Trade Rule: SHORT Signal in Negative GEX Regime",
        "category": "pretrade_rule",
        "content": (
            "When a SHORT signal fires and GEX is NEGATIVE and price is below HVL: "
            "This is the highest-conviction trade setup in the Menthor Q framework. "
            "Dealer mechanics AMPLIFY the move down. "
            "Default action: CLEAR — proceed with the short. "
            "BOOST confidence if: CVD negative, bar delta negative, DEX negative, vanna bearish. "
            "Apply stop widener: in negative GEX, use 1.5x normal stop size. "
            "The stop MUST be wider because negative gamma creates larger swings before continuation. "
            "Do NOT CAUTION or SIT_OUT a valid SHORT in negative GEX unless: "
            "  - There's an imminent news event (SIT_OUT) "
            "  - The bot has already hit daily loss limit "
            "  - CVD is POSITIVE (buying pressure contradicts short) "
            "  - Price is within 5 ticks of 0DTE put support (wait for the break, don't anticipate) "
            "Best SHORT entries in negative GEX: "
            "  - First failed retest of HVL from below "
            "  - Bounce to GEX Level 1 resistance with CVD still negative "
            "  - VWAP reject when price below VWAP and GEX negative"
        ),
        "tags": ["pretrade_rule", "short", "negative_gex", "filter", "high_confidence"],
        "regimes": ["OPEN_MOMENTUM", "MID_MORNING"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_pretrade_any_signal_positive_gex",
        "title": "Pre-Trade Rule: Any Signal in Positive GEX Regime",
        "category": "pretrade_rule",
        "content": (
            "When GEX is POSITIVE and price is above HVL: both directions are OK but adjust strategy. "
            "LONG signals in positive GEX: "
            "  CLEAR if: price pulls back to VWAP or GEX level support, bounces, CVD positive. "
            "  CAUTION if: price is near or AT the call wall (close to a ceiling). "
            "  SIT_OUT if: price already AT the call wall (no room to run). "
            "SHORT signals in positive GEX: "
            "  CLEAR if: price near call wall resistance, CVD turning negative, fade setup. "
            "  CAUTION if: CVD still positive (buyers present, short is early). "
            "  SIT_OUT if: price is near put support (close to the floor, low reward). "
            "Stop guidance for positive GEX: "
            "  Use TIGHTER stops than normal (e.g., 6-7 ticks instead of 9). "
            "  Dealer hedging suppresses moves — if price moves against you more than 7 ticks "
            "  in positive GEX, the setup is likely wrong. Cut it. "
            "General rule: In positive GEX, think 'range trader.' "
            "Enter near GEX support/resistance. Exit before the opposite wall. "
            "Do NOT let winners run past GEX levels — they will reverse."
        ),
        "tags": ["pretrade_rule", "positive_gex", "filter", "both_directions", "range"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    # ══════════════════════════════════════════════════════════════════
    # MORNING BRIEFING KNOWLEDGE
    # ══════════════════════════════════════════════════════════════════

    {
        "id": "mq_morning_routine",
        "title": "Menthor Q 5-Minute Morning Briefing Checklist",
        "category": "workflow",
        "content": (
            "Every trading morning, before 8:30 AM CST, check these Menthor Q metrics: "
            "Step 1 — GEX Regime (30 seconds): "
            "  Is net GEX positive or negative? How large is it (billions)? "
            "  This tells you: fade moves or follow them. "
            "Step 2 — HVL (30 seconds): "
            "  What is the High Vol Level? Is the current overnight price above or below it? "
            "  Above HVL = positive gamma, stable. Below HVL = negative gamma, momentum. "
            "Step 3 — DEX (15 seconds): "
            "  Positive or negative? Confirms direction bias. "
            "Step 4 — Key Levels (60 seconds): "
            "  Write down: Call Resistance, Put Support, 0DTE Call Resistance, 0DTE Put Support, "
            "  GEX Level 1 and Level 2. These are your intraday S/R for the whole day. "
            "Step 5 — Vanna and CTA (30 seconds): "
            "  Bullish or Bearish vanna? CTAs buying or selling? "
            "  This confirms the direction bias. "
            "Step 6 — Regime Summary: "
            "  Based on steps 1-5, decide: "
            "  (a) Momentum day (negative GEX + below HVL): pick a direction, follow it, wide stops. "
            "  (b) Range day (positive GEX + above HVL): fade extremes, tight stops, GEX levels. "
            "  (c) Transition day (near HVL): small size, wait for confirmation, trade the break. "
            "Record your regime call in data/menthorq_daily.json BEFORE starting the bot. "
            "The bot reads this file and applies the appropriate gates automatically."
        ),
        "tags": ["morning_routine", "workflow", "checklist", "preparation"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_council_voter_input",
        "title": "How Council AI Should Use Menthor Q Data",
        "category": "ai_guidance",
        "content": (
            "When the AI council receives Menthor Q data in its prompt, interpret it as follows: "
            "GEX NEGATIVE + Price below HVL: "
            "  → BEARISH vote. Dealers amplify moves. Mechanical selling expected. "
            "  → Confidence: HIGH (70-90). "
            "  → Supporting note: 'Negative gamma regime + below HVL = momentum bear day.' "
            "GEX POSITIVE + Price above HVL: "
            "  → Vote depends on DEX and other signals. "
            "  → GEX alone doesn't give direction in positive regime — check DEX and CVD. "
            "  → DEX positive → BULLISH vote. DEX negative → BEARISH vote. "
            "GEX NEGATIVE + Price above HVL: "
            "  → Volatile but bullish potential. "
            "  → Vote BULLISH if CVD positive + CTA buying. "
            "  → Vote NEUTRAL if CVD mixed. Note: 'Negative GEX amplifies upside as well.' "
            "Vanna BEARISH + GEX NEGATIVE: "
            "  → Strong BEARISH vote. Double mechanical selling. Confidence 80+. "
            "Vanna BULLISH + GEX transitioning positive: "
            "  → Strong BULLISH vote. Vanna bid + dealer buying. Confidence 75+. "
            "CTA SELLING + GEX NEGATIVE: "
            "  → Very strong BEARISH vote. Systematic + mechanical selling aligned. "
            "CTA max SHORT (extreme) + GEX NEGATIVE + catalyst present: "
            "  → Flag SQUEEZE POTENTIAL. Vote NEUTRAL with note about squeeze risk. "
            "  → Both directions are dangerous. Humans should decide. "
            "Always weight Menthor Q data heavily — it represents real institutional mechanics, "
            "not just price chart patterns. GEX is more reliable than EMA for regime detection."
        ),
        "tags": ["ai_guidance", "council", "interpretation", "voting"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_debrief_analysis",
        "title": "Session Debrief: Evaluating Trades Against Menthor Q Context",
        "category": "ai_guidance",
        "content": (
            "When debriefing a trading session with Menthor Q data available: "
            "For each trade, ask: "
            "(1) Was the trade direction aligned with the GEX regime? "
            "    LONG in negative GEX = fighting mechanics. Should have been flagged. "
            "    SHORT in negative GEX = with mechanics. Good setup regardless of outcome. "
            "(2) Was entry near a meaningful GEX level? "
            "    Entering far from any GEX level = no mechanical support for the setup. "
            "    Entering near GEX Level 1 = best mechanical support. "
            "(3) Did the stop account for the GEX regime? "
            "    Negative GEX requires 1.5x wider stops. Tight stops in negative gamma = getting stopped out. "
            "(4) Did vanna/charm context match the trade? "
            "    LONG when Vanna BEARISH = VIX-driven selling pressure fighting every tick. "
            "(5) Was the CTA model supporting the trade direction? "
            "    CTA selling + trading LONG = institutional-scale headwind. "
            "Debrief output format for MQ analysis: "
            "  'The GEX regime was [X]. The trade direction was [aligned/misaligned]. "
            "   Key MQ level nearest to entry: [level]. Stop adequacy for regime: [yes/no]. "
            "   Recommendation: [adjust direction gate / widen stops / use GEX levels as targets].' "
            "Most common MQ-related mistakes: "
            "  (1) Taking LONGs while below HVL in negative GEX (regime blindness) "
            "  (2) Using tight stops in negative gamma (getting whipsawed out) "
            "  (3) Ignoring GEX levels as exit targets (leaving money on table) "
            "  (4) Not widening targets in negative gamma (moves run further than expected)"
        ),
        "tags": ["debrief", "post_trade", "analysis", "ai_guidance"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ"],
    },

    {
        "id": "mq_blind_spot_levels",
        "title": "Blind Spot Levels — Cross-Market Confluence Zones",
        "category": "key_levels",
        "content": (
            "Menthor Q Blind Spot Levels (BL) are proprietary confluence zones where "
            "price levels from multiple correlated instruments (SPX, QQQ, VIX, etc.) overlap. "
            "BL 1 = strongest (most instruments converge on same price). BL 10 = weakest. "
            "How they work: When multiple instruments' gamma/delta hedging pressure "
            "converges on the same underlying price level, that zone has outsized significance. "
            "Trading rules: "
            "(1) BL 1-3: Expect sharp reactions — either hard rejection or breakout acceleration. "
            "(2) BL 1-3 near your entry: DO NOT open a new trade directly into a Blind Spot level. "
            "    Wait for the reaction, then trade the aftermath. "
            "(3) Blind Spot levels as profit targets: If already in a trade, "
            "    take full profit at BL 1-3 — high probability of reversal. "
            "(4) BL 1-3 as entry triggers: If price bounces from a BL with CVD confirming, "
            "    that's a high-confidence entry point. "
            "(5) BL 4-10: Secondary zones. Use as secondary targets or looser S/R. "
            "Combine with GEX levels: A Blind Spot that coincides with a GEX Level 1 "
            "is the strongest possible S/R zone. Price will react violently there. "
            "BL levels update nightly (~11 PM EST). Check before each session."
        ),
        "tags": ["blind_spot", "confluence", "cross_market", "key_levels"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ", "SPX", "QQQ"],
    },

    {
        "id": "mq_volatility_risk_premium",
        "title": "VRP (Volatility Risk Premium) — Stop and Size Guidance",
        "category": "risk_management",
        "content": (
            "VRP = Implied Volatility (IV) minus Historical/Realized Volatility (HV). "
            "When IV >> HV (high VRP): Market is overpaying for options = fear premium elevated. "
            "  → Widen stops: options market expects bigger moves than history suggests. "
            "  → Reduce position size: uncertainty is elevated. "
            "  → Better to be an option seller than buyer in this environment. "
            "  → For futures traders: use 1.5-2x your normal stop distance. "
            "When IV ≈ HV (normal VRP): Standard conditions. Normal stops and sizing apply. "
            "When IV < HV (negative VRP): Complacency. Market underpricing realized moves. "
            "  → Consider protective hedges (buy options as insurance). "
            "  → This often precedes a volatility spike. "
            "  → Tighten risk — realized volatility has been exceeding expected. "
            "VRP and GEX interaction: "
            "  High VRP + Negative GEX = extremely dangerous for tight stops. "
            "  The market is expecting big moves AND dealers are amplifying them. "
            "  → Use 2x normal stops, half normal size, or sit out. "
            "  High VRP + Positive GEX = still elevated fear but suppressed by dealer hedging. "
            "  → Use 1.25x normal stops. Standard size OK. "
            "Bot adjustment: When VRP is high, apply the MQ stop_multiplier from the daily file. "
            "A stop_multiplier of 1.5 in high VRP negative GEX environments is non-negotiable."
        ),
        "tags": ["vrp", "volatility", "stops", "position_sizing", "risk"],
        "regimes": ["all"],
        "instruments": ["NQ", "MNQ", "VIX"],
    },

]


# ── Loader Function ───────────────────────────────────────────────────

def load_menthorq_knowledge(reset: bool = False) -> dict:
    """
    Load all Menthor Q knowledge entries into the RAG system.

    Returns:
        {"loaded": N, "skipped": N, "total": N}
    """
    try:
        from core.knowledge_rag import KnowledgeRAG
    except ImportError as e:
        logger.error(f"Cannot import KnowledgeRAG: {e}")
        return {"loaded": 0, "skipped": 0, "total": 0}

    rag = KnowledgeRAG()

    if reset:
        logger.info("Resetting all MQ entries...")
        deleted = 0
        for entry in MENTHORQ_KNOWLEDGE:
            try:
                rag._collection.delete(ids=[entry["id"]])
                deleted += 1
            except Exception:
                pass
        logger.info(f"Deleted {deleted} MQ entries. Re-seeding...")

    loaded = 0
    skipped = 0

    for entry in MENTHORQ_KNOWLEDGE:
        try:
            # Check if already loaded
            existing = rag._collection.get(ids=[entry["id"]])
            if existing["ids"] and not reset:
                skipped += 1
                continue

            # Build full text for embedding
            full_text = (
                f"Menthor Q — {entry['title']}\n\n"
                f"Category: {entry['category']}\n"
                f"Tags: {', '.join(entry.get('tags', []))}\n"
                f"Regimes: {', '.join(entry.get('regimes', ['all']))}\n"
                f"Instruments: {', '.join(entry.get('instruments', ['NQ', 'MNQ']))}\n\n"
                f"{entry['content']}"
            )

            metadata = {
                "type": "menthorq",
                "category": entry["category"],
                "tags": ",".join(entry.get("tags", [])),
                "regimes": ",".join(entry.get("regimes", ["all"])),
                "instruments": ",".join(entry.get("instruments", ["NQ", "MNQ"])),
                "direction": entry.get("direction", "both"),
                "atr_preference": entry.get("atr_preference", "any"),
                "source": "menthorq_loader",
            }

            rag._collection.add(
                documents=[full_text],
                ids=[entry["id"]],
                metadatas=[metadata],
            )
            loaded += 1
            logger.info(f"  Loaded: {entry['id']} — {entry['title']}")

        except Exception as e:
            logger.error(f"  Failed to load {entry['id']}: {e}")

    total = loaded + skipped
    logger.info(
        f"Menthor Q knowledge: {loaded} loaded, {skipped} skipped (already present), "
        f"{total} total MQ entries"
    )
    return {"loaded": loaded, "skipped": skipped, "total": total}


def query_menthorq(question: str, n_results: int = 5) -> list[dict]:
    """Query the MQ knowledge base for relevant entries."""
    try:
        from core.knowledge_rag import KnowledgeRAG
        rag = KnowledgeRAG()
        results = rag._collection.query(
            query_texts=[question],
            n_results=n_results,
            where={"type": "menthorq"},
        )
        out = []
        for i, doc in enumerate(results["documents"][0]):
            out.append({
                "id": results["ids"][0][i],
                "content": doc,
                "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                "distance": results["distances"][0][i] if results.get("distances") else None,
            })
        return out
    except Exception as e:
        logger.error(f"Query failed: {e}")
        return []


def get_stats() -> dict:
    """Return count of MQ entries in knowledge base."""
    try:
        from core.knowledge_rag import KnowledgeRAG
        rag = KnowledgeRAG()
        all_ids = rag._collection.get(where={"type": "menthorq"})["ids"]
        by_category = {}
        metas = rag._collection.get(where={"type": "menthorq"})["metadatas"] or []
        for m in metas:
            cat = m.get("category", "unknown")
            by_category[cat] = by_category.get(cat, 0) + 1
        return {"total_mq_entries": len(all_ids), "by_category": by_category}
    except Exception as e:
        return {"error": str(e)}


# ── CLI ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Menthor Q Knowledge Loader")
    parser.add_argument("--stats", action="store_true", help="Show MQ entry stats")
    parser.add_argument("--reset", action="store_true", help="Delete and re-seed all MQ entries")
    parser.add_argument("--query", type=str, help="Test query against MQ knowledge")
    args = parser.parse_args()

    if args.stats:
        stats = get_stats()
        print(f"\nMenthor Q Knowledge Base Stats:")
        print(f"  Total MQ entries: {stats.get('total_mq_entries', 0)}")
        by_cat = stats.get("by_category", {})
        for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
            print(f"  {cat}: {count}")

    elif args.query:
        print(f"\nQuerying: '{args.query}'")
        results = query_menthorq(args.query, n_results=5)
        for r in results:
            print(f"\n  [{r['id']}] distance={r.get('distance', '?'):.3f}")
            print(f"  {r['content'][:300]}...")

    else:
        print(f"\nLoading {len(MENTHORQ_KNOWLEDGE)} Menthor Q knowledge entries...")
        result = load_menthorq_knowledge(reset=args.reset)
        print(f"Done: {result['loaded']} loaded, {result['skipped']} already present")
        stats = get_stats()
        print(f"Total MQ entries in knowledge base: {stats.get('total_mq_entries', 0)}")
        print(f"By category: {stats.get('by_category', {})}")
