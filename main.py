import os
import io
import json
import base64
import sqlite3
import hashlib
import logging
from datetime import datetime
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis
from PIL import Image
import requests

# --- CẤU HÌNH LOGGING ---
logging.basicConfig(level=logging.INFO)

# --- KHỞI TẠO FASTAPI ---
app = FastAPI(title="POS GNP AI Bridge")

# Cho phép Google Apps Script gọi sang (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- CẤU HÌNH MÔI TRƯỜNG (ENVIRONMENT VARIABLES) ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SQLITE_DB_PATH = "pos_data.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") 
GAS_WEB_APP_URL = os.getenv("GAS_WEB_APP_URL", "")

# --- CẤU HÌNH TELEGRAM BOT (THAY THẾ ZALO) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# --- KẾT NỐI REDIS (CACHE) ---
r = None
try:
    r = redis.from_url(REDIS_URL, ssl_cert_reqs=None, decode_responses=True)
    r.ping()
    logging.info("✅ Kết nối Redis thành công")
except Exception as e:
    logging.warning(f"⚠️ Không kết nối được Redis: {e}. Hệ thống sẽ chạy không có cache.")
    r = None

# --- KẾT NỐI SQLITE (LOG & INVOICES) ---
def init_sqlite():
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                action TEXT,
                input_summary TEXT,
                output_result TEXT,
                processing_time_ms INTEGER,
                status TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_hd TEXT,
                data JSON,
                status TEXT
            )
        ''')
        conn.commit()
        conn.close()
        logging.info("✅ SQLite initialized")
    except Exception as e:
        logging.error(f"Lỗi khởi tạo SQLite: {e}")

init_sqlite()

@contextmanager
def get_db():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    try:
        yield conn
    finally:
        conn.close()

def log_ai_action(action: str, input_summary: str, output_result: str, time_ms: int, status: str):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ai_logs (timestamp, action, input_summary, output_result, processing_time_ms, status) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(), action, input_summary, output_result, time_ms, status)
            )
            conn.commit()
    except Exception as e:
        logging.error(f"Lỗi ghi log SQLite: {e}")

# --- MODELS CHO THANH TOÁN ---
class InvoiceItem(BaseModel):
    ten: str
    ma: str
    gia: float
    soLuong: int
    maKho: str

class InvoiceRequest(BaseModel):
    maHD: str
    tenKhach: str
    danhSach: List[InvoiceItem]
    tongTien: float
    nhanVien: str

# --- HÀM GỬI TELEGRAM (TIỆN ÍCH) ---
def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Chưa cấu hình Telegram Bot Token hoặc Chat ID")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown" # Hỗ trợ in đậm, nghiêng
    }
    try:
        response = requests.post(url, json=payload, timeout=5)
        if response.status_code == 200:
            logging.info("✅ Đã gửi thông báo Telegram")
            return True
        else:
            logging.error(f"Lỗi gửi Telegram: {response.text}")
            return False
    except Exception as e:
        logging.error(f"Lỗi kết nối Telegram: {e}")
        return False

# --- BACKGROUND JOB: SYNC VỀ GOOGLE SHEETS + GỬI TELEGRAM ---
def sync_to_google_sheets_and_notify(ma_hd: str):
    # 1. Gửi thông báo Telegram ngay lập tức
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM invoices WHERE ma_hd=?", (ma_hd,))
            row = cursor.fetchone()
            if row:
                data = json.loads(row[0])
                # Format tin nhắn đẹp mắt
                msg = f"🔔 *THÔNG BÁO BÁN HÀNG*\n" \
                      f"🧾 *Mã HĐ:* `{data.get('maHD', 'N/A')}`\n" \
                      f"👤 *Khách:* {data.get('tenKhach', 'Khách lẻ')}\n" \
                      f"💰 *Tổng:* {data.get('tongTien', 0):,.0f}₫\n" \
                      f"👨‍💼 *NV:* {data.get('nhanVien', 'N/A')}\n" \
                      f"⏰ _{datetime.now().strftime('%H:%M:%S %d/%m/%Y')}_"
                
                send_telegram_message(msg)
    except Exception as e:
        logging.error(f"Lỗi gửi notify: {e}")

    # 2. Sync về Google Sheets
    if not GAS_WEB_APP_URL:
        logging.warning("Chưa cấu hình GAS_WEB_APP_URL, bỏ qua sync.")
        return
        
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM invoices WHERE ma_hd=?", (ma_hd,))
            row = cursor.fetchone()
            if row:
                payload = json.loads(row[0])
                response = requests.post(GAS_WEB_APP_URL, json={
                    "action": "sync_invoice",
                    "data": payload
                })
                if response.status_code == 200:
                    cursor.execute("UPDATE invoices SET status='SYNCED' WHERE ma_hd=?", (ma_hd,))
                    conn.commit()
                    logging.info(f"✅ Đã sync hóa đơn {ma_hd} về Google Sheets")
                else:
                    logging.error(f"Lỗi sync GAS: {response.status_code}")
    except Exception as e:
        logging.error(f"Lỗi sync_to_google_sheets: {e}")

# --- ENDPOINTS ---

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POS AI Bridge is running with SQLite, Redis & Telegram"}

# 1. THANH TOÁN NHANH (Lưu SQLite + Trừ Redis + Sync Background + Telegram)
@app.post("/api/thanh-toan-nhanh")
async def thanh_toan_nhanh(invoice: InvoiceRequest, background_tasks: BackgroundTasks):
    start_time = datetime.now()
    try:
        # 1. Lưu vào SQLite
        with get_db() as conn:
            cursor = conn.cursor()
            # Lưu dưới dạng JSON string
            cursor.execute("INSERT INTO invoices (ma_hd, data, status) VALUES (?, ?, ?)", 
                           (invoice.maHD, invoice.json(), "PENDING"))
            conn.commit()

        # 2. Trừ tồn kho trong Redis
        if r:
            pipe = r.pipeline()
            for item in invoice.danhSach:
                pipe.decrby(f"tonkho:{item.ma}", item.soLuong)
            pipe.execute()
            logging.info(f"📉 Đã trừ tồn kho Redis cho {invoice.maHD}")

        # 3. Đẩy vào Background Task (Sync Sheet + Gửi Telegram)
        background_tasks.add_task(sync_to_google_sheets_and_notify, invoice.maHD)
        
        end_time = datetime.now()
        duration = int((end_time - start_time).total_seconds() * 1000)
        log_ai_action("PAYMENT", f"HD: {invoice.maHD}", f"Tong: {invoice.tongTien}", duration, "SUCCESS")

        return {"status": "success", "message": "Đã xử lý tức thì", "maHD": invoice.maHD}
    except Exception as e:
        log_ai_action("PAYMENT", f"HD: {invoice.maHD}", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

# Endpoint phụ để test gửi Telegram thủ công
@app.get("/api/test-telegram")
def test_telegram():
    success = send_telegram_message("🤖 *Test thành công!* Bot POS GNP đã hoạt động.")
    if success:
        return {"status": "success", "message": "Đã gửi tin nhắn test tới Telegram"}
    return {"status": "error", "message": "Gửi thất bại, kiểm tra log"}

# 2. NÉN ẢNH (Dùng cho upload ảnh sản phẩm)
@app.post("/api/nen-anh")
async def compress_image_endpoint(file: UploadFile = File(...)):
    start_time = datetime.now()
    try:
        contents = await file.read()
        img = Image.open(io.BytesIO(contents))
        
        img.thumbnail((800, 800))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=75)
        b64_result = base64.b64encode(buffered.getvalue()).decode()
        
        end_time = datetime.now()
        duration = int((end_time - start_time).total_seconds() * 1000)
        log_ai_action("COMPRESS_IMAGE", f"File: {file.filename}", "Success", duration, "SUCCESS")
        
        return {"b64": b64_result}
    except Exception as e:
        log_ai_action("COMPRESS_IMAGE", f"File: {file.filename}", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

# 3. TÌM KIẾM BẰNG HÌNH ẢNH (AI Vision + Cache)
@app.post("/api/tim-bang-anh")
async def search_by_image_endpoint(file: UploadFile = File(...)):
    start_time = datetime.now()
    try:
        contents = await file.read()
        img_hash = hashlib.md5(contents).hexdigest()
        cache_key = f"img_search_{img_hash}"
        
        if r:
            cached_result = r.get(cache_key)
            if cached_result:
                logging.info("⚡ Cache Hit")
                log_ai_action("SEARCH_IMAGE", f"Hash: {img_hash}", "Cache Hit", 0, "SUCCESS")
                return json.loads(cached_result)

        result_data = {"tenAI": "Sản phẩm chưa xác định", "danhSach": []}
        
        if GEMINI_API_KEY:
            img_b64 = base64.b64encode(contents).decode()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            prompt = "Nhận diện sản phẩm trong ảnh này. Trả về JSON: {'ten': 'tên sản phẩm', 'tuKhoa': ['kw1', 'kw2']}"
            
            payload = {
                "contents": [{
                    "parts": [
                        {"text": prompt},
                        {"inline_data": {"mime_type": file.content_type, "data": img_b64}}
                    ]
                }],
                "generationConfig": {"response_mime_type": "application/json"}
            }
            
            response = requests.post(url, json=payload)
            if response.status_code == 200:
                res_json = response.json()
                text = res_json['candidates'][0]['content']['parts'][0]['text']
                try:
                    text = text.replace("```json", "").replace("```", "").strip()
                    ai_result = json.loads(text)
                    result_data = {
                        "tenAI": ai_result.get("ten", "Không rõ"),
                        "danhSach": [{"ten": ai_result.get("ten"), "ma": "", "gia": 0}] 
                    }
                except:
                    pass
        
        if r:
            r.setex(cache_key, 3600, json.dumps(result_data))
            
        end_time = datetime.now()
        duration = int((end_time - start_time).total_seconds() * 1000)
        log_ai_action("SEARCH_IMAGE", f"Hash: {img_hash}", json.dumps(result_data), duration, "SUCCESS")
        
        return result_data
        
    except Exception as e:
        log_ai_action("SEARCH_IMAGE", "Unknown", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

# 4. DỰ BÁO TỒN KHO (Mock Data)
@app.get("/api/bao-cao-du-bao/{days}")
async def forecast_stock_endpoint(days: int):
    mock_data = [
        {"ten": "Coca Cola 330ml", "ngay_con": 3, "muc_do": "nguy_hiem"},
        {"ten": "Mì Hảo Hảo", "ngay_con": 5, "muc_do": "canh_bao"}
    ]
    log_ai_action("FORECAST", f"Days: {days}", json.dumps(mock_data), 10, "SUCCESS_MOCK")
    return mock_data

# 5. XEM LOG (Debug)
@app.get("/api/logs")
def get_logs(limit: int = 10):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_logs ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            result = [dict(zip(columns, row)) for row in rows]
            return {"logs": result}
    except Exception as e:
        return {"error": str(e)}
