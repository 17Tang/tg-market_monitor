import os
import datetime
import logging
import asyncio
import traceback
import pytz
import threading  # 👈 核心修正：引入多線程套件，徹底防止網頁服務卡死排程器
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
    """ 【核心修正】24小時全自動定時排程：由內部精準判定台灣開盤時間，絕不受雲端主機時區干擾 """
    now = datetime.datetime.now(TW_TZ)
    
    # 嚴格判定：如果是週末，或者不是台灣時間 09:00 ~ 13:35 之間，就跳過不抓
    if now.weekday() >= 5 or not ("09:00" <= now.strftime("%H:%M") <= "13:35"):
        logging.info(f"☕ 台灣時間 {now.strftime('%H:%M')} 非台股開盤時段，自動排程不執行抓取。")
        return

    logging.info(f"⏰ 【排程啟動】當前台灣時間 {now.strftime('%H:%M')}，開始對永豐金正式環境抓取大盤快照...")
    try:
        # 連線永豐金正式環境
        api = sj.Shioaji()
        api.login(api_key=API_KEY, secret_key=SECRET_KEY)
        
        # 抓取上市(001)與上櫃(101)即時數字
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
        logging.info(f"📊 【全自動歸檔成功】TSE: {tse_diff:+d} | OTC: {otc_diff:+d}")
        api.logout()
    except Exception as e:
        logging.error(f"❌ 永豐金 API 抓取失敗，原因: {e}")

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

# ==================== TELEGRAM 指令功能區 ====================

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ 當收到 /check 指令時回應當前走勢圖 """
    try:
        await update.message.reply_text("⏳ 正在從雲端資料庫撈取最新真實數據並即時繪圖...")
        
        loop = asyncio.get_running_loop()
        latest_data, img_buf = await loop.run_in_executor(None, draw_chart_to_memory)
        
        # 【智慧防呆】如果發現今天剛好漏掉數據，手動按 /check 的當下立刻幫補抓一筆，不讓用戶看空圖
        if latest_data is None:
            logging.info("發現今日資料庫無數據，立刻在點擊當下強制觸發即時抓取...")
            await loop.run_in_executor(None, fetch_and_save_to_db)
            latest_data, img_buf = await loop.run_in_executor(None, draw_chart_to_memory)

        if latest_data is None:
            await update.message.reply_text("❌ 【憑證權限提示】已強制觸發連線，但仍無法寫入數據。請確認您的永豐金 API「正式環境」憑證是否已成功開通並在線。")
        else:
            time_str = pd.to_datetime(latest_data['timestamp']).strftime('%H:%M')
            caption_text = (
                f"📊 即時雲端監測 ({time_str})\n"
                f"🏛 上市家數差: {int(latest_data['tse_diff']):+d}\n"
                f"🏢 上櫃家數差: {int(latest_data['otc_diff']):+d}"
            )
            await update.message.reply_photo(photo=img_buf, caption=caption_text)
            
    except Exception as e:
        await update.message.reply_text(f"❌ 指令執行失敗，詳細原因: {e}")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ /history 指令：直接在 TG 查閱最新 10 筆數據進行核對 """
    try:
        query = text("SELECT timestamp, tse_diff, otc_diff FROM market_status ORDER BY timestamp DESC LIMIT 10")
        with engine.connect() as conn:
            result = conn.execute(query)
            rows = result.fetchall()
            
        if not rows:
            await update.message.reply_text("📭 目前資料庫內沒有任何數據喔！")
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
    """ 網頁服務核心：用來維持 Render 存活不中斷 """
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
    # 1. 【核心修正】啟動獨立背景定時排程器（每 5 分鐘固定觸發，不再受 Render 時區限制影響）
    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_and_save_to_db, 'interval', minutes=5)
    scheduler.start()
    logging.info("⏰ 背景定時自動排程系統已就位...")
    
    # 2. 設定 Telegram Bot 指令
    application = Application.builder().token(TG_TOKEN).build()
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("clean", clean_command))
    
    # 3. 【核心修正】使用背景 Thread 執行網頁存活服務，將主執行緒完全解放給 Bot，排程絕不卡死
    web_thread = threading.Thread(target=dummy_webhook_service, daemon=True)
    web_thread.start()
    logging.info("🌐 網頁存活守護守護服務已順利推至背景獨立執行緒...")
    
    # 4. 啟動 Telegram Bot 服務並讓主執行緒維持監聽
    logging.info("🚀 雲端自動排程數據監聽系統正式上線...")
    application.run_polling()
