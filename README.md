# 🏛️ MNQ Council of Seven (v5.0 - Final)

A multi-agent institutional trading system for Micro E-mini Nasdaq-100 (MNQ) utilizing [LangGraph](https://github.com/langchain-ai/langgraph) and [Gemini 1.5 Pro](https://ai.google.dev/).

## ⚖️ The Seven Agents
1. **Tech Bot**: Analyzes Triple Timeframe & MenthorQ Gamma Levels.
2. **Macro Bot**: Monitors News & Politics (CPI, FOMC, etc.).
3. **Psychologist Bot**: Identifies Retail Traps and Sentiment Extremes.
4. **Tape Reader Bot**: Analyzes Level 2 Order Flow and Delta.
5. **Safety Officer**: Enforces hard risk limits ($20 SL / $50 Daily Stop).
6. **Student Bot**: RL Engine for long-term strategy optimization.
7. **Shield Bot**: Manages Options Hedging and protective puts.

## 📐 Ground Truth Rules
- **Risk:** $20.00 Max per trade.
- **Stop:** $50.00 Daily Drawdown (System Shutdown).
- **Setup:** A+ Trades only (Engulfing + Retest on lower volume).
- **Execution:** Limit Orders only (No Market Orders).

### Setup
1. Add your `GOOGLE_API_KEY` to a `.env` file.
2. Install dependencies: `pip install langchain-google-genai langgraph python-dotenv`
3. Run the bot: `python main.py`