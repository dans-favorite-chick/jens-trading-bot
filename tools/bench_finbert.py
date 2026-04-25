"""
Phoenix Bot - FinBERT inference benchmark (Section 4)

Runs FinBERTSentiment.score() over ~100 representative financial headlines,
reports n / p50 / p95 / p99 / max latency in milliseconds, and writes the
result to out/bench/finbert_<host>_<YYYY-MM-DD>.json.

Acceptance gate (used by Section 4 of the build):
  p50 <= 10ms AND p99 <= 50ms

If the real ONNX model isn't on disk yet the benchmark still runs against
the DEGRADED path (which short-circuits to a constant tuple). It emits a
clear "DEGRADED - real model not on disk" line so the result isn't mistaken
for a real measurement.

Run with the ML venv (Python 3.12 + onnxruntime + transformers):
  .venv-ml\\Scripts\\python.exe tools\\bench_finbert.py

Or with the primary Python interpreter for a degraded smoke test:
  python tools\\bench_finbert.py
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import time
from datetime import date
from pathlib import Path

# Make ``core`` importable when executed from the project root.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.sentiment_finbert import FinBERTSentiment  # noqa: E402


HEADLINES: list[str] = [
    # Macro / Fed
    "Fed signals dovish pivot at next FOMC meeting",
    "Hawkish FOMC minutes spook bond market",
    "Powell hints at rate cut by Q3 if inflation cools",
    "Fed leaves rates unchanged, dot plot turns more hawkish",
    "ECB matches Fed with surprise 25bp cut",
    "BoJ ends negative rate policy, yen surges",
    "Treasury yields plunge after dovish Fed minutes",
    "10-year yield breaches 5% for first time since 2007",
    "Yield curve dis-inverts as recession bets fade",
    "Dollar index hits 6-month low on weak ISM print",
    # Inflation / data
    "CPI comes in hotter than expected at 3.7% YoY",
    "Core PCE ticks down to 2.4%, lowest since 2021",
    "PPI surprises to the downside, equities rally",
    "Nonfarm payrolls miss estimates, unemployment ticks up",
    "Hot wage growth reignites inflation fears",
    "Retail sales beat by wide margin, consumer resilient",
    "GDP misses estimates, Q3 print revised lower",
    "Consumer confidence drops to two-year low",
    "Jobless claims rise unexpectedly to 245k",
    "ISM manufacturing PMI falls deeper into contraction",
    # Mega-cap earnings / single name
    "NVDA beats earnings, guides above consensus on AI demand",
    "AAPL misses revenue estimates on weak iPhone sales",
    "MSFT crushes cloud revenue forecast, Azure +29%",
    "GOOGL ad revenue disappoints, shares slump after-hours",
    "META guides spending higher, Reality Labs loss widens",
    "AMZN beats top and bottom line, AWS reaccelerates",
    "TSLA misses deliveries, margins compress further",
    "NFLX subscriber adds smash estimates, FCF guidance raised",
    "AMD lifts data-center outlook, MI300 ramp ahead of schedule",
    "INTC slashes dividend after weak foundry guidance",
    # Sector
    "Banks rally on prospect of looser capital rules",
    "Regional bank stocks tumble on commercial real-estate fears",
    "Energy names slide as crude breaks below $70",
    "Crude oil spikes on OPEC+ surprise output cut",
    "Gold hits record high on safe-haven demand",
    "Bitcoin breaks $100k as ETF flows accelerate",
    "Healthcare ETF sinks on Medicare drug pricing rule",
    "Defense stocks pop on increased Pentagon budget",
    "Semis lead Nasdaq higher on AI capex commentary",
    "Homebuilders rally as 30-year mortgage drops below 6%",
    # Geopolitics / risk
    "Middle East tensions ease, oil retreats 4%",
    "China stimulus disappoints, copper sells off",
    "US-China trade talks stall, tech tariffs threatened",
    "Russia escalates, European gas prices surge",
    "Taiwan strait tensions weigh on chip names",
    "Red Sea shipping disruption lifts container rates",
    "EU passes tougher AI regulation, big tech to face fines",
    "Ukraine ceasefire rumors lift European equities",
    # Credit / corporate
    "Investment-grade spreads tighten to 2021 lows",
    "High-yield issuance hits record monthly pace",
    "Boeing announces production halt at 737 MAX line",
    "Disney activist investor secures board seats",
    "Pfizer slashes guidance after Covid product weakness",
    "Walmart beats and raises guidance, e-commerce surges",
    "Target warns on holiday season, shares drop 10%",
    "Costco posts strong comp sales beat",
    "Home Depot guides cautiously on housing softness",
    "Lowe's beats EPS, comps still negative",
    # Crypto / fintech
    "Coinbase beats on trading volumes, expands derivatives",
    "Robinhood reports record retail options activity",
    "PayPal restructures, plans to cut 9% of workforce",
    "Visa and Mastercard rally on resilient consumer spend",
    "MicroStrategy adds another 12,000 BTC to treasury",
    # Industrials / materials
    "Caterpillar guides above on infrastructure demand",
    "GE Aerospace orders surge to multi-year high",
    "Lockheed lands $5B Pentagon missile contract",
    "Steelmakers slide as China export glut weighs",
    "Lithium miners rally on EV battery demand revival",
    # Misc bullish / bearish flavor
    "Dow logs longest winning streak since 2017",
    "S&P 500 closes at fresh all-time high",
    "Nasdaq 100 enters correction territory after 10% drop",
    "VIX spikes above 30 on options expiry week",
    "Breadth weakens as fewer stocks lead the rally",
    "Small caps outperform on hopes for Fed easing",
    "Russell 2000 lags as financial conditions tighten",
    "Earnings recession fears recede, forward EPS revised up",
    "Profit warnings pile up across consumer discretionary",
    "Buyback authorizations top $1 trillion for 2025",
    # Data / events
    "Jackson Hole speech leaves market expectations unchanged",
    "Fed's Waller hints at multiple cuts in coming year",
    "Yellen warns on debt-ceiling brinkmanship risks",
    "OPEC delegates push back on production cut narrative",
    "China industrial production beats; copper extends gains",
    "Eurozone PMI surprises to the upside, EUR rallies",
    "UK CPI undershoots forecasts, gilts rally hard",
    "Japan core CPI matches BoJ's 2% target",
    # Tech / AI flavour
    "OpenAI announces new flagship model, NVDA jumps",
    "Microsoft reportedly cutting Azure capacity orders",
    "Apple delays Vision Pro 2 to 2026, supply chain hit",
    "Amazon launches generative AI assistant for sellers",
    "Meta open-sources Llama 4 with multimodal weights",
    # Risk-off / risk-on flavor
    "Risk-off trade returns as VIX surges 25%",
    "Equities snap losing streak as bond yields ease",
    "Defensive sectors lead market on growth concerns",
    "Cyclicals outperform as soft-landing bets build",
    "Junk-bond ETF JNK hits 52-week high",
    "Credit default swaps on US banks widen sharply",
    # More explicit bull / bear cases
    "Strong earnings beat lifts S&P futures pre-market",
    "Disappointing guidance triggers sharp sell-off",
    "Analysts upgrade NVDA with $1500 price target",
    "Goldman downgrades regional banks to underweight",
    "Morgan Stanley calls peak in dollar strength",
    "Merrill flags rising risk of credit crunch",
    "JPM raises year-end S&P target to 6500",
    "Bridgewater warns of stagflation regime",
]


def percentile(data: list[float], p: float) -> float:
    """Inclusive percentile, robust to small samples."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    if len(sorted_data) < 2:
        return float(sorted_data[0])
    # statistics.quantiles needs n>=2.
    return float(
        statistics.quantiles(sorted_data, n=100, method="inclusive")[int(p) - 1]
    )


def run_bench(
    onnx_path: str,
    tokenizer_path: str,
    repeats: int = 3,
    warmup: int = 5,
    max_len: int = 64,
    num_threads: int = 4,
) -> dict:
    """Time score() over the headline corpus and return a stats dict."""
    sentiment = FinBERTSentiment(
        onnx_path=onnx_path,
        tokenizer_path=tokenizer_path,
        max_len=max_len,
        num_threads=num_threads,
    )
    degraded = sentiment.degraded

    # Warmup also primes ORT optimization passes.
    for h in HEADLINES[:warmup]:
        sentiment.score(h)
    sentiment.cache_clear()

    timings_ms: list[float] = []
    for _ in range(repeats):
        sentiment.cache_clear()
        for h in HEADLINES:
            t0 = time.perf_counter()
            _ = sentiment.score(h)
            timings_ms.append((time.perf_counter() - t0) * 1000.0)

    sorted_ms = sorted(timings_ms)
    n = len(sorted_ms)
    result: dict = {
        "n": n,
        "p50_ms": round(percentile(sorted_ms, 50), 4),
        "p95_ms": round(percentile(sorted_ms, 95), 4),
        "p99_ms": round(percentile(sorted_ms, 99), 4),
        "max_ms": round(max(sorted_ms) if sorted_ms else 0.0, 4),
        "mean_ms": round(statistics.fmean(sorted_ms) if sorted_ms else 0.0, 4),
        "stdev_ms": round(statistics.pstdev(sorted_ms) if len(sorted_ms) > 1 else 0.0, 4),
        "max_len": max_len,
        "num_threads": num_threads,
        "onnx_path": sentiment.onnx_path,
        "tokenizer_path": sentiment.tokenizer_path,
        "host": socket.gethostname(),
        "date": date.today().isoformat(),
        "degraded": degraded,
    }
    if not degraded and os.path.isfile(sentiment.onnx_path):
        try:
            result["model_size_bytes"] = os.path.getsize(sentiment.onnx_path)
        except OSError:
            result["model_size_bytes"] = None
    else:
        result["model_size_bytes"] = None
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark FinBERTSentiment.score() over financial headlines."
    )
    parser.add_argument(
        "--onnx-path",
        default=str(PROJECT_ROOT / "models" / "finbert_onnx_int8"),
        help="Directory or file path for the ONNX model.",
    )
    parser.add_argument(
        "--tokenizer-path",
        default=str(PROJECT_ROOT / "models" / "finbert_onnx_int8"),
        help="Directory containing the HF tokenizer files.",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--max-len", type=int, default=64)
    parser.add_argument("--num-threads", type=int, default=4)
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "out" / "bench"),
    )
    args = parser.parse_args()

    result = run_bench(
        onnx_path=args.onnx_path,
        tokenizer_path=args.tokenizer_path,
        repeats=args.repeats,
        warmup=args.warmup,
        max_len=args.max_len,
        num_threads=args.num_threads,
    )

    os.makedirs(args.out_dir, exist_ok=True)
    out_path = os.path.join(
        args.out_dir,
        f"finbert_{result['host']}_{result['date']}.json",
    )
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))
    print(f"\n[bench] wrote {out_path}")

    if result["degraded"]:
        print("[bench] DEGRADED - real model not on disk (skipping acceptance gate)")
        return 0

    gate_pass = result["p50_ms"] <= 10.0 and result["p99_ms"] <= 50.0
    print(
        f"[bench] gate: p50={result['p50_ms']}ms p99={result['p99_ms']}ms "
        f"-> {'PASS' if gate_pass else 'FAIL'}"
    )
    return 0 if gate_pass else 2


if __name__ == "__main__":
    sys.exit(main())
