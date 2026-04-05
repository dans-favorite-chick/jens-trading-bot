import os
import operator
from datetime import datetime
from typing import Annotated, TypedDict, List
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import StateGraph, START, END

load_dotenv()

class TradingState(TypedDict):
    symbol: str
    market_context: str  
    news_context: str    
    tech_signal: str
    macro_signal: str
    risk_status: str
    decision: str
    log: Annotated[List[str], operator.add]

# Bot using Gemini 1.5 Flash (Optimized for 10-sec speed)
llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0)

# --- THE COUNCIL NODES ---
def tech_bot(state: TradingState):
    prompt = f"Data: {state['market_context']}. Look for Engulfing + Retest. Return 'BULLISH' or 'WAIT'."
    res = llm.invoke(prompt).content.strip()
    return {"tech_signal": res.replace("*", ""), "log": [f"Tech: {res}"]}

def macro_bot(state: TradingState):
    prompt = f"News: {state['news_context']}. Trump triggers or CPI? Return 'STABLE' or 'SIT OUT'."
    res = llm.invoke(prompt).content.strip()
    return {"macro_signal": res.replace("*", ""), "log": [f"Macro: {res}"]}

def integration_captain(state: TradingState):
    # Only execute if both key advisors are green
    if "BULLISH" in state["tech_signal"].upper() and "STABLE" in state["macro_signal"].upper():
        return {"decision": "EXECUTE", "log": ["Captain: ALL SYSTEMS GO."]}
    return {"decision": "IDLE", "log": ["Captain: STANDING DOWN."]}

# --- GRAPH ASSEMBLY ---
builder = StateGraph(TradingState)
builder.add_node("tech", tech_bot)
builder.add_node("macro", macro_bot)
builder.add_node("captain", integration_captain)

builder.add_edge(START, "tech")
builder.add_edge(START, "macro")
builder.add_edge("tech", "captain")
builder.add_edge("macro", "captain")
builder.add_edge("captain", END)

trading_brain = builder.compile()