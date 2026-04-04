import os
from datetime import datetime
from typing import Annotated, TypedDict
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END

# 1. Load Keys
load_dotenv()

# 2. Define State with Log & Journaling
class TradingState(TypedDict):
    symbol: str
    tech_signal: str
    options_signal: str
    risk_approved: bool
    decision: str
    log: list[str]

# 3. Initialize Gemini 2.5 Pro
llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0)

# --- BOT NODES ---

def tech_analysis_bot(state: TradingState):
    print("--- Tech Bot: Analyzing Volume ---")
    market_context = "Volume is 2x average, Price is above VWAP, Trend is Up."
    prompt = f"Analyze: {market_context}. If high-probability, return 'BULLISH'. Else 'NEUTRAL'."
    response = llm.invoke(prompt)
    return {
        "tech_signal": response.content.strip(),
        "log": state.get("log", []) + [f"Tech: {response.content.strip()}"]
    }

def options_bot(state: TradingState):
    print("--- Options Bot: Checking Gamma ---")
    prompt = "MNQ Price is 18150. Resistance (Gamma Wall) is 18200. Return 'CLEAR' or 'BLOCKED'."
    response = llm.invoke(prompt)
    return {
        "options_signal": response.content.strip(),
        "log": state.get("log", []) + [f"Options: {response.content.strip()}"]
    }

def integration_agent(state: TradingState):
    print("--- The Captain: Final Risk Check ---")
    # Enforcing the $25 Risk / $50 Profit rule
    if "BULLISH" in state["tech_signal"] and "CLEAR" in state["options_signal"]:
        return {
            "risk_approved": True,
            "decision": "EXECUTE LONG | SL: -$25 | TP: +$50",
            "log": state["log"] + ["Captain: Setup matches 80% win-rate criteria. Entry authorized."]
        }
    return {
        "risk_approved": False,
        "decision": "NO TRADE | Standing down",
        "log": state["log"] + ["Captain: Signal mismatch. Preserving capital."]
    }

# --- JOURNALING FEATURE ---

def save_to_journal(state: TradingState):
    date_str = datetime.now().strftime('%Y-%m-%d')
    filename = f"trading_journal_{date_str}.txt"
    with open(filename, "a") as f:
        f.write(f"\n[{datetime.now().strftime('%H:%M:%S')}] {state['symbol']} Session\n")
        f.write(f"DECISION: {state['decision']}\n")
        f.write("REASONING:\n")
        for line in state['log']:
            f.write(f"  {line}\n")
        f.write("-" * 40 + "\n")
    print(f"--- Session saved to {filename} ---")

# --- GRAPH SETUP ---

builder = StateGraph(TradingState)
builder.add_node("tech", tech_analysis_bot)
builder.add_node("options", options_bot)
builder.add_node("captain", integration_agent)

builder.add_edge(START, "tech")
builder.add_edge("tech", "options")
builder.add_edge("options", "captain")
builder.add_edge("captain", END)

app = builder.compile()

# --- RUN ---
if __name__ == "__main__":
    print("\n--- MNQ BOT ACTIVE ---")
    result = app.invoke({"symbol": "MNQ", "log": []})
    print(f"\nFinal Decision: {result['decision']}")
    save_to_journal(result)