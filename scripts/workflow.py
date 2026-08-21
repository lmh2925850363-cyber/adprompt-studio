# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D4 六步完整工作流
====================================
商品信息 → RAG 检索 → 卖点分析 → 广告文案 → 视觉方案 → 分镜生成 → 多模型模板适配 → 素材包落库+埋点

输入：商品信息（名称/类目/描述）
输出：完整素材包（卖点 + TikTok/Amazon 文案 + 视觉方案 + 分镜 + 三模型 prompt），存 SQLite 并埋点

关键设计（面试讲解用）：
1. RAG 检索：从知识库按语义相似度取 Top-3 范文作为"参考"，让 AI 模仿好结构写
2. 六步分工：每步专注一个任务（可观测可调试可复用）
3. 多模型适配：纯规则模板（零成本）把分镜翻译成即梦/可灵/Seedance 2 三套 prompt
4. 成本控制：单 case 约 4 次 LLM 调用，成本 < ¥0.02

用法：python scripts/workflow.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

# 国内网络：模型走 hf-mirror 镜像，缓存走 E 盘（避免 C 盘爆）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", "E:\\hf-cache")
os.environ.setdefault("HF_HUB_CACHE", "E:\\hf-cache\\hub")
os.environ.setdefault("HF_HUB_OFFLINE", "1")  # 模型已缓存，纯离线加载

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"
KB_DIR = REPO_DIR / "data" / "knowledge"
ENV_PATH = REPO_DIR / ".env"

# 加载 .env（key 不写进代码）
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

# 测试用例 1：运动相机（模拟真实卖家输入）
TEST_PRODUCT = {
    "name": "YepCam X5 运动相机",
    "category": "3C数码",
    "description": "4K 防水运动相机，裸机防水 10 米，带防抖技术，适合骑行、徒步、潜水等户外运动拍摄。超长续航，磁吸快拆设计。",
}


def load_env_key() -> str:
    if not DEEPSEEK_API_KEY:
        print("[错误] 未找到 DEEPSEEK_API_KEY，请先在 adprompt-studio/.env 中配置。")
        print("  格式：DEEPSEEK_API_KEY=sk-xxx")
        sys.exit(1)
    return DEEPSEEK_API_KEY


def rag_retrieve(query: str, top_k: int = 3) -> list[dict]:
    """从知识库检索最相关范文（numpy 余弦相似度，轻量零依赖）。"""
    import json as _json
    import numpy as np
    from sentence_transformers import SentenceTransformer

    if not (KB_DIR / "embeddings.npy").exists():
        print("[错误] 知识库不存在，请先运行 build_knowledge_base.py")
        sys.exit(1)

    embeddings = np.load(KB_DIR / "embeddings.npy")
    chunks = _json.loads((KB_DIR / "chunks.json").read_text(encoding="utf-8"))
    assert len(embeddings) == len(chunks), "向量数与文本数不一致，请重建知识库"

    model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    q_emb = model.encode([query], normalize_embeddings=True)  # (1, 512)
    sims = embeddings @ q_emb.T  # (1015, 512) @ (512, 1) -> (1015, 1)
    sims = sims.flatten()
    top_idx = np.argsort(-sims)[:top_k]
    docs = []
    for i in top_idx:
        c = chunks[int(i)]
        docs.append({"source_id": c["source_id"], "text": c["text"], "similarity": round(float(sims[i]), 3)})
    return docs


def call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 2000) -> str:
    """调用 DeepSeek API（OpenAI 兼容协议）。"""
    from openai import OpenAI

    client = OpenAI(api_key=load_env_key(), base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.7,
    )
    return resp.choices[0].message.content


def parse_json(text: str) -> dict:
    """解析 LLM 输出的 JSON（容忍 markdown 代码块包裹）。"""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def analyze_selling_points(product: dict, refs: list[dict]) -> list[str]:
    """③ 卖点分析：AI 从商品描述自动提取 3-5 个核心卖点。"""
    sys_prompt = (
        "你是出海电商广告专家。根据商品信息提取 3-5 个核心卖点，"
        "每个卖点一句话，用中文，直接输出 JSON 数组，如 [\"卖点1\", \"卖点2\"]。"
    )
    ref_text = "\n\n".join(f"[范文{i+1}]: {r['text'][:500]}" for i, r in enumerate(refs))
    user_prompt = (
        f"商品名称：{product['name']}\n"
        f"商品类目：{product['category']}\n"
        f"商品描述：{product['description']}\n\n"
        f"参考范文（模仿其广告视角）：\n{ref_text}\n\n"
        "请提取核心卖点，输出 JSON 数组："
    )
    raw = call_llm(sys_prompt, user_prompt, max_tokens=500)
    points = parse_json(raw)
    return points if isinstance(points, list) else [str(points)]


def generate_storyboard(product: dict, selling_points: list[str], refs: list[dict]) -> dict:
    """④ 分镜生成：参照范文结构产出结构化分镜 JSON。"""
    sys_prompt = (
        "你是出海电商广告视频分镜师。根据商品信息和卖点，生成 6-8 个镜头的分镜脚本。\n"
        "输出 JSON，格式："
        '{"shots": [{"shot_no": 1, "scene_type": "景别", "camera": "运镜", '
        '"description": "画面描述", "copy": "广告文案", "duration": "时长秒"}]}'
        "所有字段用中文或英文均可，description 要具体可拍摄。"
    )
    ref_text = "\n\n".join(f"[范文{i+1}]: {r['text'][:600]}" for i, r in enumerate(refs))
    user_prompt = (
        f"商品：{product['name']}（{product['category']}）\n"
        f"描述：{product['description']}\n"
        f"卖点：{'、'.join(selling_points)}\n\n"
        f"参考范文结构：\n{ref_text}\n\n"
        "生成 6-8 镜头分镜，输出 JSON："
    )
    raw = call_llm(sys_prompt, user_prompt, max_tokens=2500)
    return parse_json(raw)


def generate_copy(product: dict, selling_points: list[str], platform: str) -> dict:
    """③ 广告文案生成（按平台：TikTok 短文案 + Amazon 五点描述）。"""
    if platform == "amazon":
        sys_prompt = (
            "你是亚马逊电商文案专家。根据商品和卖点，输出 5 点描述（bullet points），"
            "直接输出 JSON 数组，每点一句，突出卖点与买家利益，如 [\"点1\", \"点2\"]。"
        )
        user_prompt = (
            f"商品：{product['name']}（{product['category']}）\n"
            f"描述：{product['description']}\n"
            f"卖点：{'、'.join(selling_points)}\n\n"
            "输出 Amazon 五点描述 JSON 数组："
        )
    else:  # tiktok
        sys_prompt = (
            "你是 TikTok 短视频广告文案专家。根据商品和卖点，输出 1 条 30 字内抓眼球的开头钩子文案 "
            "+ 3 条 15 字内的场景短文案，直接输出 JSON 对象 "
            '{"hook": "...", "captions": ["...", "...", "..."]}。'
        )
        user_prompt = (
            f"商品：{product['name']}（{product['category']}）\n"
            f"描述：{product['description']}\n"
            f"卖点：{'、'.join(selling_points)}\n\n"
            "输出 TikTok 文案 JSON："
        )
    raw = call_llm(sys_prompt, user_prompt, max_tokens=800)
    return parse_json(raw)


def generate_visual_plan(product: dict, selling_points: list[str]) -> dict:
    """④ 视觉方案：风格/色板/构图（文字骨架，零成本；示例图用免费额度另做）。"""
    sys_prompt = (
        "你是出海电商视觉设计师。根据商品和卖点输出视觉方案，直接输出 JSON 对象："
        '{"style": "整体风格一句话", "palette": [{"color": "色名", "hex": "#xxxxxx", "usage": "用途"}], '
        '"composition": "构图建议一句话", "mood": "情绪基调一句话"}。'
    )
    user_prompt = (
        f"商品：{product['name']}（{product['category']}）\n"
        f"描述：{product['description']}\n"
        f"卖点：{'、'.join(selling_points)}\n\n"
        "输出视觉方案 JSON："
    )
    raw = call_llm(sys_prompt, user_prompt, max_tokens=800)
    return parse_json(raw)


def log_event(event: str, payload: str = "") -> None:
    """埋点：记录事件到 SQLite events 表（D7 模拟用户/漏斗分析用）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, payload TEXT, created_at TEXT DEFAULT (datetime('now')))")
    conn.execute("INSERT INTO events(event, payload) VALUES(?,?)", (event, payload))
    conn.commit()
    conn.close()


def render_prompts(storyboard: dict, product_name: str, category: str) -> dict:
    """⑤ 多模型模板适配：分镜 JSON → 三套模型 prompt（纯规则，零成本）。"""
    from src.prompt_templates import render_all_templates
    return render_all_templates(storyboard, product_name, category)


def main() -> None:
    product = TEST_PRODUCT
    print("=== D4 六步完整工作流（测试 case：YepCam X5 运动相机）===\n")

    print("[1/6] RAG 检索范文...")
    refs = rag_retrieve(f"{product['name']} {product['description']}", top_k=3)
    for i, r in enumerate(refs):
        print(f"  范文{i+1}: {r['source_id']} 相似度{r['similarity']}")

    print("\n[2/6] 卖点分析（DeepSeek）...")
    selling_points = analyze_selling_points(product, refs)
    print("  卖点:", "、".join(selling_points))

    print("\n[3/6] 广告文案生成（DeepSeek, TikTok）...")
    tiktok_copy = generate_copy(product, selling_points, "tiktok")
    print("  hook:", tiktok_copy.get("hook", ""))
    print("  captions:", " / ".join(tiktok_copy.get("captions", [])[:2]))

    print("\n[4/6] 视觉方案（DeepSeek）...")
    visual = generate_visual_plan(product, selling_points)
    print("  风格:", visual.get("style", ""))
    print("  色板:", ", ".join(p.get("hex", "") for p in visual.get("palette", [])[:3]))

    print("\n[5/6] 分镜生成（DeepSeek）...")
    storyboard = generate_storyboard(product, selling_points, refs)
    shots = storyboard.get("shots", [])
    print(f"  生成 {len(shots)} 个镜头：")
    for s in shots[:3]:
        print(f"    镜头{s.get('shot_no')} [{s.get('scene_type')}] {s.get('camera')}: {str(s.get('description'))[:60]}")

    print("\n[6/6] 多模型模板适配（纯规则）+ 落库 + 埋点...")
    templates = render_prompts(storyboard, product["name"], product["category"])
    for model, info in templates.items():
        print(f"  {model}: {info['label']}（{len(info['prompt'])} 字符）")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_name TEXT, category TEXT,
        selling_points TEXT, tiktok_copy TEXT, visual_plan TEXT,
        storyboard TEXT, templates TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )""")
    conn.execute(
        "INSERT INTO generations(product_name, category, selling_points, tiktok_copy, visual_plan, storyboard, templates) "
        "VALUES(?,?,?,?,?,?,?)",
        (product["name"], product["category"],
         json.dumps(selling_points, ensure_ascii=False),
         json.dumps(tiktok_copy, ensure_ascii=False),
         json.dumps(visual, ensure_ascii=False),
         json.dumps(storyboard, ensure_ascii=False),
         json.dumps(templates, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    log_event("prompt_generate_success", product["name"])
    log_event("templates_rendered", ",".join(templates.keys()))

    print("  已入库 generations 表（含三套模板）+ 埋点 2 个事件")
    print("\nD4 六步工作流跑通！下一步：D5 质检器（LLM-as-judge）")


if __name__ == "__main__":
    sys.exit(main())
