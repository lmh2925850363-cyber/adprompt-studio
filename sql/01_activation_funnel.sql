-- ============================================================
-- AdPrompt Studio - SQL ① 激活漏斗分析
-- ============================================================
-- 问题：从「访问页面」到「复制 prompt」，每一层流失多少用户？
-- 漏斗：page_view → prompt_generate_click → prompt_generate_success
--        → quality_pass → prompt_copy
--
-- 口径说明（面试重点）：
-- 1. 按用户去重：同一用户多次点击/复制只算 1 人
--    （payload 格式统一为 "user=xxx;product=yyy"，user_id 从 payload 提取）
-- 2. 真实/模拟分离：is_simulated=0 是真实数据（前端埋点，带匿名 user_id），
--    is_simulated=1 是模拟数据（D7 脚本 200 用户）
-- 3. 转化率 = 本层人数 / 上一层人数（不是 / 总访问，那样看不出流失点）
--
-- 注意：D6 前端上线前的开发期数据（2026-08-20 之前）payload 无 user 标识，
--       无法按用户去重，只统计事件数——复盘报告如实标注该局限。
--
-- 用法：sqlite3 data/adprompt.db < sql/01_activation_funnel.sql
-- ============================================================

-- 用户 ID 提取：payload 格式 "user=xxx;product=yyy"
-- 兼容两种：模拟数据 user=sim_xxxx；前端真实数据 user=real_xxxx

-- ① 模拟数据漏斗（is_simulated = 1，D7 脚本 200 用户，主力分析）
WITH users AS (
  SELECT
    event,
    CASE WHEN payload LIKE 'user=%'
         THEN substr(payload, 6, instr(payload, ';') - 6)
         ELSE 'unknown' END AS user_id
  FROM events
  WHERE is_simulated = 1
),
funnel AS (
  SELECT
    COUNT(DISTINCT CASE WHEN event='page_view' THEN user_id END)              AS step1_访问,
    COUNT(DISTINCT CASE WHEN event='prompt_generate_click' THEN user_id END)  AS step2_点击生成,
    COUNT(DISTINCT CASE WHEN event='prompt_generate_success' THEN user_id END) AS step3_生成成功,
    COUNT(DISTINCT CASE WHEN event='quality_pass' THEN user_id END)           AS step4_质检通过,
    COUNT(DISTINCT CASE WHEN event='prompt_copy' THEN user_id END)            AS step5_复制
  FROM users
)
SELECT
  step1_访问,
  step2_点击生成,
  step3_生成成功,
  step4_质检通过,
  step5_复制,
  ROUND(100.0 * step2_点击生成 / step1_访问, 1)      AS 访问到点击_pct,
  ROUND(100.0 * step3_生成成功 / step2_点击生成, 1)  AS 点击到成功_pct,
  ROUND(100.0 * step4_质检通过 / step3_生成成功, 1)  AS 成功到质检通过_pct,
  ROUND(100.0 * step5_复制 / step4_质检通过, 1)      AS 质检通过到复制_pct,
  ROUND(100.0 * step5_复制 / step1_访问, 1)          AS 总转化率_pct
FROM funnel;

-- ② 真实数据漏斗（is_simulated = 0，前端埋点，带匿名 user_id）
WITH users AS (
  SELECT
    event,
    CASE WHEN payload LIKE 'user=%'
         THEN substr(payload, 6, instr(payload, ';') - 6)
         ELSE 'unknown' END AS user_id
  FROM events
  WHERE is_simulated = 0
),
funnel AS (
  SELECT
    COUNT(DISTINCT CASE WHEN event='page_view' THEN user_id END)              AS step1_访问,
    COUNT(DISTINCT CASE WHEN event='prompt_generate_click' THEN user_id END)  AS step2_点击生成,
    COUNT(DISTINCT CASE WHEN event='prompt_generate_success' THEN user_id END) AS step3_生成成功,
    COUNT(DISTINCT CASE WHEN event='quality_pass' THEN user_id END)           AS step4_质检通过,
    COUNT(DISTINCT CASE WHEN event='prompt_copy' THEN user_id END)            AS step5_复制
  FROM users
)
SELECT
  step1_访问, step2_点击生成, step3_生成成功, step4_质检通过, step5_复制,
  ROUND(100.0 * step2_点击生成 / NULLIF(step1_访问, 0), 1)  AS 访问到点击_pct,
  ROUND(100.0 * step3_生成成功 / NULLIF(step2_点击生成, 0), 1) AS 点击到成功_pct,
  ROUND(100.0 * step4_质检通过 / NULLIF(step3_生成成功, 0), 1) AS 成功到质检通过_pct,
  ROUND(100.0 * step5_复制 / NULLIF(step4_质检通过, 0), 1)  AS 质检通过到复制_pct
FROM funnel;

-- ③ 流失点定位（面试最爱的追问）
-- 模拟数据结论：
--   访问→点击    流失 24.5%（151/200，75.5% 转化）
--   点击→成功    流失 21.2%（119/151，78.8% 转化）
--   成功→质检通过 流失 36.1%（76/119，63.9% 转化）← 第二重流失
--   质检通过→复制 流失 40.8%（45/76，59.2% 转化）← 最重流失
--   → 最大流失在「质检通过→复制」：用户看到结果但没复制，
--     假设：① 质检报告里改进了但用户没感知到价值 ② 复制按钮不够显眼
--     （这正好是 A/B 实验可验证的方向，D10-D11）
-- ============================================================
