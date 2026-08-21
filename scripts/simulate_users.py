# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D7 模拟用户数据脚本
======================================
模拟 200 个用户走完产品使用路径，事件写入 SQLite events 表（is_simulated=1）。

设计（模拟数据的合理性，Owner 审核要点）：
- 用「漏斗衰减」模拟真实行为，不造假完美数据：
  * 访问页面（100%）
  * 点「生成」按钮（约 70%——有人只看不点）
  * 生成成功（点击者中约 85%——有人输入不合规失败）
  * 质检通过（生成成功者中约 75%——低分会被质检拦下）
  * 复制 prompt（质检通过者中约 60%——有人看看就走）
  * 点赞/踩反馈（复制者中约 50%）
  → 最终漏斗：200 访问 → ~140 点击 → ~119 成功 → ~89 通过 → ~53 复制
- 每个用户带唯一 user_id（payload 里），方便留存/漏斗 SQL 按用户去重
- 时间戳：模拟 3 天分布（D1/D3/D7 留存 cohort 需要跨天数据）
- 明确标注 is_simulated=1（诚实红线）

用法：python scripts/simulate_users.py [用户数，默认 200]
"""
import random
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"

# 商品池（模拟用户会"用"的商品，从 EAVSD 风格 case 里取）
PRODUCTS = [
    "YepCam X5 运动相机", "AeroGlide 碳纤维公路自行车", "LumiRose 香氛沐浴露",
    "FitFuel 乳清蛋白粉", "EcoBrew 便携咖啡机", "PetPaw 智能宠物喂食器",
    "无线降噪耳机", "智能手环", "便携蓝牙音箱", "保温杯",
]

# 漏斗概率（真实直觉校准值，可调）
P_CLICK = 0.70     # 访问后点生成
P_SUCCESS = 0.85   # 点击后生成成功
P_QUALITY = 0.75   # 成功后质检通过
P_COPY = 0.60      # 通过后复制
P_FEEDBACK = 0.50  # 复制后反馈
P_RETURN = 0.50    # 首次访问后再次回访（留存 cohort 需要；工具类产品 7 日留存 30-50% 合理）


def simulate(n_users: int, seed: int = 42) -> None:
    random.seed(seed)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, payload TEXT, created_at TEXT DEFAULT (datetime('now')), is_simulated INTEGER DEFAULT 0)")

    # 3 天时间窗（D1/D3/D7 留存 cohort 需要）
    t3 = datetime(2026, 8, 20, 9, 0, 0)   # 模拟窗口结束
    t1 = t3 - timedelta(days=6)            # 窗口开始（7 天前）
    total_events = 0

    for i in range(1, n_users + 1):
        user_id = f"sim_{i:04d}"
        # 用户首次访问时间：均匀分布在 7 天窗口内
        visit_time = t1 + timedelta(seconds=random.randint(0, int((t3 - t1).total_seconds())))
        product = random.choice(PRODUCTS)
        # 时间游标：每个事件在前一事件基础上递增，保证顺序严格合理
        t_cursor = visit_time

        def ev(event: str, step_min: tuple = (1, 5), payload_extra: str = ""):
            """记录事件，时间游标递增 step_min 分钟。"""
            nonlocal total_events, t_cursor
            t_cursor = t_cursor + timedelta(minutes=random.randint(*step_min))
            payload = f"user={user_id};product={product}" + (f";{payload_extra}" if payload_extra else "")
            conn.execute(
                "INSERT INTO events(event, payload, created_at, is_simulated) VALUES(?,?,?,1)",
                (event, payload, t_cursor.strftime("%Y-%m-%d %H:%M:%S")),
            )
            total_events += 1

        # 用户路径（漏斗衰减，时间游标逐步前进）
        ev("page_view", (0, 0))  # 访问 = 游标起点
        if random.random() > P_CLICK:
            continue  # 只看不点

        ev("prompt_generate_click", (1, 5))
        if random.random() > P_SUCCESS:
            ev("prompt_generate_fail", (1, 3))
            continue

        ev("prompt_generate_success", (2, 5))
        if random.random() > P_QUALITY:
            ev("quality_fail", (1, 3))
            continue

        ev("quality_pass", (1, 3))
        if random.random() > P_COPY:
            continue

        ev("prompt_copy", (1, 3))
        if random.random() > P_FEEDBACK:
            pass  # 不反馈也继续（下面加留存行为）

        if random.random() < 0.7:
            ev("feedback_good" if random.random() < 0.7 else "feedback_bad", (1, 3))

        # ===== 留存行为（D9 留存 cohort 需要）=====
        # 约 P_RETURN 的用户会再次访问，记第二次 page_view
        if random.random() < P_RETURN:
            # 回访间隔：优先 1 天（D1 留存），其次 3 天（D3）；
            # 7 天回访大多超出 7 天窗口被丢弃，不浪费生成
            gap_days = random.choice([1, 1, 1, 3])  # 加权：D1 更常见
            return_time = visit_time + timedelta(days=gap_days)
            if return_time < t3:  # 不超出模拟窗口
                t_cursor = return_time  # 直接设游标到回访时间
                ev("page_view", (0, 0))  # 第二次访问，时间=return_time

    conn.commit()

    # 统计漏斗
    print(f"=== D7 模拟完成：{n_users} 个用户，{total_events} 条事件（is_simulated=1）===")
    for event in ["page_view", "prompt_generate_click", "prompt_generate_success",
                  "quality_pass", "prompt_copy", "feedback_good", "feedback_bad"]:
        n = conn.execute("SELECT COUNT(DISTINCT payload) FROM events WHERE event=? AND is_simulated=1", (event,)).fetchone()[0]
        print(f"  {event:<24} {n}")
    conn.close()
    print("\n数据已写入 events 表（is_simulated=1）。下一步：D8 SQL 漏斗分析")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    simulate(n)
