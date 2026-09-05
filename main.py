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
