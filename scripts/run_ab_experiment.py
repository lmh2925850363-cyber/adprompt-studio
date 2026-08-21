# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D10/D11 A/B 实验（模拟）
===========================================
实验：「质检后自动重写」vs「仅手动提示」
- A 组（对照）：质检打分 → 直接展示（通过率 P_QUALITY_A）
- B 组（实验）：质检打分 → 低分自动重写 ≤2 轮 → 展示（通过率更高）

模拟设计（模拟数据的合理性，Owner 审核要点）：
- 800 用户，50/50 随机分 A/B（每组 400，满足 MDE 样本量 ~373/组）
- B 组的质检通过率高于 A 组（自动重写提升了质量——这正是假设的机制）
- 复制率：B 组略高于 A 组（结果更可用 → 更多人复制，但效应温和）
- 成本护栏：B 组因重写多调 LLM，单 case 成本更高，但需 ≤ ¥0.1/次
- 所有事件带 ab_group 标记 + is_simulated=1（诚实红线）

⚠️ 诚实声明：数据为模拟，结论只标「方向性」；样本量达到 MDE 公式要求
   但无法替代真实用户验证。

用法：python scripts/run_ab_experiment.py
"""
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"

PRODUCTS = [
    "YepCam X5 运动相机", "AeroGlide 碳纤维公路自行车", "LumiRose 香氛沐浴露",
    "FitFuel 乳清蛋白粉", "EcoBrew 便携咖啡机", "PetPaw 智能宠物喂食器",
    "无线降噪耳机", "智能手环", "便携蓝牙音箱", "保温杯",
]

# 实验参数（A/B 差异的核心：质检通过率）
P_CLICK = 0.70
P_SUCCESS = 0.85
P_QUALITY_A = 0.55   # A 组（手动提示）：55% 通过
P_QUALITY_B = 0.75   # B 组（自动重写）：75% 通过（假设机制）
P_COPY_A = 0.50      # A 组复制率（较低，用户对结果信心不足）
P_COPY_B = 0.62      # B 组复制率（结果更可用 → 复制意愿更高）
P_FEEDBACK = 0.50
COST_PER_CALL = 0.012  # 每次 LLM 调用估算 ¥0.012（2k in × 1.5/M + 1.5k out × 6/M）
CALLS_A = 5          # A 组：4 步生成 + 1 质检
CALLS_B = 7          # B 组：4 步生成 + 1 质检 + 平均 2 次重写调用
COST_GUARDRAIL = 0.1  # 单 case 成本护栏（Owner 拍板）


def simulate_ab(n_users: int = 800, seed: int = 7) -> None:
    random.seed(seed)
    conn = sqlite3.connect(DB_PATH)
    # A/B 实验数据写独立表，不污染 events（events 是产品漏斗/留存的干净数据源）
    conn.execute("""CREATE TABLE IF NOT EXISTS ab_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT, ab_group TEXT, event TEXT, product TEXT,
        created_at TEXT, is_simulated INTEGER DEFAULT 1
    )""")
    conn.execute("DELETE FROM ab_events")

    results = {"A": {"pass": 0, "copy": 0, "calls": 0, "cost": 0.0, "users": 0},
               "B": {"pass": 0, "copy": 0, "calls": 0, "cost": 0.0, "users": 0}}

    def ev(user_id, group, event, product, ts):
        conn.execute(
            "INSERT INTO ab_events(user_id, ab_group, event, product, created_at, is_simulated) VALUES(?,?,?,?,?,1)",
            (user_id, group, event, product, ts),
        )

    for i in range(1, n_users + 1):
        user_id = f"ab_{i:04d}"
        group = "A" if random.random() < 0.5 else "B"
        results[group]["users"] += 1
        product = random.choice(PRODUCTS)

        t = datetime(2026, 8, 14) + timedelta(seconds=random.randint(0, 6 * 86400))
        ts = lambda m: (t + timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731

        ev(user_id, group, "page_view", product, ts(0))
        if random.random() > P_CLICK:
            continue
        if random.random() > P_SUCCESS:
            ev(user_id, group, "prompt_generate_fail", product, ts(3))
            continue

        ev(user_id, group, "prompt_generate_success", product, ts(5))

        # 质检：A/B 组通过率不同（实验机制）
        p_quality = P_QUALITY_A if group == "A" else P_QUALITY_B
        calls = CALLS_A if group == "A" else CALLS_B
        if random.random() > p_quality:
            ev(user_id, group, "quality_fail", product, ts(7))
            results[group]["calls"] += calls
            results[group]["cost"] += calls * COST_PER_CALL
            continue

        results[group]["pass"] += 1
        ev(user_id, group, "quality_pass", product, ts(7))

        # 复制：B 组复制率更高（结果更可用）
        p_copy = P_COPY_A if group == "A" else P_COPY_B
        if random.random() <= p_copy:
            results[group]["copy"] += 1
            ev(user_id, group, "prompt_copy", product, ts(9))

        results[group]["calls"] += calls
        results[group]["cost"] += calls * COST_PER_CALL

    conn.commit()
    conn.close()

    # ===== 统计与显著性检验 =====
    print("=== A/B 实验结果（模拟 800 用户，50/50 分组）===\n")
    print(f"{'指标':<22}{'A 对照组':>12}{'B 实验组':>12}")
    for label, key in [("用户数", "users"), ("质检通过", "pass"), ("复制", "copy")]:
        a = results["A"][key]
        b = results["B"][key]
        print(f"{label:<22}{a:>12}{b:>12}")

    # 主指标：质检通过后复制率
    a_pass, b_pass = results["A"]["pass"], results["B"]["pass"]
    a_copy, b_copy = results["A"]["copy"], results["B"]["copy"]
    a_rate = a_copy / a_pass if a_pass else 0
    b_rate = b_copy / b_pass if b_pass else 0
    print(f"\n主指标 - 质检通过后复制率:")
    print(f"  A 组: {a_rate:.1%} ({a_copy}/{a_pass})")
    print(f"  B 组: {b_rate:.1%} ({b_copy}/{b_pass})")
    print(f"  提升: {(b_rate - a_rate):.1%}")

    # 两比例 z 检验
    import math
    p_pool = (a_copy + b_copy) / (a_pass + b_pass) if (a_pass + b_pass) else 0
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / a_pass + 1 / b_pass)) if (a_pass and b_pass) else 0
    z = (b_rate - a_rate) / se if se else 0
    # 双侧 p 值近似（正态）
    p_value = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    print(f"  z = {z:.2f}, p = {p_value:.4f} {'(p<0.05 显著)' if p_value < 0.05 else '(p≥0.05 不显著)'}")

    # 护栏检查
    print(f"\n护栏 - 单 case 成本（阈值 ¥{COST_GUARDRAIL}）:")
    a_cost = results["A"]["cost"] / results["A"]["users"]
    b_cost = results["B"]["cost"] / results["B"]["users"]
    print(f"  A 组: ¥{a_cost:.4f}/次")
    print(f"  B 组: ¥{b_cost:.4f}/次")
    guard_ok = b_cost <= COST_GUARDRAIL
    print(f"  B 组护栏: {'PASS' if guard_ok else 'FAIL（超限）'}")

    # 决策
    sig = p_value < 0.05
    print("\n=== 决策 ===")
    if sig and guard_ok:
        print("主指标显著提升 + 护栏通过 → 支持假设（方向性）")
    elif sig and not guard_ok:
        print("主指标显著但护栏恶化 → 否决（护栏优先，方法论铁律）")
    else:
        print("主指标无显著差异 → 证据不足，维持现状（方向性）")
    print("\n⚠️ 诚实声明：数据为模拟（is_simulated=1），结论仅「方向性」，需真实用户验证。")


if __name__ == "__main__":
    sys.exit(simulate_ab())
