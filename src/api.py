# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D6 后端 API（FastAPI）
========================================
提供两个能力：
1. POST /api/generate：接收商品信息 → 跑六步工作流 → 返回完整素材包（含三模板 + 质检分）
2. POST /api/event：埋点上报（前端页面事件 → SQLite events 表，D7/D8 分析用）

设计要点：
- 复用 scripts/workflow.py 的六步逻辑（不重复实现）
- 埋点是独立端点：前端所有事件（页面访问/点击/复制/点赞）都走这里，数据闭环从这里开始
- 同步处理生成请求（MVP 简单可靠；量大后可改异步任务队列——面试可讲这个演进）

用法：uvicorn src.api:app --reload --port 8000
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"
FRONTEND_DIR = REPO_DIR / "frontend"

# 让 scripts 可作为模块导入
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(REPO_DIR / "scripts"))
sys.path.insert(0, str(REPO_DIR / "src"))

from workflow import (  # noqa: E402
    rag_retrieve, analyze_selling_points, generate_copy,
    generate_visual_plan, generate_storyboard, log_event,
)
from src.prompt_templates import render_all_templates  # noqa: E402
from src.quality_judge import judge_storyboard  # noqa: E402

app = FastAPI(title="AdPrompt Studio API", version="0.1.0")

# CORS：允许前端本地访问（开发期全开）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class GenerateRequest(BaseModel):
    name: str
    category: str = ""
    description: str = ""
    platform: str = "tiktok"  # tiktok | amazon


class EventRequest(BaseModel):
    event: str
    payload: str = ""
    is_simulated: int = 0  # 0=真实用户事件（前端），1=模拟数据（D7 脚本）


def _log_db_event(event: str, payload: str = "", is_simulated: int = 0) -> None:
    """写事件到 SQLite（供 D7 模拟用户 + D8 SQL 分析）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, payload TEXT,
            created_at TEXT DEFAULT (datetime('now')), is_simulated INTEGER DEFAULT 0
        )"""
    )
    conn.execute("INSERT INTO events(event, payload, is_simulated) VALUES(?,?,?)", (event, payload, is_simulated))
    conn.commit()
    conn.close()


@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/dashboard")
def dashboard():
    """数据看板（D12）：漏斗 / cohort 热力图 / 成本趋势。"""
    dash = FRONTEND_DIR / "dashboard.html"
    if not dash.exists():
        return {"error": "看板未生成，先运行 python scripts/build_dashboard.py"}
    return FileResponse(dash)


@app.get("/api/health")
def health():
    return {"status": "ok", "db": str(DB_PATH.exists())}


@app.post("/api/event")
def track_event(req: EventRequest):
    """埋点上报端点：前端所有事件都打到这里。"""
    _log_db_event(req.event, req.payload, req.is_simulated)
    return {"ok": True}


@app.post("/api/generate")
def generate(req: GenerateRequest):
    """核心：生成完整素材包（六步工作流 + 质检）。"""
    t0 = time.time()
    product = {"name": req.name, "category": req.category, "description": req.description}

    # 埋点：生成开始
    _log_db_event("prompt_generate_click", product["name"])

    try:
        # ① RAG 检索
        refs = rag_retrieve(f"{product['name']} {product['description']}", top_k=3)
        # ② 卖点分析
        points = analyze_selling_points(product, refs)
        # ③ 广告文案（按平台）
        copy = generate_copy(product, points, req.platform)
        # ④ 视觉方案
        visual = generate_visual_plan(product, points)
        # ⑤ 分镜生成
        storyboard = generate_storyboard(product, points, refs)
        # 质检（D5）：打分 + 低分自动重写（最多 2 轮）
        judge_result = judge_storyboard(storyboard, product)
        quality = {
            "style_score": judge_result["style_score"],
            "usability_score": judge_result["usability_score"],
            "overall": round(judge_result["overall"], 2),
            "pass": judge_result["pass"],
            "suggestions": judge_result["suggestions"],
        }
        _log_db_event("quality_pass" if quality["pass"] else "quality_fail", product["name"])
        # ⑥ 多模型模板适配
        templates = render_all_templates(storyboard, product["name"], product["category"])

        # 落库 generations
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_name TEXT, category TEXT,
                selling_points TEXT, tiktok_copy TEXT, visual_plan TEXT,
                storyboard TEXT, templates TEXT, quality TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )"""
        )
        conn.execute(
            "INSERT INTO generations(product_name, category, selling_points, tiktok_copy, visual_plan, storyboard, templates, quality) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (product["name"], product["category"],
             json.dumps(points, ensure_ascii=False),
             json.dumps(copy, ensure_ascii=False),
             json.dumps(visual, ensure_ascii=False),
             json.dumps(storyboard, ensure_ascii=False),
             json.dumps(templates, ensure_ascii=False),
             json.dumps(quality, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

        _log_db_event("prompt_generate_success", product["name"])

        return {
            "ok": True,
            "elapsed_s": round(time.time() - t0, 1),
            "product": product,
            "selling_points": points,
            "copy": copy,
            "visual": visual,
            "storyboard": storyboard,
            "templates": templates,
            "quality": quality,
        }
    except Exception as e:  # noqa: BLE001
        _log_db_event("prompt_generate_fail", f"{product['name']}: {e!r}")
        return {"ok": False, "error": repr(e)}


# 前端静态文件
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
