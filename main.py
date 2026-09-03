from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
import io
import base64

app = FastAPI()

# Cho phép ứng dụng web (POS GNP) gọi vào API này
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

@app.post("/nen-anh")
async def compress_image(file: UploadFile = File(...)):
    try:
        # Đọc file ảnh gửi lên
        contents = await file.read()
        image = Image.open(io.BytesIO(contents))
        
        # Resize ảnh về tối đa 800px để nhẹ (tối ưu cho web/app)
        image.thumbnail((800, 800), Image.LANCZOS)
        
        # Lưu lại vào bộ nhớ đệm dưới dạng JPEG
        buffered = io.BytesIO()
        image.save(buffered, format="JPEG", quality=75)
        
        # Chuyển sang chuỗi Base64 để gửi ngược về Google Apps Script
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return {"b64": f"data:image/jpeg;base64,{img_str}"}
    except Exception as e:
        return {"error": str(e)}

@app.post("/tim-bang-anh")
async def search_by_image(file: UploadFile = File(...)):
    # Đây là hàm giả lập cho tính năng tìm kiếm bằng ảnh (CLIP)
    # Hiện tại trả về dữ liệu mẫu để test kết nối
    return {
        "tenAI": "Sản phẩm mẫu (Test)",
        "danhSach": [
            {"id": "1", "ten": "Sữa tươi Vinamilk", "gia": 28000},
            {"id": "2", "ten": "Bánh mì Sandwich", "gia": 15000}
        ]
    }

@app.get("/bao-cao-du-bao/{days}")
async def forecast_stock(days: int):
    # Giả lập dữ liệu dự báo tồn kho
    return [
        {"ten": "Sữa tươi Vinamilk", "ngay_con": 3, "muc_do": "nguy_hiem"},
        {"ten": "Bánh mì Sandwich", "ngay_con": 5, "muc_do": "canh_bao"}
    ]
