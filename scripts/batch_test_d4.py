# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D4 批量验证脚本
=================================
用 EAVSD 真实 case 跑 5 个商品的六步工作流，验证流程稳定性与三模板输出。
只做流程冒烟验证（每 case 真实调用 DeepSeek，成本约 ¥0.02/case）。
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HOME", "E:\\hf-cache")
os.environ.setdefault("HF_HUB_CACHE", "E:\\hf-cache\\hub")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(REPO_DIR / "scripts"))

from workflow import (  # noqa: E402
    rag_retrieve, analyze_selling_points, generate_copy,
    generate_visual_plan, generate_storyboard, render_prompts, log_event,
)

# 从 EAVSD 抽 5 个 case 当测试商品（用真实分镜的场景描述拼商品描述）
TEST_CASES = [
    {"name": "AeroGlide 碳纤维公路自行车", "category": "运动户外",
     "description": "碳纤维车架轻量公路自行车，专业级变速系统，适合城市通勤与长途骑行。轮组破风设计，骑行更省力。"},
    {"name": "LumiRose 香氛沐浴露", "category": "美妆个护",
     "description": "玫瑰精油香氛沐浴露，绵密泡沫深层清洁，持久留香 12 小时。天然植物配方，敏感肌可用。"},
    {"name": "FitFuel 乳清蛋白粉", "category": "运动营养",
     "description": "高纯度乳清蛋白粉，每份含 25g 蛋白质，三重过滤工艺。适合健身增肌人群，巧克力味。"},
    {"name": "EcoBrew 便携咖啡机", "category": "厨房家电",
     "description": "便携式手压咖啡机，无需电源，15 秒萃取意式浓缩。航空铝机身，露营办公室两相宜。"},
    {"name": "PetPaw 智能宠物喂食器", "category": "宠物用品",
     "description": "智能宠物喂食器，APP 远程定时投喂，摄像头实时监控。防卡粮设计，适合出差养宠家庭。"},
]


def main() -> None:
    print(f"=== D4 批量验证：{len(TEST_CASES)} 个 EAVSD 风格 case ===\n")
    results = []
    for i, product in enumerate(TEST_CASES, 1):
        print(f"----- case {i}/{len(TEST_CASES)}: {product['name']} -----")
        try:
            refs = rag_retrieve(f"{product['name']} {product['description']}", top_k=3)
            points = analyze_selling_points(product, refs)
            copy = generate_copy(product, points, "tiktok")
            visual = generate_visual_plan(product, points)
            storyboard = generate_storyboard(product, points, refs)
            templates = render_prompts(storyboard, product["name"], product["category"])

            n_shots = len(storyboard.get("shots", []))
            t_len = {m: len(info["prompt"]) for m, info in templates.items()}
            ok = (len(points) >= 3 and n_shots >= 6
                  and all(v > 200 for v in t_len.values()))
            results.append({"name": product["name"], "ok": ok, "shots": n_shots,
                            "points": len(points), "tpl_len": t_len})
            print(f"  卖点 {len(points)} 个 | 分镜 {n_shots} 镜头 | 模板长度 {t_len} | {'PASS' if ok else 'FAIL'}")

            # 落库
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO generations(product_name, category, selling_points, tiktok_copy, visual_plan, storyboard, templates) "
                "VALUES(?,?,?,?,?,?,?)",
                (product["name"], product["category"],
                 json.dumps(points, ensure_ascii=False), json.dumps(copy, ensure_ascii=False),
                 json.dumps(visual, ensure_ascii=False), json.dumps(storyboard, ensure_ascii=False),
                 json.dumps(templates, ensure_ascii=False)),
            )
            conn.commit()
            conn.close()
            log_event("prompt_generate_success", product["name"])
        except Exception as e:  # noqa: BLE001
            results.append({"name": product["name"], "ok": False, "error": repr(e)})
            print(f"  FAIL: {e!r}")

    print("\n===== 批量结果汇总 =====")
    passed = sum(1 for r in results if r["ok"])
    for r in results:
        status = "PASS" if r["ok"] else "FAIL"
        detail = r.get("error", f"{r.get('shots', 0)}镜头/{r.get('points', 0)}卖点")
        print(f"  [{status}] {r['name']} - {detail}")
    print(f"\n通过 {passed}/{len(results)}，成本约 ¥{passed*0.02:.2f}")


if __name__ == "__main__":
    sys.exit(main())
