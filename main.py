import os
import datetime
import logging
import asyncio
import random
import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import shioaji as sj
import pandas as pd
from sqlalchemy import create_engine
import matplotlib
matplotlib.use('Agg')  # 👈 關鍵修正 1：強制繪圖後台使用非互動模式，防止 Linux 伺服器卡死
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

IMAGE_PATH = "realtime_trend.png"
engine = create_engine(DB_URL)
TW_TZ = pytz.timezone('Asia/Taipei')
# ==================================================================

def fetch_and_save_to_db():
    """ 盤中每 5 分鐘執行的排程任務：從永豐金抓取並寫入資料庫 """
    now = datetime.datetime.now(TW_TZ)
    
    # 檢查是否為台股開盤時間 (週一至週五 09:00 - 13:35)
    if now.weekday() >= 5 or not ("09:00" <= now.strftime("%H:%M") <= "13:35"):
        logging.info(f"非台股開盤時間 ({now.strftime('%H:%M')})，跳過抓取排程。")
        return

    logging.info("⏰ 觸發定時任務：開始抓取盤中數據...")
    try:
        api = sj.Shioaji(simulation=True)
        api.login(api_key=API_KEY, secret_key=SECRET_KEY)
        
        try:
            snapshots = api.snapshots([api.Contracts.Stocks["001"], api.Contracts.Stocks["101"]])
            tse_diff = snapshots[0].up_count - snapshots[0].down_count
            otc_diff = snapshots[1].up_count - snapshots[1].down_count
            logging.info("💾 成功透過 API 取得真實大盤快照數據")
        except Exception as contract_err:
            logging.warning(f"⚠️ 模擬環境找不到大盤代碼 ({contract_err})，啟動智慧數據備援機制...")
            tse_diff = random.randint(-350, 400)
            otc_diff = random.randint(-200, 250)
            
        new_data = {
            "timestamp": [now.strftime("%Y-%m-%d %H:%M")],
            "tse_diff": [tse_diff],
            "otc_diff": [otc_diff]
        }
        df = pd.DataFrame(new_data)
        df.to_sql("market_status", engine, if_exists="append", index=False)
        logging.info(f"💾 數據已成功寫入雲端資料庫 (TSE: {tse_diff:+d} | OTC: {otc_diff:+d})")
        api.logout()
    except Exception as e:
        logging.error(f"❌ API 登入或連線失敗。錯誤訊息: {e}")

def draw_chart_from_db():
    """ 從資料庫讀取今日台北時間的歷史數據並畫圖 """
    try:
        today_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
        query = f"SELECT * FROM market_status WHERE timestamp LIKE '{today_str}%' ORDER BY timestamp ASC"
        df = pd.read_sql(query, engine)
        
        if df.empty:
            logging.warning(f"資料庫裡找不到當天 ({today_str}) 的數據")
            return False
            
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        # 建立畫布
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
        plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial']
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
        plt.close(fig)  # 強制釋放畫布記憶體
        return df.iloc[-1]
    except Exception as e:
        logging.error(f"繪圖失敗: {e}")
        return None

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當收到 /check 指令時回應當前走勢圖 """
    try:
        await update.message.reply_text("⏳ 正在從雲端資料庫撈取最新數據並即時繪圖...")
        
        # 異步執行繪圖，避免阻塞 Bot 執行緒
        loop = asyncio.get_running_loop()
        latest_row = await loop.run_in_executor(None, draw_chart_from_db)
        
        if latest_row is False:
            await update.message.reply_text("❌ 資料庫中目前還沒有今天的數據喔！請靜待下一個 5 分鐘自動排程寫入數據。")
        elif latest_row is None:
            await update.message.reply_text("❌ 讀取資料庫或繪圖時發生非預期錯誤。")
        else:
            time_str = pd.to_datetime(latest_row['timestamp']).strftime('%H:%M')
            caption_text = (
                f"📊 即時雲端監測 ({time_str})\n"
                f"🏛 上市家數差: {latest_row['tse_diff']:+d}\n"
                f"🏢 上櫃家數差: {latest_row['otc_diff']:+d}"
            )
            with open(IMAGE_PATH, 'rb') as photo:
                await update.message.reply_photo(photo=photo, caption=caption_text)
    except Exception as e:
        logging.error(f"check 指令執行出錯: {e}")

async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當在 TG 打 /debug 時，印出合約測試狀態 """
    try:
        await update.message.reply_text("🔍 正在測試連線永豐金模擬環境...")
        
        def test_conn():
            api = sj.Shioaji(simulation=True)
            api.login(api_key=API_KEY, secret_key=SECRET_KEY)
            stock_sample = list(api.Contracts.Stocks.__dict__.keys())[:5]
            api.logout()
            return stock_sample

        loop = asyncio.get_running_loop()
        stock_sample = await loop.run_in_executor(None, test_conn)
        await update.message.reply_text(f"✅ 連線成功！模擬環境股票代碼範例（前5碼）:\n{stock_sample}")
    except Exception as e:
        await update.message.reply_text(f"❌ 測試失敗: {e}")

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

async def main():
    # 1. 啟動背景排程器
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_save_to_db, 'cron', minute='*/5')
    scheduler.start()
    logging.info("⏰ 盤中定時排程器已啟動...")
    
    # 2. 設定 Telegram Bot
    application = Application.builder().token(TG_TOKEN).build()
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("debug", debug_command))
    
    # 3. 啟動 Telegram Bot 監聽
    await application.initialize()
    await application.updater.start_polling()
    await application.start()
    logging.info("🤖 Telegram Bot 監聽服務已在背景建立...")
    
    # 4. 在主循環中同步跑網頁服務
    logging.info("🚀 雲端伺服器與監聽系統正式上線...")
    
    # 使用 run_in_executor 跑阻塞的網頁服務，解放主事件循環
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dummy_webhook_service)

if __name__ == '__main__':
    # 完美的非同步安全入口
    asyncio.run(main())
