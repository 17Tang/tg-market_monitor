import os
import datetime
import logging
import asyncio
import traceback
import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from apscheduler.schedulers.background import BackgroundScheduler
import shioaji as sj
import pandas as pd
from sqlalchemy import create_engine, text
import matplotlib
matplotlib.use('Agg')  # 強制非互動模式，Linux 伺服器專用
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
    """ 盤中每 5 分鐘執行的排程任務：從永豐金正式環境抓取大盤快照 """
    now = datetime.datetime.now(TW_TZ)
    
    if now.weekday() >= 5 or not ("09:00" <= now.strftime("%H:%M") <= "13:35"):
        logging.info(f"非台股開盤時間 ({now.strftime('%H:%M')})，跳過抓取排程。")
        return

    logging.info("⏰ 觸發定時任務：開始抓取盤中數據...")
    try:
        # 關鍵修正：徹底移除 simulation=True，直接連線永豐金正式生產環境！
        api = sj.Shioaji()
        api.login(api_key=API_KEY, secret_key=SECRET_KEY)
        
        # 正式環境百分之百支援 001(上市) 與 101(上櫃) 大盤合約
        snapshots = api.snapshots([api.Contracts.Stocks["001"], api.Contracts.Stocks["101"]])
        tse_diff = snapshots[0].up_count - snapshots[0].down_count
        otc_diff = snapshots[1].up_count - snapshots[1].down_count
        
        new_data = {
            "timestamp": [now.strftime("%Y-%m-%d %H:%M")],
            "tse_diff": [tse_diff],
            "otc_diff": [otc_diff]
        }
        df = pd.DataFrame(new_data)
        df.to_sql("market_status", engine, if_exists="append", index=False)
        logging.info(f"📊 【正式數據】寫入成功 (TSE: {tse_diff:+d} | OTC: {otc_diff:+d})")
        api.logout()
    except Exception as e:
        logging.error(f"❌ 永豐金正式環境連線或抓取失敗。錯誤訊息: {e}")

def draw_chart_to_memory():
    """ 從資料庫讀取今日數據，並直接把圖片畫在記憶體裡（原生 SQL 安全版） """
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
    
    # 轉成純字典傳遞，避開 Pandas 歧義檢查
    latest_row_dict = df.iloc[-1].to_dict()
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8), sharex=True)
    plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial']
    plt.rcParams['axes.unicode_minus'] = False 

    # 上市圖表
    tse_colors = ['red' if val >= 0 else 'green' for val in df['tse_diff']]
    ax1.bar(df['timestamp'], df['tse_diff'], color=tse_colors, width=0.003)
    ax1.set_title("TWSE Market Width (TSE Diff)", fontsize=14)
    ax1.axhline(0, color='gray', linewidth=0.8, linestyle='--')
    ax1.grid(True, alpha=0.3)

    # 上櫃圖表
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

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當收到 /check 指令時回應當前走勢圖 """
    try:
        await update.message.reply_text("⏳ 正在從雲端資料庫撈取最新真實數據並即時繪圖...")
        
        loop = asyncio.get_running_loop()
        
        def safe_draw():
            try:
                return draw_chart_to_memory()
            except Exception as inner_err:
                return "ERR_CRASH", traceback.format_exc()

        result = await loop.run_in_executor(None, safe_draw)
        
        if isinstance(result, tuple) and result[0] == "ERR_CRASH":
            error_details = result[1]
            await update.message.reply_text(f"❌ 繪圖核心組件崩潰！詳細錯誤追蹤如下：\n```text\n{error_details}\n```", parse_mode="Markdown")
            return

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
        ext_err = traceback.format_exc()
        await update.message.reply_text(f"❌ 指令執行失敗：\n```text\n{ext_err}\n```", parse_mode="Markdown")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /history 指令：直接在 TG 查閱最新 10 筆數據進行核對 """
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
    """ /clean 指令：刪除今天以前的所有舊歷史資料 """
    try:
        today_str = datetime.datetime.now(TW_TZ).strftime("%Y-%m-%d")
        query = text("DELETE FROM market_status WHERE timestamp NOT LIKE :today")
        with engine.connect() as conn:
            with conn.begin():
                result = conn.execute(query, {"today": f"{today_str}%"})
                deleted_rows = result.rowcount
        await update.message.reply_text(f"🧹 清理成功！已成功清空今天以前的舊資料，共刪除 {deleted_rows} 筆歷史紀錄。")
    except Exception as e:
        await update.message.reply_text(f"❌ 資料庫清理失敗: {e}")

def dummy_webhook_service():
    """ 網頁服務核心，用來維持 Render 存活 """
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
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("clean", clean_command))
    
    # 3. 啟動 Telegram Bot 監聽
    await application.initialize()
    await application.updater.start_polling()
    await application.start()
    logging.info("🤖 Telegram Bot 監聽服務已在背景建立...")
    
    # 4. 執行網頁服務
    logging.info("🚀 雲端伺服器與監聽系統正式上線...")
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, dummy_webhook_service)

if __name__ == '__main__':
    asyncio.run(main())
