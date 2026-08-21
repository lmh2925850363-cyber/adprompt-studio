# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D4 多模型 Prompt 模板适配引擎
================================================
把结构化分镜 JSON 翻译成三个视频模型的专属 prompt。

为什么需要（产品差异化卖点）：
- 同一个分镜，直接粘给三个模型 = 三个都生成不好（语言/结构/风格完全不同）
- 即梦/可灵要中文分段式，Seedance 2 要英文时间轴格式
- 面向卖家的"多模型模板适配"是 AdPrompt Studio 区别于即梦自带提示词优化的核心差异

设计原则：
- 纯规则模板（不调 LLM）→ 零成本、确定性、可测试
- 模板规则集中在此文件，后续可调（如改 Seedance 版式）
- 三模型输出统一结构：{model: {prompt, format_note}}

用法：from src.prompt_templates import render_all_templates
"""
import json
from pathlib import Path

# 模板规则（集中管理，可调）
MODEL_TEMPLATES = {
    "jimeng": {
        "label": "即梦（中文分段式）",
        "format_note": "中文提示词，分段描述镜头，适合即梦/剪映等中文视频模型",
    },
    "kling": {
        "label": "可灵（中文镜头序列）",
        "format_note": "中文提示词，编号镜头序列，适合可灵/海螺等",
    },
    "seedance": {
        "label": "Seedance 2（中文时间轴）",
        "format_note": "时间轴分段式（0-5s/5-10s/10-15s），中文描述，适合 Seedance 2（已按 Owner 决策出中文版）",
    },
}


def _shots_to_segments(shots: list[dict]) -> list[dict]:
    """把分镜 JSON 统一成镜头列表（兼容缺字段）。"""
    segments = []
    for s in shots:
        segments.append({
            "no": s.get("shot_no", len(segments) + 1),
            "scene_type": s.get("scene_type", ""),
            "camera": s.get("camera", ""),
            "description": s.get("description", ""),
            "copy": s.get("copy", ""),
            "duration": s.get("duration", ""),
        })
    return segments


def render_jimeng(storyboard: dict, product_name: str) -> str:
    """即梦版：中文分段式描述，镜头逐段列出。"""
    shots = _shots_to_segments(storyboard.get("shots", []))
    parts = [f"为 {product_name} 生成一支电商广告短视频，电影感画质，逐镜头描述："]
    for s in shots:
        line = (f"镜头{s['no']}（{s['scene_type']}，{s['camera']}，{s['duration']}）："
                f"{s['description']}。画面文案：{s['copy']}")
        parts.append(line)
    parts.append("整体风格统一，产品始终清晰，结尾露出品牌 logo。")
    return "\n".join(parts)


def render_kling(storyboard: dict, product_name: str) -> str:
    """可灵版：中文编号镜头序列，突出运镜流畅。"""
    shots = _shots_to_segments(storyboard.get("shots", []))
    parts = [f"为 {product_name} 生成电商广告短视频，运镜流畅，镜头序列如下："]
    for s in shots:
        line = (f"{s['no']}. 景别：{s['scene_type']}；运镜：{s['camera']}；"
                f"画面：{s['description']}；时长：{s['duration']}；文案：{s['copy']}")
        parts.append(line)
    parts.append("要求：镜头衔接自然，主体稳定，广告感强，结尾品牌定格。")
    return "\n".join(parts)


def render_seedance(storyboard: dict, product_name: str, category: str) -> str:
    """Seedance 2 版：中文时间轴分段式（Owner 决策：出中文版，语言统一、零额外成本）。"""
    shots = _shots_to_segments(storyboard.get("shots", []))
    parts = [
        f"为 {product_name} 生成一支 {category} 类 15 秒电商广告短视频，"
        "超写实画质，电影级光影，产品外观全程一致，无变形无伪影。",
    ]
    # 时间轴分段：把 6-8 个镜头均匀分到 3 个时间段
    buckets = [shots[i::3] for i in range(3)]  # 均匀分 3 组
    time_ranges = ["0-5 秒", "5-10 秒", "10-15 秒"]
    for bucket, tr in zip(buckets, time_ranges):
        if not bucket:
            continue
        desc = " ".join(
            f"镜头{s['no']}（{s['scene_type']}，{s['camera']}）：{s['description']}"
            for s in bucket
        )
        parts.append(f"{tr}：{desc}")
    parts.append("结尾：品牌 logo 定格，干净背景，高端商业质感收尾。")
    return "\n\n".join(parts)


def render_all_templates(storyboard: dict, product_name: str, category: str = "") -> dict:
    """渲染三套模板，返回 {model: {prompt, format_note, label}}。"""
    return {
        "jimeng": {
            "label": MODEL_TEMPLATES["jimeng"]["label"],
            "format_note": MODEL_TEMPLATES["jimeng"]["format_note"],
            "prompt": render_jimeng(storyboard, product_name),
        },
        "kling": {
            "label": MODEL_TEMPLATES["kling"]["label"],
            "format_note": MODEL_TEMPLATES["kling"]["format_note"],
            "prompt": render_kling(storyboard, product_name),
        },
        "seedance": {
            "label": MODEL_TEMPLATES["seedance"]["label"],
            "format_note": MODEL_TEMPLATES["seedance"]["format_note"],
            "prompt": render_seedance(storyboard, product_name, category),
        },
    }


def save_templates(templates: dict, out_path: Path) -> None:
    """把三套 prompt 存成 JSON 文件（素材包的一部分）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(templates, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # 自测：用 D3 生成的 YepCam 分镜跑一遍
    import sqlite3
    conn = sqlite3.connect(Path(__file__).resolve().parent.parent / "data" / "adprompt.db")
    row = conn.execute("SELECT product_name, category, storyboard FROM generations ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    sb = json.loads(row[2])
    out = render_all_templates(sb, row[0], row[1])
    for model, info in out.items():
        print(f"\n===== {info['label']} =====")
        print(info["prompt"][:400], "...")
