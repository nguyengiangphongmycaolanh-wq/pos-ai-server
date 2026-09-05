from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
import io
import base64
import logging

# Cấu hình logging để xem lỗi trên Render
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="POS GNP AI Server")

# Cho phép ứng dụng web (POS GNP) gọi vào API này (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def read_root():
    return {"status": "ok", "message": "POS AI Server is running. Ready to compress images!"}

# 1. ENDPOINT NÉN ẢNH (Khớp với goiPythonAPI trong Code.gs)
@app.post("/nen-anh")
async def compress_image(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        image = Image.open(io.BytesIO(contents))
        
        # Chuyển sang RGB nếu ảnh là PNG có nền trong suốt (tránh lỗi khi lưu JPEG)
        if image.mode in ("RGBA", "P"):
            image = image.convert('RGB')

        # Resize ảnh về tối đa 800px (tối ưu cho Google Apps Script)
        image.thumbnail((800, 800), Image.LANCZOS)
        
        # Lưu lại vào bộ nhớ đệm dưới dạng JPEG
        buffered = io.BytesIO()
        image.save(buffered, format="JPEG", quality=75)
        
        # QUAN TRỌNG: Trả về chuỗi base64 THUẦN TÚY (không có tiền tố data:image...)
        # Vì Google Apps Script Utilities.base64Decode chỉ nhận chuỗi thuần
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return {"b64": img_str}
    except Exception as e:
        logging.error(f"Lỗi nén ảnh: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# 2. ENDPOINT ĐỌC BẢNG GIÁ / TÌM KIẾM ẢNH (Khớp với processAIQueue trong JS)
# Trong JS bạn gọi là '/ai-doc-bang-gia', nên mình đặt tên route y hệt
@app.post("/ai-doc-bang-gia")
async def ai_read_price_list(file: UploadFile = File(...)):
    """
    Hàm này hiện tại là GIẢ LẬP (Mock).
    Để dùng thật, bạn cần tích hợp Google Vision API hoặc Gemini API ở đây.
    """
    try:
        # Giả lập dữ liệu trả về để test kết nối
        # Cấu trúc trả về phải khớp với những gì JS mong đợi: { success: true, danhSach: [...] }
        return {
            "success": True,
            "danhSach": [
                {"ten": "Sữa tươi Vinamilk (Test)", "ma": "SP001", "giaNhap": 25000},
                {"ten": "Bánh mì Sandwich (Test)", "ma": "SP002", "giaNhap": 12000}
            ]
        }
    except Exception as e:
        logging.error(f"Lỗi AI đọc bảng giá: {str(e)}")
        return {"success": False, "error": str(e)}

# 3. ENDPOINT DỰ BÁO TỒN KHO (Khớp với layDuBaoTonKhoPython trong Code.gs)
@app.get("/bao-cao-du-bao/{days}")
async def forecast_stock(days: int):
    # Giả lập dữ liệu dự báo tồn kho
    # Cấu trúc trả về khớp với Code.gs: [{ten, ngay_con, muc_do}]
    return [
        {"ten": "Sữa tươi Vinamilk", "ngay_con": 3, "muc_do": "nguy_hiem"},
        {"ten": "Bánh mì Sandwich", "ngay_con": 5, "muc_do": "canh_bao"},
        {"ten": "Coca Cola 330ml", "ngay_con": 10, "muc_do": "an_toan"}
    ]

# Endpoint phụ để test tìm kiếm bằng ảnh (nếu bạn muốn dùng riêng)
@app.post("/tim-bang-anh")
async def search_by_image(file: UploadFile = File(...)):
    return {
        "tenAI": "Sản phẩm mẫu (Test)",
        "danhSach": [
            {"id": "1", "ten": "Sữa tươi Vinamilk", "gia": 28000},
            {"id": "2", "ten": "Bánh mì Sandwich", "gia": 15000}
        ]
    }
