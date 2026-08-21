-- ============================================================
-- AdPrompt Studio - SQL ③ 成本分析
-- ============================================================
-- 问题：生成一个「有效 prompt」（质检通过）到底花多少钱？
--
-- 计算逻辑（对应 Crepal 的 50 美元教训——单素材成本意识）：
-- 总成本 = Σ(每次 LLM 调用的输入 tokens × 单价 + 输出 tokens × 单价)
-- 单有效 prompt 成本 = 总成本 / 质检通过次数
--
-- 成本参数（DeepSeek 官方价格，2026-08）：
--   deepseek-chat: 输入 ¥2 / 1M tokens（缓存未命中），输出 ¥8 / 1M tokens
--   按保守估算（含缓存命中折扣，均价）：输入 ¥1.5/M，输出 ¥6/M
--
-- 调用量估算（从事件数据反推）：
--   每次生成 = 4 次 LLM 调用（卖点/文案/视觉/分镜）+ 质检 1 次 + 可能重写
--   平均每次调用约 输入 2k tokens + 输出 1.5k tokens
--
-- ⚠️ 诚实口径：事件表记录的是「生成次数」，不记录每次调用精确 token 数。
--    本分析用「平均 token 量 × 单价」估算，报告里如实标注为「估算值」。
--    精确值需在 API 层加 usage 记录（迭代项）。
--
-- 用法：sqlite3 data/adprompt.db < sql/03_cost_analysis.sql
-- ============================================================

-- 成本参数（可调）
WITH params AS (
  SELECT
    1.5 AS price_in_per_m,     -- 输入 ¥/1M tokens
    6.0 AS price_out_per_m,    -- 输出 ¥/1M tokens
    2000.0 AS avg_in_tokens,   -- 平均单次调用输入 tokens
    1500.0 AS avg_out_tokens,  -- 平均单次调用输出 tokens
    5.0 AS calls_per_generation  -- 每次生成约 5 次调用（4 步工作流 + 1 质检）
),
stats AS (
  SELECT
    (SELECT COUNT(*) FROM events WHERE event='prompt_generate_success' AND is_simulated=0) AS real_generations,
    (SELECT COUNT(*) FROM events WHERE event='prompt_generate_success' AND is_simulated=1) AS sim_generations,
    (SELECT COUNT(*) FROM events WHERE event='quality_pass' AND is_simulated=0)          AS real_quality_pass,
    (SELECT COUNT(*) FROM events WHERE event='quality_pass' AND is_simulated=1)          AS sim_quality_pass
)
SELECT
  stats.sim_generations AS 模拟生成次数,
  stats.sim_quality_pass AS 模拟质检通过数,
  ROUND(
    stats.sim_generations * params.calls_per_generation
    * (params.avg_in_tokens * params.price_in_per_m + params.avg_out_tokens * params.price_out_per_m) / 1e6,
    3
  ) AS 模拟数据估算成本_元,
  ROUND(
    stats.sim_generations * params.calls_per_generation
    * (params.avg_in_tokens * params.price_in_per_m + params.avg_out_tokens * params.price_out_per_m) / 1e6
    / NULLIF(stats.sim_quality_pass, 0),
    4
  ) AS 单有效prompt成本_元
FROM stats, params;

-- 解释（面试讲法）：
-- 模拟 113 次生成 × 5 次调用 × (2k×1.5 + 1.5k×6)/1M
--   = 113 × 5 × (0.003 + 0.009) = 113 × 5 × 0.012 = ¥6.78
-- 单有效 prompt = 6.78 / 73 质检通过 ≈ ¥0.093
-- → 全项目模拟 + 真实开发成本总计 < ¥10（预算 ¥20 的一半）
-- ============================================================
