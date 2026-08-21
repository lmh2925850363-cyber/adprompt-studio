# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D2 知识库构建脚本
====================================
输入：data/adprompt.db 的 seedance_prompts 表（D1 已清洗落库的 400 条真实范文）
输出：data/knowledge/ 下的轻量向量库（embeddings.npy + chunks.json）

做什么（小白版）：
- 把 400 条"优秀 prompt 范文"翻译成 AI 能懂的"向量语言"（embedding）
- 存进向量库，以后用户输入商品信息时，能按"语义相似度"检索最相关的范文

为什么选 bge-small-zh：
- 本地运行、免费（不依赖云 API，成本 ¥0）
- 中文效果好（用户输入是中文商品描述，范文是英文 prompt，跨语言检索靠它）
- 模型小（约 100MB），CPU 就能跑，2 周内可控

为什么分块：
- 范文最长 11207 字符，单条整体 embedding 会稀释语义（开头卖点 + 结尾时间轴混在一起）
- 按段落分块后，每块表达一个完整语义单元，检索更精准

为什么用 numpy 不用 ChromaDB（2026-08-19 决策）：
- 数据仅 1015 条（矩阵约 2MB），numpy 暴力余弦检索毫秒级，无需重型向量库
- ChromaDB 在 Python 3.13 有 HNSW 索引兼容坑（github issue #5925），跨进程加载失败
- 工程原则：规模小时选最小可行方案；规模大了再换专业向量库，检索接口保持不变

用法：python scripts/build_knowledge_base.py
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"
KB_DIR = REPO_DIR / "data" / "knowledge"

# 分块参数（上下文工程：结构 > 体积，小块高密度）
MAX_CHUNK_CHARS = 800      # 单块最大字符数
OVERLAP_CHARS = 100        # 块间重叠，避免切断语义（保证句子完整性）

# 模型选择：bge-small-zh-v1.5（BAAI 开源，中文检索 SOTA 小模型）
# 走 hf-mirror 镜像下载模型权重（国内网络）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
MODEL_NAME = "BAAI/bge-small-zh-v1.5"


def split_prompt(prompt: str) -> list[str]:
    """按段落 + 字符上限分块，块间保留重叠，避免切断语义。"""
    # 先按换行分段（prompt 里通常每段是一个镜头/一个语义单元）
    paragraphs = [p.strip() for p in prompt.split("\n") if p.strip()]
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        # 单段超长（>MAX），按句子进一步切
        while len(para) > MAX_CHUNK_CHARS:
            split_at = para.rfind(". ", 0, MAX_CHUNK_CHARS)
            if split_at == -1:
                split_at = MAX_CHUNK_CHARS
            piece = para[: split_at + 1]
            chunks.append(piece)
            para = para[split_at + 1 :].strip()
        if para:
            if len(current) + len(para) + 1 <= MAX_CHUNK_CHARS:
                current = f"{current}\n{para}".strip() if current else para
            else:
                chunks.append(current)
                # 重叠：保留上一块尾部 OVERLAP_CHARS 字符
                overlap = current[-OVERLAP_CHARS:] if len(current) > OVERLAP_CHARS else current
                current = f"{overlap}\n{para}".strip()

    if current:
        chunks.append(current)
    return chunks


def load_prompts() -> list[dict]:
    """从 SQLite 读取全部范文（真实数据，is_simulated=0）。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT source_id, category, prompt FROM seedance_prompts WHERE is_simulated=0"
    ).fetchall()
    conn.close()
    return [{"source_id": r[0], "category": r[1], "prompt": r[2]} for r in rows]


def main() -> None:
    print("=== D2 知识库构建 ===\n")
    prompts = load_prompts()
    print(f"[1/4] 读取范文: {len(prompts)} 条")

    # 分块
    all_chunks = []
    for p in prompts:
        chunks = split_prompt(p["prompt"])
        for i, c in enumerate(chunks):
            all_chunks.append({
                "source_id": p["source_id"],
                "category": p["category"],
                "chunk_index": i,
                "text": c,
            })
    print(f"[2/4] 分块完成: {len(prompts)} 条 → {len(all_chunks)} 个块 "
          f"(平均每篇 {len(all_chunks)/max(len(prompts),1):.1f} 块)")
    print(f"      块长度范围: {min(len(c['text']) for c in all_chunks)} - "
          f"{max(len(c['text']) for c in all_chunks)} 字符")

    # 加载 embedding 模型（首次会从 hf-mirror 下载 ~100MB 权重）
    print(f"[3/4] 加载 embedding 模型: {MODEL_NAME}（首次运行会下载，约 1-2 分钟）...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME)
    texts = [c["text"] for c in all_chunks]
    embeddings = model.encode(texts, normalize_embeddings=True, show_progress_bar=True)
    print(f"      embedding 完成: {embeddings.shape}")

    # 写入轻量向量库：numpy .npy + metadata JSON
    # 为什么不用 ChromaDB：数据仅 1015 条（矩阵约 2MB），numpy 暴力余弦检索毫秒级；
    # ChromaDB 在 Python 3.13 有 HNSW 索引兼容坑（github issue #5925），且引入重型依赖。
    # 工程决策：规模小时选最小可行方案，规模大了再换专业向量库（ChromaDB/Weaviate）——接口留好即可。
    print(f"[4/4] 写入轻量向量库: {KB_DIR}")
    KB_DIR.mkdir(parents=True, exist_ok=True)
    import numpy as np

    np.save(KB_DIR / "embeddings.npy", embeddings)  # (N, 512) float32
    with open(KB_DIR / "chunks.json", "w", encoding="utf-8") as f:
        json.dump(all_chunks, f, ensure_ascii=False, indent=1)
    print(f"      入库完成: {embeddings.shape[0]} 个向量 (embeddings.npy + chunks.json)")

    # 验证：检索测试（模拟用户输入中文商品描述）
    print("\n=== 检索验证（模拟真实查询）===")
    test_queries = [
        "卖点是防水的户外运动相机，适合骑行拍摄",
        "一款给妈妈的节日礼物香水，温馨优雅",
        "健身补剂蛋白粉，突出增肌效果",
    ]
    for q in test_queries:
        q_emb = model.encode(q, normalize_embeddings=True)  # (1, 512)
        sims = (embeddings @ q_emb.T).flatten()  # 余弦相似度（已归一化）
        top_idx = np.argsort(-sims)[:2]
        print(f"\n  查询: 「{q}」")
        for rank, i in enumerate(top_idx, 1):
            c = all_chunks[i]
            print(f"    [{rank}] 来源 {c['source_id']} ({c['category']}) 相似度 {sims[i]:.3f}: {c['text'][:80]}...")

    print("\n知识库构建完成！下一步（D3）: 工作流主体——商品信息 → RAG 检索 → 卖点分析 → 分镜生成")


if __name__ == "__main__":
    sys.exit(main())
