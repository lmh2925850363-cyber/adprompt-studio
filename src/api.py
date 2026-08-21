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
    generate_visual_plan, generate_storyboard, log_event, call_llm,
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


class OptimizeRequest(BaseModel):
    """Prompt 优化模式（P1）：贴差 prompt → RAG 检索范文 → 改进版 + 解释。"""
    prompt: str
    model: str = "seedance"  # 目标模型：seedance | jimeng | kling
    context: str = ""        # 可选：商品/场景描述，帮助检索更准


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


@app.post("/api/optimize")
def optimize(req: OptimizeRequest):
    """
    Prompt 优化模式（P1，第二个功能入口）：
    用户贴一个"感觉不好用"的 prompt → RAG 检索知识库最佳实践范文 →
    LLM 生成改进版 + 解释改了哪里、为什么。

    产品价值：解决卖家"prompt 写不好但不知道怎么改"的问题——
    不是从零生成，而是把差的改好，并让用户学会（解释改了什么）。
    """
    t0 = time.time()
    if not req.prompt.strip():
        return {"ok": False, "error": "请粘贴要优化的 prompt"}

    _log_db_event("prompt_optimize_click", req.model)
    try:
        # ① RAG 检索最佳实践范文（用差 prompt + 可选上下文作为查询）
        query = f"{req.context} {req.prompt}"[:500] if req.context else req.prompt[:500]
        refs = rag_retrieve(query, top_k=3)
        ref_text = "\n\n".join(f"[范文{i+1}]: {r['text'][:500]}" for i, r in enumerate(refs))

        # ② LLM 生成改进版 + 解释
        sys_prompt = (
            "你是视频生成 prompt 优化专家。用户会粘贴一个效果不佳的 prompt，"
            "参考知识库里的最佳实践范文，输出优化后的版本并解释改了哪里。\n"
            "输出 JSON："
            '{"optimized_prompt": "改进后的完整 prompt", '
            '"changes": [{"what": "改了什么", "why": "为什么这么改"}], '
            '"summary": "一句话总结"}'
        )
        user_prompt = (
            f"目标模型：{req.model}\n"
            f"用户的原 prompt：\n{req.prompt}\n\n"
            f"参考最佳实践范文：\n{ref_text}\n\n"
            "请优化并解释，输出 JSON："
        )
        raw = call_llm(sys_prompt, user_prompt, max_tokens=2500)
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text.strip())

        # ③ 落库（素材库可回看优化记录）+ 埋点
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""CREATE TABLE IF NOT EXISTS optimizations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_prompt TEXT, model TEXT, context TEXT,
            optimized_prompt TEXT, changes TEXT, summary TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.execute(
            "INSERT INTO optimizations(original_prompt, model, context, optimized_prompt, changes, summary) VALUES(?,?,?,?,?,?)",
            (req.prompt, req.model, req.context,
             result.get("optimized_prompt", ""),
             json.dumps(result.get("changes", []), ensure_ascii=False),
             result.get("summary", "")),
        )
        conn.commit()
        conn.close()
        _log_db_event("prompt_optimize_success", req.model)

        return {
            "ok": True,
            "elapsed_s": round(time.time() - t0, 1),
            "optimized_prompt": result.get("optimized_prompt", ""),
            "changes": result.get("changes", []),
            "summary": result.get("summary", ""),
            "references": [r["source_id"] for r in refs],
        }
    except Exception as e:  # noqa: BLE001
        _log_db_event("prompt_optimize_fail", f"{req.model}: {e!r}")
        return {"ok": False, "error": repr(e)}


@app.get("/api/library")
def library():
    """素材库：历史生成的素材包列表（第二个功能入口，工作台感）。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, product_name, category, created_at, quality FROM generations ORDER BY id DESC LIMIT 50"
    ).fetchall()
    conn.close()
    items = []
    for r in rows:
        quality = {}
        try:
            quality = json.loads(r[4] or "{}")
        except json.JSONDecodeError:
            pass
        items.append({
            "id": r[0], "product": r[1], "category": r[2],
            "created_at": r[3], "quality_overall": quality.get("overall"),
        })
    return {"ok": True, "count": len(items), "items": items}


@app.get("/api/library/{item_id}")
def library_detail(item_id: int):
    """素材库详情：单个素材包完整内容（含三模板 + 分镜）。"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, product_name, category, selling_points, tiktok_copy, visual_plan, storyboard, templates, quality, created_at "
        "FROM generations WHERE id=?", (item_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "素材不存在"}
    return {
        "ok": True,
        "item": {
            "id": row[0], "product": row[1], "category": row[2],
            "selling_points": json.loads(row[3] or "[]"),
            "copy": json.loads(row[4] or "{}"),
            "visual": json.loads(row[5] or "{}"),
            "storyboard": json.loads(row[6] or "{}"),
            "templates": json.loads(row[7] or "{}"),
            "quality": json.loads(row[8] or "{}"),
            "created_at": row[9],
        },
    }


# 前端静态文件
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
