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

from fastapi import FastAPI, UploadFile, File, HTTPException
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
SQLITE_DB_PATH = "ai_history.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") 

# --- KẾT NỐI REDIS (CACHE) ---
r = None
try:
    # ssl_cert_reqs=None cần thiết cho một số nhà cung cấp Redis cloud (như Upstash)
    r = redis.from_url(REDIS_URL, ssl_cert_reqs=None, decode_responses=True)
    r.ping()
    logging.info("✅ Kết nối Redis thành công")
except Exception as e:
    logging.warning(f"⚠️ Không kết nối được Redis: {e}. Hệ thống sẽ chạy không có cache.")
    r = None

# --- KẾT NỐI SQLITE (LOG LỊCH SỬ) ---
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

# --- TIỆN ÍCH XỬ LÝ ẢNH ---
def compress_image_base64(base64_str: str, quality: int = 75, max_size: int = 800) -> str:
    try:
        if "," in base64_str:
            base64_str = base64_str.split(",")[1]
        
        img_data = base64.b64decode(base64_str)
        img = Image.open(io.BytesIO(img_data))
        
        img.thumbnail((max_size, max_size))
        
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=quality)
        return base64.b64encode(buffered.getvalue()).decode()
    except Exception as e:
        logging.error(f"Lỗi nén ảnh base64: {e}")
        return base64_str

# --- ENDPOINTS ---

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POS AI Bridge is running with SQLite & Redis"}

# 1. NÉN ẢNH (Dùng cho upload ảnh sản phẩm)
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

# 2. TÌM KIẾM BẰNG HÌNH ẢNH (AI Vision)
@app.post("/api/tim-bang-anh")
async def search_by_image_endpoint(file: UploadFile = File(...)):
    start_time = datetime.now()
    try:
        contents = await file.read()
        
        # Tạo Hash để Cache
        img_hash = hashlib.md5(contents).hexdigest()
        cache_key = f"img_search_{img_hash}"
        
        # Kiểm tra Redis Cache
        if r:
            cached_result = r.get(cache_key)
            if cached_result:
                logging.info("⚡ Cache Hit")
                log_ai_action("SEARCH_IMAGE", f"Hash: {img_hash}", "Cache Hit", 0, "SUCCESS")
                return json.loads(cached_result)

        # Gọi Gemini API nếu chưa có cache
        result_data = {"tenAI": "Sản phẩm chưa xác định", "danhSach": []}
        
        if GEMINI_API_KEY:
            img_b64 = base64.b64encode(contents).decode()
            # Sử dụng gemini-1.5-flash cho tốc độ nhanh và chi phí thấp
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
        
        # Lưu Cache
        if r:
            r.setex(cache_key, 3600, json.dumps(result_data))
            
        end_time = datetime.now()
        duration = int((end_time - start_time).total_seconds() * 1000)
        log_ai_action("SEARCH_IMAGE", f"Hash: {img_hash}", json.dumps(result_data), duration, "SUCCESS")
        
        return result_data
        
    except Exception as e:
        log_ai_action("SEARCH_IMAGE", "Unknown", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

# 3. DỰ BÁO TỒN KHO (Mock Data)
@app.get("/api/bao-cao-du-bao/{days}")
async def forecast_stock_endpoint(days: int):
    mock_data = [
        {"ten": "Coca Cola 330ml", "ngay_con": 3, "muc_do": "nguy_hiem"},
        {"ten": "Mì Hảo Hảo", "ngay_con": 5, "muc_do": "canh_bao"}
    ]
    log_ai_action("FORECAST", f"Days: {days}", json.dumps(mock_data), 10, "SUCCESS_MOCK")
    return mock_data

# 4. XEM LOG (Debug)
@app.get("/api/logs")
def get_logs(limit: int = 10):
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_logs ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return {"logs": rows}
    except Exception as e:
        return {"error": str(e)}
