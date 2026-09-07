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
GAS_WEB_APP_URL = os.getenv("GAS_WEB_APP_URL", "") # URL Web App GAS

# Cấu hình Telegram
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

# --- KẾT NỐI SQLITE (LOG, INVOICES & TUYEN CACHE) ---
def init_sqlite():
    try:
        conn = sqlite3.connect(SQLITE_DB_PATH)
        cursor = conn.cursor()
        # Bảng log AI
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
        # Bảng hóa đơn tạm
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ma_hd TEXT,
                data JSON,
                status TEXT
            )
        ''')
        # Bảng cache tuyến (Backup cho Redis)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tuyen_cache (
                date TEXT PRIMARY KEY,
                data JSON,
                updated_at TEXT
            )
        ''')
        conn.commit()
        conn.close()
        logging.info("✅ SQLite initialized (Logs, Invoices, Tuyen Cache)")
    except Exception as e:
        logging.error(f"Lỗi khởi tạo SQLite: {e}")

init_sqlite()

@contextmanager
def get_db():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row # Để truy cập cột theo tên
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

# --- MODELS ---
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

class TuyenRequest(BaseModel):
    phienId: str
    ngay: Optional[str] = None # YYYY-MM-DD

# --- TIỆN ÍCH: TELEGRAM ---
def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        response = requests.post(url, json=payload, timeout=5)
        return response.status_code == 200
    except Exception as e:
        logging.error(f"Lỗi Telegram: {e}")
        return False

# --- BACKGROUND JOB: SYNC HÓA ĐƠN ---
def sync_to_google_sheets_and_notify(ma_hd: str):
    # 1. Gửi Telegram
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM invoices WHERE ma_hd=?", (ma_hd,))
            row = cursor.fetchone()
            if row:
                data = json.loads(row[0])
                msg = f"🔔 *BÁN HÀNG*\n🧾 `{data.get('maHD')}`\n👤 {data.get('tenKhach')}\n💰 {data.get('tongTien'):,.0f}₫\n👨💼 {data.get('nhanVien')}"
                send_telegram_message(msg)
    except Exception as e:
        logging.error(f"Lỗi notify: {e}")

    # 2. Sync về Google Sheets
    if not GAS_WEB_APP_URL: return
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM invoices WHERE ma_hd=?", (ma_hd,))
            row = cursor.fetchone()
            if row:
                payload = json.loads(row[0])
                response = requests.post(GAS_WEB_APP_URL, json={"action": "sync_invoice", "data": payload})
                if response.status_code == 200:
                    cursor.execute("UPDATE invoices SET status='SYNCED' WHERE ma_hd=?", (ma_hd,))
                    conn.commit()
                    logging.info(f"✅ Synced invoice {ma_hd}")
                    
                    # Xóa cache tuyến để lần sau load là thấy trạng thái mới
                    # (Giả sử bán hàng xong thì khách đó đã được ghé)
                    try:
                        ngay_hien_tai = datetime.now().strftime("%Y-%m-%d")
                        # Xóa tất cả cache tuyến của ngày hôm nay (đơn giản hóa)
                        cursor.execute("DELETE FROM tuyen_cache WHERE date = ?", (ngay_hien_tai,))
                        conn.commit()
                        if r:
                            # Xóa Redis keys liên quan (pattern search hơi phức tạp nên xóa theo key cụ thể nếu biết, hoặc flushdb nếu ít data)
                            # Ở đây ta chỉ xóa SQLite, Redis sẽ tự hết hạn sau 5 phút
                            pass
                    except: pass
    except Exception as e:
        logging.error(f"Lỗi sync sheet: {e}")

# --- ENDPOINTS ---

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POS AI Bridge Ready (Tuyen + AI + Telegram)"}

# 1. TUYẾN BÁN HÀNG SIÊU NHANH (REDIS + SQLITE + GAS)
@app.post("/api/tuyen-hom-nay")
async def lay_tuyen_hom_nay(req: TuyenRequest):
    ngay_hien_tai = req.ngay or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"tuyen:{ngay_hien_tai}:{req.phienId}"
    
    # 1. Check Redis (< 5ms)
    if r:
        try:
            cached_data = r.get(cache_key)
            if cached_data:
                logging.info(f"⚡ Redis Hit: {cache_key}")
                return json.loads(cached_data)
        except Exception as e:
            logging.error(f"Redis error: {e}")

    # 2. Check SQLite (< 20ms)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM tuyen_cache WHERE date = ?", (ngay_hien_tai,))
            row = cursor.fetchone()
            if row:
                logging.info(f"💾 SQLite Hit: {ngay_hien_tai}")
                data = json.loads(row['data'])
                if r: r.setex(cache_key, 300, json.dumps(data)) # Sync ngược lên Redis
                return data
    except Exception as e:
        logging.error(f"SQLite error: {e}")

    # 3. Call GAS (> 500ms)
    if not GAS_WEB_APP_URL:
        raise HTTPException(status_code=500, detail="Missing GAS_WEB_APP_URL")

    try:
        response = requests.post(GAS_WEB_APP_URL, json={
            "action": "get_tuyen_data",
            "phienId": req.phienId,
            "ngay": ngay_hien_tai
        }, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get("thanhCong"):
                tuyen_data = result.get("data")
                # Save to Redis (5 mins)
                if r: r.setex(cache_key, 300, json.dumps(tuyen_data))
                # Save to SQLite
                with get_db() as conn:
                    cursor = conn.cursor()
                    cursor.execute("INSERT OR REPLACE INTO tuyen_cache (date, data, updated_at) VALUES (?, ?, ?)", 
                                   (ngay_hien_tai, json.dumps(tuyen_data), datetime.now().isoformat()))
                    conn.commit()
                return tuyen_data
            else:
                raise HTTPException(status_code=400, detail=result.get("thongBao", "GAS Error"))
        else:
            raise HTTPException(status_code=500, detail="GAS Connection Error")
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="GAS Timeout")
    except Exception as e:
        logging.error(f"GAS Fetch Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# 2. XÓA CACHE TUYẾN (Khi check-in hoặc bán hàng)
@app.post("/api/invalidate-tuyen-cache")
async def invalidate_tuyen_cache(req: TuyenRequest):
    ngay_hien_tai = req.ngay or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"tuyen:{ngay_hien_tai}:{req.phienId}"
    if r: r.delete(cache_key)
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM tuyen_cache WHERE date = ?", (ngay_hien_tai,))
            conn.commit()
    except: pass
    return {"status": "success"}

# 3. THANH TOÁN NHANH
@app.post("/api/thanh-toan-nhanh")
async def thanh_toan_nhanh(invoice: InvoiceRequest, background_tasks: BackgroundTasks):
    start_time = datetime.now()
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO invoices (ma_hd, data, status) VALUES (?, ?, ?)", 
                           (invoice.maHD, invoice.json(), "PENDING"))
            conn.commit()

        if r:
            pipe = r.pipeline()
            for item in invoice.danhSach:
                pipe.decrby(f"tonkho:{item.ma}", item.soLuong)
            pipe.execute()

        background_tasks.add_task(sync_to_google_sheets_and_notify, invoice.maHD)
        
        duration = int((datetime.now() - start_time).total_seconds() * 1000)
        log_ai_action("PAYMENT", f"HD: {invoice.maHD}", f"Tong: {invoice.tongTien}", duration, "SUCCESS")
        return {"status": "success", "maHD": invoice.maHD}
    except Exception as e:
        log_ai_action("PAYMENT", f"HD: {invoice.maHD}", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

# 4. NÉN ẢNH
@app.post("/api/nen-anh")
async def compress_image_endpoint(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        img = Image.open(io.BytesIO(contents))
        img.thumbnail((800, 800))
        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=75)
        return {"b64": base64.b64encode(buffered.getvalue()).decode()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 5. TÌM KIẾM ẢNH (AI)
@app.post("/api/tim-bang-anh")
async def search_by_image_endpoint(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        img_hash = hashlib.md5(contents).hexdigest()
        cache_key = f"img_search_{img_hash}"
        
        if r:
            cached = r.get(cache_key)
            if cached: return json.loads(cached)

        result_data = {"tenAI": "Unknown", "danhSach": []}
        if GEMINI_API_KEY:
            img_b64 = base64.b64encode(contents).decode()
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
            payload = {
                "contents": [{"parts": [{"text": "Nhận diện sản phẩm. JSON: {'ten': '...', 'tuKhoa': []}"}, {"inline_data": {"mime_type": file.content_type, "data": img_b64}}]}],
                "generationConfig": {"response_mime_type": "application/json"}
            }
            resp = requests.post(url, json=payload)
            if resp.status_code == 200:
                text = resp.json()['candidates'][0]['content']['parts'][0]['text'].replace("```json", "").replace("```", "").strip()
                ai_res = json.loads(text)
                result_data = {"tenAI": ai_res.get("ten"), "danhSach": [{"ten": ai_res.get("ten"), "ma": "", "gia": 0}]}
        
        if r: r.setex(cache_key, 3600, json.dumps(result_data))
        return result_data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# 6. TEST TELEGRAM
@app.get("/api/test-telegram")
def test_telegram():
    if send_telegram_message("🤖 Test thành công! POS GNP AI Bridge đang hoạt động."):
        return {"status": "success"}
    return {"status": "error"}

# 7. LOGS
@app.get("/api/logs")
def get_logs(limit: int = 10):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_logs ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return {"logs": [dict(row) for row in rows]}
    except Exception as e:
        return {"error": str(e)}
