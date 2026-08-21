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


class SourcingRequest(BaseModel):
    """V2 选品分析：输入品类方向 → 规则引擎评估 → 推荐 SKU 方向。"""
    category: str = ""        # 品类方向（可选，空则给热门品类）
    budget: float = 5000.0    # 单 SKU 预算（¥）
    platform: str = "tiktok"  # tiktok | amazon


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


class CollectRequest(BaseModel):
    """F 反馈飞轮：把用户 👍 的素材收藏进知识库（数据飞轮，越用越强）。"""
    generation_id: int
    product: str = ""
    category: str = ""


@app.post("/api/collect")
def collect(req: CollectRequest):
    """
    F 数据飞轮：把质检通过 + 用户认可（👍）的素材收藏到 knowledge 收藏库。
    未来重建知识库时，收藏的素材作为「用户验证过的好 prompt」参与检索——
    让系统越用越懂"用户觉得什么好"。
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        # 读该 generation 的模板 prompt（收藏主素材 = 三模型 prompt）
        row = conn.execute(
            "SELECT product_name, category, templates FROM generations WHERE id=?", (req.generation_id,)
        ).fetchone()
        if not row:
            conn.close()
            return {"ok": False, "error": "素材不存在"}
        templates = json.loads(row[2] or "{}")

        conn.execute("""CREATE TABLE IF NOT EXISTS collected (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_id INTEGER, product TEXT, category TEXT,
            template_text TEXT, collected_at TEXT DEFAULT (datetime('now')),
            is_simulated INTEGER DEFAULT 0
        )""")
        # 收藏全部三模型 prompt（作为知识库回灌候选）
        for model, info in templates.items():
            conn.execute(
                "INSERT INTO collected(generation_id, product, category, template_text) VALUES(?,?,?,?)",
                (req.generation_id, row[0], row[1], info.get("prompt", "")),
            )
        conn.commit()
        conn.close()
        _log_db_event("collect_to_kb", f"generation={req.generation_id}")
        return {"ok": True, "collected_models": list(templates.keys())}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}


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


@app.post("/api/sourcing")
def sourcing(req: SourcingRequest):
    """
    V2 选品分析（规则引擎，不调 LLM 省成本）：
    输入品类方向/预算/平台 → 评估品类热度、利润空间、合规风险 → 推荐 SKU 方向。

    设计说明（可拷打）：
    - 规则引擎 vs LLM：选品评估用「可解释的规则」而非黑盒 LLM——面试可讲
      "确定性判断用规则（成本低、可解释），创意生成用 LLM（需要创造力）"
    - 品类热度：基于公开电商常识的评级模型（不是真实流量数据，诚实标注）
    - 利润估算：售价/采购成本/平台佣金/物流 → 毛利（参数模型）
    - 合规检查：平台规则常识清单（TikTok/Amazon）
    """
    # 品类知识库（公开常识评级，2026-08 校准；不是实时数据，诚实标注）
    CATEGORIES = {
        "3C数码": {"heat": 8, "margin": "medium", "note": "高热度高竞争，需差异化；手机配件/耳机/智能穿戴较稳", "platforms": ["tiktok", "amazon"]},
        "美妆个护": {"heat": 9, "margin": "high", "note": "美妆是 TikTok 转化王品类，但需成分合规（FDA/欧盟）", "platforms": ["tiktok"]},
        "运动户外": {"heat": 7, "margin": "medium", "note": "露营/健身器材热度上升，季节性明显", "platforms": ["tiktok", "amazon"]},
        "宠物用品": {"heat": 8, "margin": "high", "note": "宠物经济持续增长，智能喂食器/玩具需求稳", "platforms": ["tiktok", "amazon"]},
        "厨房家电": {"heat": 6, "margin": "low", "note": "客单价高但物流重、佣金高，适合 Amazon 不适合 TikTok", "platforms": ["amazon"]},
        "运动营养": {"heat": 7, "margin": "high", "note": "蛋白粉/补剂复购强，但各国食品合规严格", "platforms": ["tiktok"]},
        "家居日用": {"heat": 6, "margin": "low", "note": "需求稳定竞争激烈，利润薄，适合走量", "platforms": ["amazon"]},
    }
    # 合规规则（平台常识清单，非实时政策）
    COMPLIANCE = {
        "tiktok": ["广告素材需标注「广告」", "夸大功效类文案易被拒（减肥/医疗）", "音乐版权需授权", "目标市场当地语言"],
        "amazon": ["主图白底规范", "五点描述不得含促销词（free/sale）", "需类目审核（美妆/食品）", "专利侵权风险自查"],
    }

    # 候选品类：指定了就用指定，否则给热门
    if req.category and req.category in CATEGORIES:
        cand = [req.category]
    elif req.category:
        cand = [c for c in CATEGORIES if req.category in c]
    else:
        cand = ["美妆个护", "3C数码", "宠物用品", "运动户外", "运动营养"]
    cand = [c for c in cand if c in CATEGORIES]
    if not cand:
        cand = ["3C数码"]

    results = []
    for cat in cand:
        info = CATEGORIES[cat]
        if req.platform not in info["platforms"]:
            continue
        # 利润估算（参数模型，可调）
        if info["margin"] == "high":
            est_profit_pct, risk = 0.35, "中"
        elif info["margin"] == "medium":
            est_profit_pct, risk = 0.25, "中低"
        else:
            est_profit_pct, risk = 0.15, "低"
        est_profit = round(req.budget * est_profit_pct, 0)
        results.append({
            "category": cat,
            "heat": info["heat"],
            "heat_label": "🔥 高热度" if info["heat"] >= 8 else "🌟 中高热度",
            "est_profit": est_profit,
            "est_profit_pct": int(est_profit_pct * 100),
            "note": info["note"],
            "compliance": COMPLIANCE.get(req.platform, []),
        })
    results.sort(key=lambda r: (-r["heat"], -r["est_profit"]))

    _log_db_event("sourcing_analyze", req.category or "热门")
    return {
        "ok": True,
        "platform": req.platform,
        "budget": req.budget,
        "results": results,
        "note": "规则引擎评估（公开常识评级，非实时流量数据，结论方向性）",
    }


@app.get("/api/export/{item_id}")
def export_item(item_id: int):
    """I 导出功能：素材包导出为 Markdown 文件（可直接保存分享）。"""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT id, product_name, category, selling_points, tiktok_copy, visual_plan, storyboard, templates, quality, created_at "
        "FROM generations WHERE id=?", (item_id,)
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "素材不存在"}
    selling = json.loads(row[3] or "[]")
    copy = json.loads(row[4] or "{}")
    visual = json.loads(row[5] or "{}")
    storyboard = json.loads(row[6] or "{}")
    templates = json.loads(row[7] or "{}")
    quality = json.loads(row[8] or "{}")

    md = [f"# {row[1]} 广告素材包", ""]
    md.append(f"> 生成时间：{row[9]} ｜ 类目：{row[2]} ｜ 质检分：{quality.get('overall', '—')}")
    md.append("")
    md.append("## 卖点分析")
    for i, p in enumerate(selling, 1):
        md.append(f"{i}. {p}")
    md.append("")
    md.append("## 广告文案")
    if isinstance(copy, dict):
        if copy.get("hook"):
            md.append(f"- 开头钩子：{copy['hook']}")
        if copy.get("captions"):
            md.append("- 场景文案：")
            for c in copy["captions"]:
                md.append(f"  - {c}")
    md.append("")
    md.append("## 视觉方案")
    if visual.get("style"):
        md.append(f"- 风格：{visual['style']}")
    if visual.get("composition"):
        md.append(f"- 构图：{visual['composition']}")
    if visual.get("mood"):
        md.append(f"- 情绪：{visual['mood']}")
    md.append("")
    md.append("## 分镜脚本")
    for s in storyboard.get("shots", []):
        md.append(f"- **镜头{s.get('shot_no')}**（{s.get('scene_type')}，{s.get('camera')}，{s.get('duration')}）：{s.get('description')}")
        if s.get("copy"):
            md.append(f"  - 文案：{s['copy']}")
    md.append("")
    md.append("## 三模型 Prompt")
    for model, info in templates.items():
        md.append(f"### {info.get('label', model)}")
        md.append("```")
        md.append(info.get("prompt", ""))
        md.append("```")
        md.append("")

    _log_db_event("export_item", f"generation={item_id}")
    return {"ok": True, "filename": f"{row[1]}_素材包.md", "markdown": "\n".join(md)}


# 前端静态文件
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
