from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
import io
import base64
import logging
import os
import redis
import hashlib

# Cấu hình logging
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="POS GNP AI Server")

# Cấu hình Redis (Lấy URL từ Environment Variable trên Render)
# Ví dụ REDIS_URL: redis://default:password@host:port
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
try:
    r = redis.from_url(REDIS_URL, ssl_cert_reqs=None)
    r.ping()
    logging.info("✅ Kết nối Redis thành công")
except Exception as e:
    logging.warning(f"⚠️ Không kết nối được Redis ({e}). Hệ thống sẽ chạy không có cache.")
    r = None

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POS AI Server is running with Redis support!"}

# 1. ENDPOINT NÉN ẢNH (Có Cache Redis)
@app.post("/nen-anh")
async def compress_image(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        
        # Tạo hash của ảnh để làm key cache
        file_hash = hashlib.md5(contents).hexdigest()
        cache_key = f"img_compress_{file_hash}"
        
        # Kiểm tra Redis Cache
        if r:
            cached_result = r.get(cache_key)
            if cached_result:
                logging.info(f"⚡ Cache Hit: {cache_key}")
                return {"b64": cached_result.decode('utf-8')}

        # Xử lý ảnh nếu không có cache
        image = Image.open(io.BytesIO(contents))
        if image.mode in ("RGBA", "P"):
            image = image.convert('RGB')
        image.thumbnail((800, 800), Image.LANCZOS)
        
        buffered = io.BytesIO()
        image.save(buffered, format="JPEG", quality=75)
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        # Lưu vào Redis (TTL 1 tiếng)
        if r:
            r.setex(cache_key, 3600, img_str)
            logging.info(f"💾 Cached: {cache_key}")
        
        return {"b64": img_str}
    except Exception as e:
        logging.error(f"Lỗi nén ảnh: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# 2. ENDPOINT ĐỌC BẢNG GIÁ (Mock + Cache)
@app.post("/ai-doc-bang-gia")
async def ai_read_price_list(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        file_hash = hashlib.md5(contents).hexdigest()
        cache_key = f"ai_price_{file_hash}"
        
        if r:
            cached_result = r.get(cache_key)
            if cached_result:
                import json
                return json.loads(cached_result)

        # Giả lập dữ liệu (Thực tế sẽ gọi Gemini/Vision ở đây)
        result = {
            "success": True,
            "danhSach": [
                {"ten": "Sữa tươi Vinamilk (AI)", "ma": "SP001", "giaNhap": 25000},
                {"ten": "Bánh mì Sandwich (AI)", "ma": "SP002", "giaNhap": 12000}
            ]
        }
        
        if r:
            import json
            r.setex(cache_key, 3600, json.dumps(result))
            
        return result
    except Exception as e:
        logging.error(f"Lỗi AI: {str(e)}")
        return {"success": False, "error": str(e)}

# 3. ENDPOINT DỰ BÁO (Mock)
@app.get("/bao-cao-du-bao/{days}")
async def forecast_stock(days: int):
    return [
        {"ten": "Sữa tươi Vinamilk", "ngay_con": 3, "muc_do": "nguy_hiem"},
        {"ten": "Bánh mì Sandwich", "ngay_con": 5, "muc_do": "canh_bao"}
    ]

@app.post("/tim-bang-anh")
async def search_by_image(file: UploadFile = File(...)):
    return {
        "tenAI": "Sản phẩm mẫu",
        "danhSach": [{"id": "1", "ten": "Sữa tươi", "gia": 28000}]
    }
import os
import io
import json
import base64
import sqlite3
from datetime import datetime
from typing import Optional, List
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis
from PIL import Image
import requests

# --- CẤU HÌNH ---
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
SQLITE_DB_PATH = "ai_history.db"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "") # Nếu muốn dùng Gemini trực tiếp từ Python

app = FastAPI(title="POS GNP AI Bridge")

# Cho phép GAS gọi sang
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- KẾT NỐI REDIS ---
try:
    r = redis.from_url(REDIS_URL, ssl_cert_reqs=None, decode_responses=True)
    r.ping()
    print("✅ Kết nối Redis thành công")
except Exception as e:
    print(f"⚠️ Không kết nối được Redis: {e}. Hệ thống sẽ chạy không có cache.")
    r = None

# --- KẾT NỐI SQLITE (LOG AI) ---
def init_sqlite():
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
        print(f"Lỗi ghi log SQLite: {e}")

# --- MODELS ---
class PredictionRequest(BaseModel):
    history_data: List[dict]

# --- TIỆN ÍCH ---
def compress_image_base64(base64_str: str, quality: int = 75, max_size: int = 800) -> str:
    try:
        # Loại bỏ header data:image... nếu có
        if "," in base64_str:
            base64_str = base64_str.split(",")[1]
        
        img_data = base64.b64decode(base64_str)
        img = Image.open(io.BytesIO(img_data))
        
        # Resize
        img.thumbnail((max_size, max_size))
        
        # Convert to RGB if necessary (for JPEG)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=quality)
        return base64.b64encode(buffered.getvalue()).decode()
    except Exception as e:
        print(f"Lỗi nén ảnh: {e}")
        return base64_str # Trả về ảnh gốc nếu lỗi

# --- ENDPOINTS ---

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POS AI Bridge is running with SQLite & Redis"}

@app.post("/api/nen-anh")
async def compress_image_endpoint(file: UploadFile = File(...)):
    start_time = datetime.now()
    try:
        contents = await file.read()
        img = Image.open(io.BytesIO(contents))
        
        # Resize & Compress
        img.thumbnail((800, 800))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
            
        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=75)
        b64_result = base64.b64encode(buffered.getvalue()).decode()
        
        # Log
        end_time = datetime.now()
        duration = int((end_time - start_time).total_seconds() * 1000)
        log_ai_action("COMPRESS_IMAGE", f"File: {file.filename}", "Success", duration, "SUCCESS")
        
        return {"b64": b64_result}
    except Exception as e:
        log_ai_action("COMPRESS_IMAGE", f"File: {file.filename}", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/tim-bang-anh")
async def search_by_image_endpoint(file: UploadFile = File(...)):
    start_time = datetime.now()
    try:
        # 1. Đọc ảnh
        contents = await file.read()
        
        # 2. Kiểm tra Cache Redis (Dùng hash của ảnh làm key)
        # Lưu ý: Trong thực tế nên dùng perceptual hash, ở đây dùng MD5 đơn giản cho demo
        import hashlib
        img_hash = hashlib.md5(contents).hexdigest()
        cache_key = f"img_search_{img_hash}"
        
        if r:
            cached_result = r.get(cache_key)
            if cached_result:
                print("⚡ Cache Hit")
                log_ai_action("SEARCH_IMAGE", f"Hash: {img_hash}", "Cache Hit", 0, "SUCCESS")
                return json.loads(cached_result)

        # 3. Gọi Gemini API (Nếu có Key)
        result_data = {"tenAI": "Sản phẩm chưa xác định (Chưa có API Key)", "danhSach": []}
        
        if GEMINI_API_KEY:
            # Encode ảnh sang base64 để gửi Gemini
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
                # Parse JSON từ text
                try:
                    # Clean markdown code block nếu có
                    text = text.replace("```json", "").replace("```", "").strip()
                    ai_result = json.loads(text)
                    result_data = {
                        "tenAI": ai_result.get("ten", "Không rõ"),
                        "danhSach": [{"ten": ai_result.get("ten"), "ma": "", "gia": 0}] # Cấu trúc giả lập cho frontend
                    }
                except:
                    pass
        
        # 4. Lưu Cache
        if r:
            r.setex(cache_key, 3600, json.dumps(result_data)) # Cache 1 tiếng
            
        # 5. Log
        end_time = datetime.now()
        duration = int((end_time - start_time).total_seconds() * 1000)
        log_ai_action("SEARCH_IMAGE", f"Hash: {img_hash}", json.dumps(result_data), duration, "SUCCESS")
        
        return result_data
        
    except Exception as e:
        log_ai_action("SEARCH_IMAGE", "Unknown", str(e), 0, "FAILED")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/bao-cao-du-bao/{days}")
async def forecast_stock_endpoint(days: int):
    # Giả lập dữ liệu dự báo (Vì Prophet cần cài đặt nặng, ở đây trả về mock data)
    # Trong thực tế, bạn sẽ load model Prophet ở đây
    mock_data = [
        {"ten": "Coca Cola 330ml", "ngay_con": 3, "muc_do": "nguy_hiem"},
        {"ten": "Mì Hảo Hảo", "ngay_con": 5, "muc_do": "canh_bao"}
    ]
    log_ai_action("FORECAST", f"Days: {days}", json.dumps(mock_data), 10, "SUCCESS_MOCK")
    return mock_data

@app.get("/api/logs")
def get_logs(limit: int = 10):
    """Xem log AI gần nhất"""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM ai_logs ORDER BY id DESC LIMIT ?", (limit,))
            rows = cursor.fetchall()
            return {"logs": rows}
    except Exception as e:
        return {"error": str(e)}
