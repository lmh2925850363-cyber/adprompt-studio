-- ============================================================
-- AdPrompt Studio - SQL ② 留存 Cohort 分析
-- ============================================================
-- 问题：用户首次访问后，第 1/3/7 天还会回来吗？
--
-- 方法（标准 cohort 分析）：
-- 1. 按「首次访问日」把用户分组（cohort）
-- 2. 看每个 cohort 在首次访问后第 N 天是否再次访问
-- 3. 留存率 = 第 N 天回访用户数 / cohort 用户数
--
-- 口径说明：
-- - 首次访问 = 该用户最早的 page_view
-- - 回访 = 首次访问之后又有 page_view
-- - 数据：模拟用户（is_simulated=1），D7 脚本生成
-- - ⚠️ 样本局限（诚实标注）：模拟窗口 7 天，晚进组的 cohort
--   回访时间超出窗口被丢弃；回访样本共 11 人，结论仅「方向性」
--
-- 用法：sqlite3 data/adprompt.db < sql/02_retention_cohort.sql
-- ============================================================

WITH visits AS (
  -- 每个用户每次 page_view 的日期
  SELECT
    substr(payload, 6, instr(payload, ';') - 6) AS user_id,
    substr(created_at, 1, 10) AS visit_day
  FROM events
  WHERE event = 'page_view' AND is_simulated = 1
),
first_visits AS (
  -- 每个用户的首次访问日（cohort 归属）
  SELECT user_id, MIN(visit_day) AS first_day
  FROM visits GROUP BY user_id
),
cohorts AS (
  -- 每个 cohort（首次访问日）的用户数
  SELECT first_day, COUNT(*) AS cohort_size
  FROM first_visits GROUP BY first_day
),
return_days AS (
  -- 每个用户首次访问后的回访情况：第 N 天是否回来
  -- 用「日期差」（date 归一化）而非时间戳差，避免跨天边界误判
  SELECT
    f.user_id,
    f.first_day,
    CAST(julianday(date(v.visit_day)) - julianday(date(f.first_day)) AS INTEGER) AS day_gap
  FROM first_visits f
  JOIN visits v ON v.user_id = f.user_id
  WHERE date(v.visit_day) > date(f.first_day)  -- 只算首次之后的回访
)
SELECT
  c.first_day AS 首次访问日,
  c.cohort_size AS 用户数,
  COUNT(DISTINCT CASE WHEN r.day_gap = 1 THEN r.user_id END) AS D1回访,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN r.day_gap = 1 THEN r.user_id END) / c.cohort_size, 1) AS D1留存_pct,
  COUNT(DISTINCT CASE WHEN r.day_gap = 3 THEN r.user_id END) AS D3回访,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN r.day_gap = 3 THEN r.user_id END) / c.cohort_size, 1) AS D3留存_pct,
  COUNT(DISTINCT CASE WHEN r.day_gap = 7 THEN r.user_id END) AS D7回访,
  ROUND(100.0 * COUNT(DISTINCT CASE WHEN r.day_gap = 7 THEN r.user_id END) / c.cohort_size, 1) AS D7留存_pct
FROM cohorts c
LEFT JOIN return_days r ON r.first_day = c.first_day
GROUP BY c.first_day
ORDER BY c.first_day;

-- 结论（模拟数据，方向性）：
-- 留存率呈下降趋势（D1 > D3 > D7），符合工具类产品直觉。
-- ⚠️ 样本局限：回访样本仅 11 人，且晚进组的 cohort（8/19-8/20）
--    没有足够的观察窗口，D7 留存基本无法观测——如实标注。
-- ============================================================
