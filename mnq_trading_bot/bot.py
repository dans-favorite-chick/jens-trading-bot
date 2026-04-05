import os
import yfinance as yf
import pandas as pd
import requests
from dotenv import load_dotenv
from main import trading_brain  
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
from truthbrush import Api 

# Load environment variables
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
FINNHUB_KEY = os.getenv("FINNHUB_API_KEY")
TRUTH_USER = os.getenv("TRUTH_SOCIAL_USERNAME")
TRUTH_PASS = os.getenv("TRUTH_SOCIAL_PASSWORD")
TRUTH_HANDLE = os.getenv("TRUTH_SOCIAL_HANDLE") # realDonaldTrump

analyzer = SentimentIntensityAnalyzer()

# --- THE POLITICIAN (Live Trump Feed) ---
def get_trump_sentiment():
    try:
        api = Api(TRUTH_USER, TRUTH_PASS)
        # Pull last 3 posts from the handle in your .env
        statuses = api.pull_statuses(TRUTH_HANDLE, limit=3)
        combined_text = " ".join([s['content'] for s in statuses])
        
        keywords = ["tariff", "interest rate", "inflation", "war", "oil", "straight"]
        found = [w for w in keywords if w in combined_text.lower()]
        score = analyzer.polarity_scores(combined_text)['compound']
        return score, found
    except Exception as e:
        print(f"Truth Social Error: {e}")
        return 0, []

# --- MACRO ANALYST (Finnhub News) ---
def get_market_news():
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
        response = requests.get(url).json()
        headlines = [n['headline'] for n in response[:5]]
        combined_news = " ".join(headlines)
        score = analyzer.polarity_scores(combined_news)['compound']
        return score, headlines[0]
    except:
        return 0, "No news available"

async def council_meeting(context: ContextTypes.DEFAULT_TYPE):
    # 1. Scrape Real Data
    data = yf.download("NQ=F", period="1d", interval="5m")
    if data.empty: return
    
    market_raw = data.tail(3).to_string() 
    trump_score, trump_keywords = get_trump_sentiment()
    news_raw = f"Trump keywords: {trump_keywords}. Sentiment: {trump_score}"

    # 2. CALL THE BRAIN (main.py)
    result = trading_brain.invoke({
        "symbol": "MNQ",
        "market_context": market_raw,
        "news_context": news_raw,
        "log": []
    })

    # 3. Handle Decision
    if result["decision"] == "EXECUTE":
        report = "🏛 **COUNCIL ALERT** 🏛\n" + "\n".join(result["log"])
        keyboard = [[InlineKeyboardButton("✅ INITIATE", callback_data='buy'),
                     InlineKeyboardButton("❌ REJECT", callback_data='ignore')]]
        await context.bot.send_message(chat_id=CHAT_ID, text=report, 
                                       reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def handle_response(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    result = "APPROVED" if query.data == "buy" else "REJECTED"
    # Logic for Student Bot to save this result goes here
    await query.edit_message_text(text=f"Decision Logged: **{result}**.")

if __name__ == '__main__':
    application = Application.builder().token(TOKEN).build()
    
    # --- 10-SECOND WATCHER ---
    application.job_queue.run_repeating(council_meeting, interval=10, first=1)
    
    application.add_handler(CallbackQueryHandler(handle_response))
    print("The Council is watching every 10 seconds...")
    application.run_polling()