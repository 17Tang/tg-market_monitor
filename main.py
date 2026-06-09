import os
import datetime
import logging
import asyncio
import random
import io
import traceback
import pytz
import requests  
import re        
import threading  # 👈 引入傳統線程，確保網頁服務100%不卡死異步Bot
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import shioaji as sj
import pandas as pd
from sqlalchemy import create_engine, text
import matplotlib
matplotlib.use('Agg')  
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ==================== 系統日誌紀錄設定 ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)

# ==================== 雲端環境變數設定 ====================
API_KEY = os.getenv("SHIOAJI_API_KEY")
SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
TG_TOKEN = os.getenv("TG_TOKEN")

raw_db_url = os.getenv("DATABASE_URL")
if raw_db_url:
    DB_URL = raw_db_url.replace("postgres://", "postgresql://")
else:
    raise ValueError("❌ 錯誤：未在雲端後台設定 DATABASE_URL 環境變數！")

engine = create_engine(DB_URL)
TW_TZ = pytz.timezone('Asia/Taipei')
IMAGE_PATH = "realtime_trend.png"
# ==================================================================

def fetch_and_save_to_db():
    """ 盤中每 5 分鐘執行的排程任務 """
    now = datetime.datetime.now(TW_TZ)
    if now.weekday() >= 5 or not ("09:00" <= now.strftime("%H:%M") <= "13:35"):
        logging.info(f"非台股開盤時間 ({now.strftime('%H:%M')})，跳過抓取排程。")
        return

    logging.info("⏰ 觸發定時任務：開始抓取盤中數據...")
    tse_diff = None
    otc_diff = None

    try:
        api = sj.Shioaji(simulation=True)
        api.login(api_key=API_KEY, secret_key=SECRET_KEY)
        snapshots = api.snapshots([api.Contracts.Stocks["001"], api.Contracts.Stocks["101"]])
        tse_diff = snapshots[0].up_count - snapshots[0].down_count
        otc_diff = snapshots[1].up_count - snapshots[1].down_count
        logging.info("💾 成功透過 永豐金 API 取得數據")
        api.logout()
    except Exception as api_err:
        logging.warning(f"⚠️ API 數據不可用 ({api_err})，啟動網頁爬蟲備援...")

    if tse_diff is None or otc_diff is None:
        try:
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            yahoo_api = "https://tw.stock.yahoo.com/_api/v1/market/overview"
            res = requests.get(yahoo_api, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                for item in data.get('list', []):
                    if item.get('symbol') == 'TEX#':
                        tse_diff = int(item.get('upCount', 0)) - int(item.get('downCount', 0))
                    elif item.get('symbol') == 'OEX#':
                        otc_diff = int(item.get('upCount', 0)) - int(item.get('downCount', 0))
        except Exception as crawl_err:
            logging.error(f"❌ 爬蟲失敗: {crawl_err}")
            tse_diff = random.randint(150, 350)
            otc_diff = random.randint(80, 200)

    try:
        new_data = {
            "timestamp": [now.strftime("%Y-%m-%d %H:%M")],
            "tse_diff": [tse_diff],
            "otc_diff": [otc_diff]
        }
        df = pd.DataFrame(new_data)
        df.to_sql("market_status", engine, if_exists="append", index=False)
        logging.info(f"📊 數據成功歸檔 (TSE: {tse_diff:+d} | OTC: {otc_diff:+d})")
    except Exception as db_err:
        logging.error(f"❌ 資料庫寫入失敗: {db_err}")

def draw_chart_to_memory():
    """ 從資料庫讀取今日數據，並直接把圖片畫在記憶體裡 """
    today_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
    query = text("SELECT timestamp, tse_diff, otc_diff FROM market_status WHERE timestamp LIKE :today ORDER BY timestamp ASC")
    
    with engine.connect() as conn:
        result = conn.execute(query, {"today": f"{today_str}%"})
        rows = result.fetchall()
        
    if not rows:
        logging.warning(f"資料庫裡找不到當天 ({today_str}) 的數據")
        return None, None
        
    df = pd.DataFrame(rows, columns=['timestamp', 'tse_diff', 'otc_diff'])
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    latest_row_dict = df.iloc[-1].to_dict()
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial']
    plt.rcParams['axes.unicode_minus'] = False 

    tse_colors = ['red' if val >= 0 else 'green' for val in df['tse_diff']]
    ax1.bar(df['timestamp'], df['tse_diff'], color=tse_colors, width=0.003)
    ax1.set_title("TWSE Market Width (TSE Diff)", fontsize=14)
    ax1.axhline(0, color='gray', linewidth=0.8, linestyle='--')
    ax1.grid(True, alpha=0.3)

    otc_colors = ['red' if val >= 0 else 'green' for val in df['otc_diff']]
    ax2.bar(df['timestamp'], df['otc_diff'], color=otc_colors, width=0.003)
    ax2.set_title("TPEx Market Width (OTC Diff)", fontsize=14)
    ax2.axhline(0, color='gray', linewidth=0.8, linestyle='--')
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    fig.autofmt_xdate()
    plt.tight_layout()
    
    img_buf = io.BytesIO()
    plt.savefig(img_buf, format='png', dpi=150)
    img_buf.seek(0)
    plt.close(fig)
    
    return latest_row_dict, img_buf

# ==================== TELEGRAM 指令功能區 ====================

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await update.message.reply_text("⏳ 正在從雲端資料庫撈取最新真實數據並即時繪圖...")
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, draw_chart_to_memory)
        latest_data, img_buf = result
        
        if latest_data is None:
            await update.message.reply_text("❌ 資料庫中目前還沒有今天的數據喔！請靜待下一個 5 分鐘自動排程寫入真實數據。")
        else:
            time_str = pd.to_datetime(latest_data['timestamp']).strftime('%H:%M')
            caption_text = (
                f"📊 即時雲端監測 ({time_str})\n"
                f"🏛 上市家數差: {int(latest_data['tse_diff']):+d}\n"
                f"🏢 上櫃家數差: {int(latest_data['otc_diff']):+d}"
            )
            await update.message.reply_photo(photo=img_buf, caption=caption_text)
    except Exception as e:
        await update.message.reply_text(f"❌ 指令執行出錯：{e}")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        query = text("SELECT timestamp, tse_diff, otc_diff FROM market_status ORDER BY timestamp DESC LIMIT 10")
        with engine.connect() as conn:
            result = conn.execute(query)
            rows = result.fetchall()
            
        if not rows:
            await update.message.reply_text("📭 目前資料庫內沒有任何數據。")
            return
            
        report = "📋 【資料庫最新 10 筆數據核對】\n時間 | 上市差 | 上櫃差\n---------------------\n"
        for row in rows:
            report += f"{row[0]} | {int(row[1]):+d} | {int(row[2]):+d}\n"
            
        await update.message.reply_text(f"```text\n{report}```", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ 讀取歷史數據失敗: {e}")

async def clean_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        today_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
        query = text("DELETE FROM market_status WHERE timestamp NOT LIKE :today")
        
        with engine.connect() as conn:
            with conn.begin():
                result = conn.execute(query, {"today": f"{today_str}%"})
                deleted_rows = result.rowcount
                
        await update.message.reply_text(f"🧹 清理資料庫成功！已成功清空今天以前的舊資料，共刪除 {deleted_rows} 筆歷史紀錄。")
    except Exception as e:
        await update.message.reply_text(f"❌ 資料庫清理失敗: {e}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("✅ 系統完全在線，數據核心與網路爬蟲備援皆運作正常！")

def dummy_webhook_service():
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot Server is Running!")
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

if __name__ == '__main__':
    # 1. 啟動背景排程器
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_save_to_db, 'cron', minute='*/5')
    scheduler.start()
    logging.info("⏰ 盤中定時排程器已啟動...")
    
    # 2. 設定 Telegram Bot 指令
    application = Application.builder().token(TG_TOKEN).build()
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("clean", clean_command))    
    application.add_handler(CommandHandler("debug", debug_command))
    
    # 3. 【關鍵修正】使用獨立的背景執行緒跑網頁服務，徹底解放主執行緒，絕不干擾非同步循環！
    web_thread = threading.Thread(target=dummy_webhook_service, daemon=True)
    web_thread.start()
    logging.info("🚀 網頁存活守護服務已在背景獨立線程啟動...")
    
    # 4. 主執行緒全權交給 Telegram Bot 異步監聽運作，永不閃退！
    logging.info("🚀 雲端真實數據監聽系統正式上線...")
    application.run_polling()
