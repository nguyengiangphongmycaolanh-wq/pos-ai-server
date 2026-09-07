# ============================================================
# POS GNP — PYTHON BRIDGE (FastAPI + Redis + SQLite)
# Bản vá tương thích 100% với GAS v14 + Frontend v13 / KIT v4
# ============================================================
import os, io, json, base64, sqlite3, hashlib, logging, unicodedata
from datetime import datetime, timedelta
from typing import Optional, List, Any
from contextlib import contextmanager

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis
from PIL import Image
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pos-bridge")

app = FastAPI(title="POS GNP AI Bridge")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# ---------------- ENV (đặt trong Render → Environment) ----------------
REDIS_URL          = os.getenv("REDIS_URL", "")
SQLITE_DB_PATH     = os.getenv("SQLITE_PATH", "pos_data.db")
GEMINI_API_KEY     = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL       = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GAS_WEB_APP_URL    = os.getenv("GAS_WEB_APP_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ---------------- REDIS (tuỳ chọn — thiếu vẫn chạy) ----------------
r = None
if REDIS_URL:
    try:
        r = redis.from_url(REDIS_URL, ssl_cert_reqs=None, decode_responses=True)
        r.ping(); log.info("✅ Redis connected")
    except Exception as e:
        log.warning("⚠️ Redis OFF: %s", e); r = None

# ---------------- SQLITE ----------------
@contextmanager
def get_db():
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try: yield conn
    finally: conn.close()

def init_sqlite():
    with get_db() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS ai_logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, action TEXT,
            input_summary TEXT, output_result TEXT,
            processing_time_ms INTEGER, status TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS invoices(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ma_hd TEXT UNIQUE,
            data JSON, status TEXT, created_at TEXT)''')
        # ✅ PK gồm cả phien_id: tuyến của Chủ và NV không đè nhau
        c.execute('''CREATE TABLE IF NOT EXISTS tuyen_cache(
            date TEXT, phien_id TEXT, data JSON, updated_at TEXT,
            PRIMARY KEY(date, phien_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS san_pham(
            id TEXT PRIMARY KEY, raw JSON, updated_at TEXT)''')
        conn.commit()
init_sqlite()

def _r_get(key):
    if not r: return None
    try:
        v = r.get(key); return json.loads(v) if v else None
    except Exception: return None

def _r_set(key, val, ttl=7200):
    if not r: return
    try: r.setex(key, ttl, json.dumps(val, ensure_ascii=False))
    except Exception: pass

def _r_del_prefix(prefix):
    if not r: return
    try:
        keys = r.keys(prefix + "*")
        if keys: r.delete(*keys)
    except Exception: pass

def bo_dau(s):
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("đ", "d")

def log_ai_action(action, inp, out, ms, status):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO ai_logs(timestamp,action,input_summary,output_result,processing_time_ms,status) VALUES (?,?,?,?,?,?)",
                         (datetime.now().isoformat(), action, inp, out, ms, status))
            conn.commit()
    except Exception as e:
        log.error("Log lỗi: %s", e)

# ---------------- MODELS ----------------
class InvoiceItem(BaseModel):
    ten: str = ""; ma: str = ""; gia: float = 0
    soLuong: int = 0; maKho: str = ""; giaNhap: float = 0

class InvoiceRequest(BaseModel):
    maHD: str; tenKhach: str = "Khách lẻ"
    danhSach: List[InvoiceItem] = []; tongTien: float = 0; nhanVien: str = ""

class TuyenRequest(BaseModel):
    phienId: str = ""; ngay: Optional[str] = None

class CacheTuyenRequest(BaseModel):
    phienId: str = ""; ngay: Optional[str] = None; data: Any = None

class InvalidateRequest(BaseModel):
    pattern: str = "all"

class ZaloRequest(BaseModel):
    maHD: str = ""; khach: str = ""; tong: float = 0
    nv: str = ""; timestamp: str = ""

# ---------------- TELEGRAM + SYNC NỀN ----------------
def send_telegram_message(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=5)
        return resp.status_code == 200
    except Exception as e:
        log.error("Telegram lỗi: %s", e); return False

def sync_to_google_sheets_and_notify(ma_hd: str):
    try:
        with get_db() as conn:
            row = conn.execute("SELECT data FROM invoices WHERE ma_hd=?", (ma_hd,)).fetchone()
        if not row: return
        data = json.loads(row["data"])
        send_telegram_message(
            f"🔔 *BÁN HÀNG*\n🧾 `{data.get('maHD')}`\n👤 {data.get('tenKhach')}\n"
            f"💰 {data.get('tongTien', 0):,.0f}₫\n👨‍💼 {data.get('nhanVien')}")
        if GAS_WEB_APP_URL:
            resp = requests.post(GAS_WEB_APP_URL,
                                 json={"action": "sync_invoice", "data": data}, timeout=30)
            if resp.status_code == 200:
                with get_db() as conn:
                    conn.execute("UPDATE invoices SET status='SYNCED' WHERE ma_hd=?", (ma_hd,))
                    conn.execute("DELETE FROM tuyen_cache WHERE date=?",
                                 (datetime.now().strftime("%Y-%m-%d"),))
                    conn.commit()
                log.info("✅ Synced %s", ma_hd)
    except Exception as e:
        log.error("Sync lỗi: %s", e)

# ---------------- HEALTH ----------------
@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok", "redis": bool(r),
            "sqlite": os.path.exists(SQLITE_DB_PATH), "time": datetime.now().isoformat()}

# ---------------- 1) SẢN PHẨM (GAS gọi) ----------------
@app.get("/api/san-pham")
def api_san_pham():
    c = _r_get("pos:products:index")
    if c and c.get("data"): return {"data": c["data"], "source": "redis"}
    with get_db() as conn:
        rows = conn.execute("SELECT raw FROM san_pham").fetchall()
    if not rows: return {"data": None}          # GAS tự fallback về Sheet
    return {"data": [json.loads(x["raw"]) for x in rows], "source": "sqlite"}

@app.post("/api/cache-san-pham")
def api_cache_san_pham(payload: List[Any]):
    now = datetime.now().isoformat()
    with get_db() as conn:
        conn.execute("DELETE FROM san_pham")
        conn.executemany("INSERT OR REPLACE INTO san_pham(id,raw,updated_at) VALUES (?,?,?)",
                         [(str(p.get("id", i)), json.dumps(p, ensure_ascii=False), now)
                          for i, p in enumerate(payload)])
        conn.commit()
    _r_set("pos:products:index",
           {"ts": int(datetime.now().timestamp() * 1000), "data": payload}, 7200)
    return {"status": "success", "so_sp": len(payload)}

# ---------------- 2) XÓA CACHE (GAS gọi mọi lần sửa/bán) ----------------
@app.post("/api/invalidate-cache")
def api_invalidate(b: InvalidateRequest):
    p = b.pattern
    if p == "all":
        _r_del_prefix("pos:"); _r_del_prefix("tuyen:")
        with get_db() as conn:
            conn.execute("DELETE FROM tuyen_cache"); conn.execute("DELETE FROM san_pham"); conn.commit()
    elif p == "san_pham":
        _r_del_prefix("pos:products")
        with get_db() as conn: conn.execute("DELETE FROM san_pham"); conn.commit()
    elif p == "tuyen":
        _r_del_prefix("tuyen:")
        with get_db() as conn: conn.execute("DELETE FROM tuyen_cache"); conn.commit()
    elif p == "khach_hang":
        _r_del_prefix("pos:khach")
    else:
        _r_del_prefix(p)
    return {"status": "success"}

# ---------------- 3) TUYẾN (✅ trả vỏ {data} · ✅ KHÔNG gọi ngược GAS) ----------------
@app.post("/api/tuyen-hom-nay")
def api_tuyen_hom_nay(req: TuyenRequest):
    ngay = req.ngay or datetime.now().strftime("%Y-%m-%d")
    key = f"tuyen:{ngay}:{req.phienId}"
    c = _r_get(key)
    if c: return {"data": c, "source": "redis"}
    with get_db() as conn:
        row = conn.execute("SELECT data FROM tuyen_cache WHERE date=? AND phien_id=?",
                           (ngay, req.phienId)).fetchone()
    if row:
        d = json.loads(row["data"]); _r_set(key, d, 300)
        return {"data": d, "source": "sqlite"}
    return {"data": None}   # GAS tự tính từ Sheet rồi đẩy lên /api/cache-tuyen

@app.post("/api/cache-tuyen")
def api_cache_tuyen(b: CacheTuyenRequest):
    ngay = b.ngay or datetime.now().strftime("%Y-%m-%d")
    _r_set(f"tuyen:{ngay}:{b.phienId}", b.data, 300)
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO tuyen_cache(date,phien_id,data,updated_at) VALUES (?,?,?,?)",
                     (ngay, b.phienId, json.dumps(b.data, ensure_ascii=False),
                      datetime.now().isoformat()))
        conn.commit()
    return {"status": "success"}

@app.post("/api/invalidate-tuyen-cache")
def api_invalidate_tuyen(req: TuyenRequest):
    ngay = req.ngay or datetime.now().strftime("%Y-%m-%d")
    _r_del_prefix(f"tuyen:{ngay}:")
    with get_db() as conn:
        conn.execute("DELETE FROM tuyen_cache WHERE date=?", (ngay,)); conn.commit()
    return {"status": "success"}

# ---------------- 4) THANH TOÁN NHANH ----------------
@app.post("/api/thanh-toan-nhanh")
def thanh_toan_nhanh(invoice: InvoiceRequest, background_tasks: BackgroundTasks):
    t0 = datetime.now()
    try:
        dump = invoice.model_dump() if hasattr(invoice, "model_dump") else invoice.dict()
        with get_db() as conn:
            conn.execute("INSERT OR REPLACE INTO invoices(ma_hd,data,status,created_at) VALUES (?,?,?,?)",
                         (invoice.maHD, json.dumps(dump, ensure_ascii=False),
                          "PENDING", t0.isoformat()))
            conn.commit()
        # Tồn kho sắp đổi ở Sheet → bỏ cache SP để lần sau nạp mới
        _r_del_prefix("pos:products")
        with get_db() as conn:
            conn.execute("DELETE FROM san_pham"); conn.commit()
        background_tasks.add_task(sync_to_google_sheets_and_notify, invoice.maHD)
        ms = int((datetime.now() - t0).total_seconds() * 1000)
        log_ai_action("PAYMENT", f"HD {invoice.maHD}", f"Tong {invoice.tongTien}", ms, "SUCCESS")
        return {"status": "success", "maHD": invoice.maHD}
    except Exception as e:
        log_ai_action("PAYMENT", f"HD {invoice.maHD}", str(e), 0, "FAILED")
        raise HTTPException(500, str(e))

# ---------------- 5) NÉN ẢNH ----------------
@app.post("/api/nen-anh")
async def nen_anh(file: UploadFile = File(...)):
    try:
        img = Image.open(io.BytesIO(await file.read()))
        if img.mode in ("RGBA", "P", "LA"): img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        q, buf = 75, io.BytesIO()
        img.save(buf, format="JPEG", quality=q, optimize=True)
        while buf.tell() > 900_000 and q > 30:
            q -= 10; buf = io.BytesIO(); img.save(buf, format="JPEG", quality=q, optimize=True)
        return {"b64": base64.b64encode(buf.getvalue()).decode()}
    except Exception as e:
        raise HTTPException(500, str(e))

# ---------------- 6) TÌM BẰNG HÌNH ẢNH (trả VỀ SP THẬT để chạm thêm giỏ được) ----------------
def _lay_ds_sp():
    c = _r_get("pos:products:index")
    if c and c.get("data"): return c["data"]
    with get_db() as conn:
        rows = conn.execute("SELECT raw FROM san_pham").fetchall()
    return [json.loads(x["raw"]) for x in rows]

@app.post("/api/tim-bang-anh")
async def tim_bang_anh(file: UploadFile = File(...)):
    contents = await file.read()
    key = f"img_search_{hashlib.md5(contents).hexdigest()}"
    c = _r_get(key)
    if c: return c
    result = {"tenAI": "", "danhSach": []}
    if GEMINI_API_KEY:
        try:
            resp = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
                json={"contents": [{"parts": [
                    {"text": 'Nhận diện sản phẩm tạp hoá trong ảnh. Trả về CHỈ JSON: {"ten": "..."}'},
                    {"inline_data": {"mime_type": file.content_type or "image/jpeg",
                                     "data": base64.b64encode(contents).decode()}}]}],
                    "generationConfig": {"response_mime_type": "application/json"}},
                timeout=20)
            if resp.status_code == 200:
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                ai = json.loads(text.replace("```json", "").replace("```", "").strip())
                ten_ai = str(ai.get("ten", "")).strip()
                result["tenAI"] = ten_ai
                kd = bo_dau(ten_ai).replace(" ", "")
                if kd:
                    result["danhSach"] = [
                        p for p in _lay_ds_sp()
                        if kd in bo_dau(p.get("ten", "")).replace(" ", "")
                        or bo_dau(p.get("ten", "")).replace(" ", "") in kd][:12]
        except Exception as e:
            log.error("Gemini lỗi: %s", e)
    _r_set(key, result, 3600)
    return result

# ---------------- 7) DỰ BÁO TỒN KHO ----------------
@app.get("/api/bao-cao-du-bao/{so_ngay}")
def bao_cao_du_bao(so_ngay: int = 7):
    cut = datetime.now() - timedelta(days=30)
    ban = {}
    with get_db() as conn:
        rows = conn.execute("SELECT data, created_at FROM invoices").fetchall()
    for x in rows:
        try:
            if datetime.fromisoformat(x["created_at"]) < cut: continue
            for it in json.loads(x["data"]).get("danhSach", []):
                k = bo_dau(it.get("ten", "")).strip()
                ban[k] = ban.get(k, 0) + (it.get("soLuong") or 0)
        except Exception: continue
    if not ban: return []
    out = []
    for p in _lay_ds_sp():
        tb = ban.get(bo_dau(p.get("ten", "")).strip(), 0) / 30.0
        if tb <= 0: continue
        ngay_con = (float(p.get("tonKho") or 0)) / tb
        if ngay_con <= so_ngay:
            out.append({"ten": p.get("ten"), "ngay_con": round(ngay_con, 1),
                        "muc_do": "nguy_hiem" if ngay_con <= 3 else "canh_bao"})
    out.sort(key=lambda x: x["ngay_con"])
    return out

# ---------------- 8) THÔNG BÁO BÁN HÀNG (alias sửa lỗi nối đuôi /api) ----------------
@app.post("/api/thong-bao-zalo")
@app.post("/api/api/thong-bao-zalo")
def thong_bao_zalo(b: ZaloRequest):
    ok = send_telegram_message(
        f"🧾 *{b.maHD}*\n👤 {b.khach}\n💰 {b.tong:,.0f}₫\n👨‍💼 {b.nv}\n🕒 {b.timestamp}")
    log_ai_action("NOTIFY", b.maHD, f"telegram={ok}", 0, "SUCCESS" if ok else "SKIP")
    return {"status": "ok", "telegram": ok}

# ---------------- 9) TIỆN ÍCH ----------------
@app.get("/api/test-telegram")
def test_telegram():
    return {"status": "success" if send_telegram_message("🤖 Test OK — POS GNP AI Bridge") else "error"}

@app.get("/api/logs")
def get_logs(limit: int = 10):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM ai_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"logs": [dict(x) for x in rows]}
