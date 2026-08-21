# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D5 LLM-as-judge 质检器
==========================================
给生成的素材包打分（2 维：风格匹配 + 可用性），低于阈值触发自动重写（最多 2 轮）。

这是本项目"模型评估方法论"王牌的核心实现（面试重点）：
- LLM-as-judge：用另一个 LLM 当裁判给生成结果打分（不是人肉看，不是只看格式）
- 为什么用 LLM 打分而不是规则：质量是主观的（"这个分镜够不够吸引人"），规则判不了
- 2 维评分 + 阈值 3.5 + 重写反馈（修改建议回传给生成器，闭环）

设计：
- judge_storyboard(storyboard, product) → {style_score, usability_score, pass, suggestions}
- 风格匹配：分镜是否符合商品定位/卖点（如防水相机不该拍办公室场景）
- 可用性：分镜是否可拍摄/描述是否具体（如"漂亮的画面"不可用，"镜头缓慢推近展示机身金属质感"可用）

用法：
    from src.quality_judge import judge_storyboard
    result = judge_storyboard(storyboard, product)
"""
import json
import os
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_DIR / ".env"

# 加载 .env
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-chat"

QUALITY_THRESHOLD = 3.5  # 阈值（Owner 已拍板）


def _call_judge_llm(system_prompt: str, user_prompt: str, max_tokens: int = 800) -> str:
    """调用 DeepSeek 当裁判。"""
    from openai import OpenAI

    if not DEEPSEEK_API_KEY:
        print("[错误] 未找到 DEEPSEEK_API_KEY，请先配置 .env")
        sys.exit(1)
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=max_tokens,
        temperature=0.2,  # 裁判要稳定，低温度
    )
    return resp.choices[0].message.content


def _parse_judge_output(raw: str) -> dict:
    """解析裁判输出 JSON（容忍 markdown 包裹）。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def judge_storyboard(storyboard: dict, product: dict, retry: int = 0) -> dict:
    """
    质检单个分镜：返回 {style_score, usability_score, pass, suggestions, overall, retry}
    retry 参数用于区分是第几轮重写后的质检。
    """
    sys_prompt = (
        "你是资深电商广告视频质检专家。评估以下商品的分镜脚本质量，从 2 个维度打分（0-5 分，可给小数）：\n"
        "1. style_score 风格匹配：分镜是否符合商品定位与卖点（防水相机不该拍办公室；卖点要体现在画面里）\n"
        "2. usability_score 可用性：描述是否具体可拍摄（'漂亮的画面'不可用；'镜头缓慢推近展示机身金属质感'可用）\n"
        "输出 JSON："
        '{"style_score": 0-5, "usability_score": 0-5, "suggestions": ["改进建议1", "改进建议2"]}\n'
        "注意：评分要严格，4 分以上才算优秀；建议要具体可执行。"
    )
    user_prompt = (
        f"商品：{product['name']}（{product['category']}）\n"
        f"商品描述：{product['description']}\n"
        f"分镜 JSON：\n{json.dumps(storyboard, ensure_ascii=False, indent=2)}\n\n"
        "请按 2 个维度打分并给出改进建议："
    )
    raw = _call_judge_llm(sys_prompt, user_prompt, max_tokens=800)
    result = _parse_judge_output(raw)

    style = float(result.get("style_score", 0))
    usability = float(result.get("usability_score", 0))
    overall = (style + usability) / 2
    suggestions = result.get("suggestions", [])
    if isinstance(suggestions, str):
        suggestions = [suggestions]

    return {
        "style_score": style,
        "usability_score": usability,
        "overall": overall,
        "pass": overall >= QUALITY_THRESHOLD,
        "suggestions": suggestions,
        "retry": retry,
    }


def rewrite_storyboard(storyboard: dict, product: dict, judge_result: dict) -> dict:
    """
    根据质检建议重写分镜（把建议喂回给生成 LLM，针对性改进）。
    """
    sys_prompt = (
        "你是出海电商广告视频分镜师。根据质检反馈重写分镜，修复指出的问题，"
        "保持原有镜头数量（6-8 个），输出 JSON："
        '{"shots": [{"shot_no": 1, "scene_type": "景别", "camera": "运镜", '
        '"description": "画面描述", "copy": "广告文案", "duration": "时长秒"}]}'
    )
    user_prompt = (
        f"商品：{product['name']}（{product['category']}）\n"
        f"描述：{product['description']}\n"
        f"原分镜：\n{json.dumps(storyboard, ensure_ascii=False, indent=2)}\n\n"
        f"质检反馈（必须修复）：\n" + "\n".join(f"- {s}" for s in judge_result.get("suggestions", []))
        + "\n\n请重写分镜，输出 JSON："
    )
    raw = _call_judge_llm(sys_prompt, user_prompt, max_tokens=2500)
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def judge_with_rewrite(storyboard: dict, product: dict, max_rounds: int = 2) -> dict:
    """
    完整质检闭环：打分 → 不过则重写 → 再打分，最多 max_rounds 轮。
    返回最终结果（含每轮记录，供评估报告用）。
    """
    current = storyboard
    rounds = []
    final = None

    for round_no in range(max_rounds + 1):  # 初始 + 2 轮重写 = 最多 3 次质检
        result = judge_storyboard(current, product, retry=round_no)
        rounds.append({
            "round": round_no,
            "storyboard": current,
            "judge": {k: v for k, v in result.items() if k != "retry"},
        })
        print(f"    质检轮 {round_no}: style={result['style_score']} usability={result['usability_score']} "
              f"overall={result['overall']:.2f} {'PASS' if result['pass'] else 'REWRITE'}")
        if result["pass"]:
            final = result
            break
        if round_no < max_rounds:
            current = rewrite_storyboard(current, product, result)
    if final is None:
        final = rounds[-1]["judge"]
        # 补 retry 字段
        final["retry"] = len(rounds) - 1

    return {
        "passed": final["pass"],
        "final_overall": final["overall"],
        "final_scores": {"style": final["style_score"], "usability": final["usability_score"]},
        "rounds_used": len(rounds) - 1,
        "rounds": rounds,
    }


if __name__ == "__main__":
    # 自测：取库里最新一个分镜质检
    import sqlite3
    conn = sqlite3.connect(REPO_DIR / "data" / "adprompt.db")
    row = conn.execute("SELECT product_name, category, storyboard FROM generations ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    if row:
        product = {"name": row[0], "category": row[1], "description": ""}
        sb = json.loads(row[2])
        print(f"=== 质检：{product['name']} ===")
        res = judge_with_rewrite(sb, product)
        print(f"\n最终: {'PASS' if res['passed'] else 'FAIL'} | overall={res['final_overall']:.2f} | 用了 {res['rounds_used']} 轮重写")
    else:
        print("库中没有分镜数据，先运行 workflow.py")
