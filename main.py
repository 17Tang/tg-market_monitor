import os
import datetime
import logging
import asyncio
import threading
import pytz  # 時區處理套件
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import shioaji as sj
import pandas as pd
from sqlalchemy import create_engine
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

# 讀取 Render 後台的資料庫網址，並修正 postgres:// 的相容性問題
raw_db_url = os.getenv("DATABASE_URL")
if raw_db_url:
    DB_URL = raw_db_url.replace("postgres://", "postgresql://")
else:
    raise ValueError("❌ 錯誤：未在雲端後台設定 DATABASE_URL 環境變數！")

IMAGE_PATH = "realtime_trend.png"
engine = create_engine(DB_URL)

# 強制設定為台北時區
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
        api = sj.Shioaji()
        # simulation=True 開啟模擬環境連線，避開正式環境 Production 權限限制
        api.login(api_key=API_KEY, secret_key=SECRET_KEY, simulation=True)
        
        # 抓取上市櫃大盤快照 (001 為上市大盤，101 為上櫃大盤)
        snapshots = api.snapshots([api.Contracts.Stocks["001"], api.Contracts.Stocks["101"]])
        
        new_data = {
            "timestamp": [now.strftime("%Y-%m-%d %H:%M")],
            "tse_diff": [snapshots[0].up_count - snapshots[0].down_count],
            "otc_diff": [snapshots[1].up_count - snapshots[1].down_count]
        }
        df = pd.DataFrame(new_data)
        
        # 寫入 PostgreSQL 資料庫中名為 market_status 的資料表
        df.to_sql("market_status", engine, if_exists="append", index=False)
        logging.info("💾 數據已成功寫入雲端資料庫")
        api.logout()
    except Exception as e:
        logging.error(f"❌ 抓取或寫入資料庫失敗。錯誤訊息: {e}")


def draw_chart_from_db():
    """ 從資料庫讀取今日台北時間的歷史數據並畫圖 """
    try:
        # 只撈取台北時間當天的數據
        today_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
        query = f"SELECT * FROM market_status WHERE timestamp LIKE '{today_str}%' ORDER BY timestamp ASC"
        df = pd.read_sql(query, engine)
        
        if df.empty:
            logging.warning(f"資料庫裡找不到當天 ({today_str}) 的數據")
            return False
            
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        
        # 開始繪製上下雙層圖表
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
        plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial']  # 雲端 Linux 預設通用字體
        plt.rcParams['axes.unicode_minus'] = False 

        # --- 上市大盤繪圖 ---
        tse_colors = ['red' if val >= 0 else 'green' for val in df['tse_diff']]
        ax1.bar(df['timestamp'], df['tse_diff'], color=tse_colors, width=0.003)
        ax1.set_title("TWSE Market Width (TSE Diff)", fontsize=14)
        ax1.axhline(0, color='gray', linewidth=0.8, linestyle='--')
        ax1.grid(True, alpha=0.3)

        # --- 上櫃大盤繪圖 ---
        otc_colors = ['red' if val >= 0 else 'green' for val in df['otc_diff']]
        ax2.bar(df['timestamp'], df['otc_diff'], color=otc_colors, width=0.003)
        ax2.set_title("TPEx Market Width (OTC Diff)", fontsize=14)
        ax2.axhline(0, color='gray', linewidth=0.8, linestyle='--')
        ax2.grid(True, alpha=0.3)

        # 時間軸 X 軸格式化 (顯示時:分)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        fig.autofmt_xdate()
        
        plt.tight_layout()
        plt.savefig(IMAGE_PATH, dpi=150)
        plt.close()
        return df.iloc[-1]  # 回傳當天最新的一筆數據紀錄
    except Exception as e:
        logging.error(f"繪圖失敗: {e}")
        return None


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當收到 /check 指令時回應當前走勢圖 """
    await update.message.reply_text("⏳ 正在從雲端資料庫撈取最新數據並即時繪圖...")
    
    latest_row = draw_chart_from_db()
    if latest_row is False:
        await update.message.reply_text("❌ 資料庫中目前還沒有今天的數據喔！(可能前幾小時的自動排程因未到盤中或發生錯誤未成功寫入)")
    elif latest_row is None:
        await update.message.reply_text("❌ 讀取資料庫或繪圖時發生非預期錯誤。")
    else:
        # 成功繪圖，發送照片至 Telegram
        time_str = pd.to_datetime(latest_row['timestamp']).strftime('%H:%M')
        caption_text = (
            f"📊 即時雲端監測 ({time_str})\n"
            f"🏛 上市家數差: {latest_row['tse_diff']:+d}\n"
            f"🏢 上櫃家數差: {latest_row['otc_diff']:+d}"
        )
        with open(IMAGE_PATH, 'rb') as photo:
            await update.message.reply_photo(photo=photo, caption=caption_text)


async def debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當在 TG 打 /debug 時，不經資料庫，直接連線永豐金印出此時大盤原始數據 """
    await update.message.reply_text("🔍 正在即時連線永豐金（模擬環境）抓取原始大盤快照...")
    
    try:
        api = sj.Shioaji()
        api.login(api_key=API_KEY, secret_key=SECRET_KEY, simulation=True)
        
        tse_contract = api.Contracts.Stocks["001"]
        otc_contract = api.Contracts.Stocks["101"]
        snapshots = api.snapshots([tse_contract, otc_contract])
        api.logout()
        
        # 解析上市原始數據
        tse = snapshots[0]
        tse_text = (
            f"🏛️ 【上市大盤快照原始數據】\n"
            f"• 商品代碼: {tse.code}\n"
            f"• 總上漲家數 (up_count): {tse.up_count}\n"
            f"• 總下跌家數 (down_count): {tse.down_count}\n"
            f"• 漲停家數 (up_limit_count): {tse.up_limit_count}\n"
            f"• 跌停家數 (down_limit_count): {tse.down_limit_count}\n"
            f"• 平盤家數 (same_count): {tse.same_count}\n"
            f"• 差值 (上漲-下跌): {tse.up_count - tse.down_count}\n"
            f"• 最新成交價 (close): {tse.close}\n"
        )
        
        # 解析上櫃原始數據
        otc = snapshots[1]
        otc_text = (
            f"🏢 【上櫃大盤快照原始數據】\n"
            f"• 商品代碼: {otc.code}\n"
            f"• 總上漲家數 (up_count): {otc.up_count}\n"
            f"• 總下跌家數 (down_count): {otc.down_count}\n"
            f"• 漲停家數 (up_limit_count): {otc.up_limit_count}\n"
            f"• 跌停家數 (down_limit_count): {otc.down_limit_count}\n"
            f"• 平盤家數 (same_count): {otc.same_count}\n"
            f"• 差值 (上漲-下跌): {otc.up_count - otc.down_count}\n"
            f"• 最新成交價 (close): {otc.close}\n"
        )
        
        await update.message.reply_text(tse_text + "\n" + otc_text)
        
    except Exception as e:
        await update.message.reply_text(f"❌ 連線或讀取原始資料失敗！錯誤訊息:\n{e}")


def dummy_webhook_service():
    """ 建立輕量網頁服務，用來防範 Render 免費版 Web Service 因無流量而被關閉 """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Bot Server is Running!")
    
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()


async def start_bot_async(application):
    """ 負責在正確的異步循環中啟動 Telegram Bot """
    await application.initialize()
    await application.updater.start_polling()
    await application.start()
    logging.info("🤖 Telegram Bot 監聽服務已在背景建立...")

if __name__ == '__main__':
    # 1. 啟動背景排程器（每 5 分鐘執行一次資料採集任務）
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_save_to_db, 'cron', minute='*/5')
    scheduler.start()
    logging.info("⏰ 盤中定時排程器已啟動...")
    
    # 2. 設定 Telegram Bot 指令處理器
    application = Application.builder().token(TG_TOKEN).build()
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("debug", debug_command))
    
    # 3. 使用最新標準的 asyncio 機制在主執行緒中安全啟動 Bot
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_bot_async(application))
    except Exception as e:
        logging.error(f"❌ 啟動 Telegram 監聽時發生錯誤: {e}")
    
    # 4. 主執行緒運行網頁服務，防止 Render 判定服務失效
    logging.info("🚀 雲端伺服器與監聽系統正式上線...")
    dummy_webhook_service()
