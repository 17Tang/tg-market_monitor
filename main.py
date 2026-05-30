import os
import datetime
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import shioaji as sj
import pandas as pd
from sqlalchemy import create_engine
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# 啟用日誌紀錄
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# ==================== 雲端環境變數設定 (安全版) ====================
# os.getenv() 只帶入變數名稱，它會自動去 Render 後台抓取對應的值
API_KEY = os.getenv("SHIOAJI_API_KEY")
SECRET_KEY = os.getenv("SHIOAJI_SECRET_KEY")
TG_TOKEN = os.getenv("TG_TOKEN")

# 讀取 Render 後台的資料庫網址，並防呆修正 postgres:// 的相容性問題
raw_db_url = os.getenv("DATABASE_URL")
if raw_db_url:
    DB_URL = raw_db_url.replace("postgres://", "postgresql://")
else:
    raise ValueError("❌ 錯誤：未在雲端後台設定 DATABASE_URL 環境變數！")

IMAGE_PATH = "realtime_trend.png"
engine = create_engine(DB_URL)
# ==================================================================

def fetch_and_save_to_db():
    """ 盤中每 5 分鐘執行的任務：從永豐金抓取並塞入資料庫 """
    now = datetime.datetime.now()
    
    # 檢查是否為台股開盤時間 (週一至週五 09:00 - 13:35)
    if now.weekday() >= 5 or not ("09:00" <= now.strftime("%H:%M") <= "13:35"):
        return

    logging.info("⏰ 觸發定時任務：開始抓取盤中數據...")
    try:
        api = sj.Shioaji()
        api.login(api_key=API_KEY, secret_key=SECRET_KEY)
        
        # 抓取上市櫃大盤快照
        snapshots = api.snapshots([api.Contracts.Stocks["001"], api.Contracts.Stocks["101"]])
        
        new_data = {
            "timestamp": [now.strftime("%Y-%m-%d %H:%M")],
            "tse_diff": [snapshots[0].up_count - snapshots[0].down_count],
            "otc_diff": [snapshots[1].up_count - snapshots[1].down_count]
        }
        df = pd.DataFrame(new_data)
        
        # 寫入 PostgreSQL 資料庫，若資料表不存在會自動建立，存在則自動附加(append)
        df.to_sql("market_status", engine, if_exists="append", index=False)
        logging.info("💾 數據已成功寫入雲端資料庫")
        api.logout()
    except Exception as e:
        logging.error(f"❌ 抓取或寫入資料庫失敗: {e}")

def draw_chart_from_db():
    """ 從資料庫讀取今日數據並畫圖 """
    try:
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        # SQL 語法：只撈取今天的數據
        query = f"SELECT * FROM market_status WHERE timestamp LIKE '{today_str}%' ORDER BY timestamp ASC"
        df = pd.read_sql(query, engine)
        
        if df.empty:
            return False
            
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        # 開始繪圖
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
        plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial'] # 雲端 Linux 通常沒新細明體，用預設字體避免崩潰
        plt.rcParams['axes.unicode_minus'] = False 

        # 上市
        tse_colors = ['red' if val >= 0 else 'green' for val in df['tse_diff']]
        ax1.bar(df['timestamp'], df['tse_diff'], color=tse_colors, width=0.003)
        ax1.set_title("TWSE Market Width (TSE Diff)", fontsize=14)
        ax1.axhline(0, color='gray', linewidth=0.8, linestyle='--')
        ax1.grid(True, alpha=0.3)

        # 上櫃
        otc_colors = ['red' if val >= 0 else 'green' for val in df['otc_diff']]
        ax2.bar(df['timestamp'], df['otc_diff'], color=otc_colors, width=0.003)
        ax2.set_title("TPEx Market Width (OTC Diff)", fontsize=14)
        ax2.axhline(0, color='gray', linewidth=0.8, linestyle='--')
        ax2.grid(True, alpha=0.3)

        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        fig.autofmt_xdate()
        
        plt.tight_layout()
        plt.savefig(IMAGE_PATH, dpi=150)
        plt.close()
        return df.iloc[-1] # 回傳最新一筆資料數據
    except Exception as e:
        logging.error(f"繪圖失敗: {e}")
        return None

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當收到 /check 指令時回應 """
    await update.message.reply_text("⏳ 正在從雲端資料庫撈取最新數據並即時繪圖...")
    
    latest_row = draw_chart_from_db()
    if latest_row is False:
        await update.message.reply_text("❌ 資料庫中目前還沒有今天的數據喔！")
    elif latest_row is None:
        await update.message.reply_text("❌ 讀取資料庫或繪圖時發生非預期錯誤。")
    else:
        # 成功，發送照片
        time_str = pd.to_datetime(latest_row['timestamp']).strftime('%H:%M')
        caption_text = f"📊 即時雲端監測 ({time_str})\n🏛 上市家數差: {latest_row['tse_diff']:+d}\n🏢 上櫃家數差: {latest_row['otc_diff']:+d}"
        with open(IMAGE_PATH, 'rb') as photo:
            await update.message.reply_photo(photo=photo, caption=caption_text)

def dummy_webhook_service():
    """ 建立一個超簡單的 HTTP 網頁，用來滿足 Render 免費版對 Web Service 的連線要求 """
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
    # 1. 啟動排程器：每 5 分鐘自動執行一次 fetch_and_save_to_db
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_save_to_db, 'cron', minute='*/5')
    scheduler.start()
    
    # 2. 設定 Telegram 監聽
    application = Application.builder().token(TG_TOKEN).build()
    application.add_handler(CommandHandler("check", check_command))
    
    # 3. 在背景執行 Telegram 監聽
    import threading
    bot_thread = threading.Thread(target=application.run_polling, daemon=True)
    bot_thread.start()
    
    # 4. 主執行緒拿來跑網頁服務，防止 Render 判定服務失效
    logging.info("🚀 雲端伺服器與監聽系統正式上線...")
    dummy_webhook_service()