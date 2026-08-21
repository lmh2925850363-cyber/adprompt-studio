# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D12 看板生成脚本
====================================
从 SQLite 读取分析数据，生成自包含看板页面 frontend/dashboard.html
（内联 SVG 画图，Linear 暗色风格，不依赖 CDN，离线可打开可截图）

三张图：
1. 激活漏斗（访问→点击→成功→质检通过→复制）
2. 留存 cohort 热力图（首次访问日 × D1/D3/D7）
3. 成本趋势（按天估算成本 + 累计）

用法：python scripts/build_dashboard.py
"""
import json
import sqlite3
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DB_PATH = REPO_DIR / "data" / "adprompt.db"
OUT_PATH = REPO_DIR / "frontend" / "dashboard.html"

# 设计色（深空紫 · 与主页 index.html 统一）
C_BG = "#0a0a12"
C_PANEL = "#10101c"
C_ACCENT = "#8b5cf6"
C_VIOLET = "#7c6ff0"
C_HOVER = "#9d8fff"
C_TEXT = "#f4f4ff"
C_SEC = "#c9c9e0"
C_TER = "#8f8fa8"
C_QUA = "#5d5d75"
C_GREEN = "#10b981"
C_RED = "#f43f5e"
C_BORDER = "rgba(255,255,255,0.08)"


def get_funnel():
    """激活漏斗：按用户去重，返回 (环节名列表, 人数列表, 转化率列表)。"""
    conn = sqlite3.connect(DB_PATH)
    steps = ["page_view", "prompt_generate_click", "prompt_generate_success", "quality_pass", "prompt_copy"]
    labels = ["访问页面", "点击生成", "生成成功", "质检通过", "复制 Prompt"]
    counts = []
    for e in steps:
        n = conn.execute(
            "SELECT COUNT(DISTINCT substr(payload,6,instr(payload,';')-6)) FROM events WHERE event=? AND is_simulated=1", (e,)
        ).fetchone()[0]
        counts.append(n)
    conn.close()
    rates = []
    for i in range(1, len(counts)):
        rates.append(round(100.0 * counts[i] / counts[i - 1], 1) if counts[i - 1] else 0)
    return labels, counts, rates


def get_retention():
    """留存 cohort：首次访问日 × D1/D3 留存率。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """WITH visits AS (
            SELECT substr(payload,6,instr(payload,';')-6) AS uid, substr(created_at,1,10) AS d
            FROM events WHERE event='page_view' AND is_simulated=1
        ), firsts AS (
            SELECT uid, MIN(d) AS fd FROM visits GROUP BY uid
        )
        SELECT f.fd, COUNT(DISTINCT f.uid),
            COUNT(DISTINCT CASE WHEN CAST(julianday(date(v.d))-julianday(date(f.fd)) AS INTEGER)=1 THEN v.uid END),
            COUNT(DISTINCT CASE WHEN CAST(julianday(date(v.d))-julianday(date(f.fd)) AS INTEGER)=3 THEN v.uid END)
        FROM firsts f LEFT JOIN visits v ON v.uid=f.uid AND date(v.d)>date(f.fd)
        GROUP BY f.fd ORDER BY f.fd"""
    ).fetchall()
    conn.close()
    cohorts = []
    for r in rows:
        day, size, d1, d3 = r[0][5:], r[1], r[2], r[3]
        cohorts.append({
            "day": day,
            "size": size,
            "d1": round(100.0 * d1 / size, 1) if size else 0,
            "d3": round(100.0 * d3 / size, 1) if size else 0,
        })
    return cohorts


def get_cost_trend():
    """成本趋势：按天统计生成数，估算成本（沿用 D9 口径）。"""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT substr(created_at,1,10) AS d, COUNT(*) FROM events
           WHERE event='prompt_generate_success' AND is_simulated=1 GROUP BY d ORDER BY d"""
    ).fetchall()
    conn.close()
    # 每生成 5 次调用，每次 ¥0.012
    trend = []
    cum = 0.0
    for r in rows:
        day, cnt = r[0][5:], r[1]
        cost = cnt * 5 * 0.012
        cum += cost
        trend.append({"day": day, "count": cnt, "cost": round(cost, 2), "cum": round(cum, 2)})
    return trend


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_funnel_svg(labels, counts, rates):
    """横向条形漏斗（SVG）。"""
    max_c = max(counts)
    bar_h = 40
    gap = 18
    width = 560
    total_h = len(labels) * (bar_h + gap) + 20
    parts = [f'<svg viewBox="0 0 {width} {total_h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']
    for i, (label, cnt, rate) in enumerate(zip(labels, counts, [None] + rates)):
        y = 10 + i * (bar_h + gap)
        bar_w = int((cnt / max_c) * (width - 260))
        x = (width - 260 - bar_w) / 2
        parts.append(
            f'<rect x="{x}" y="{y}" width="{bar_w}" height="{bar_h}" rx="8" '
            f'fill="rgba(113,112,255,{0.25 + 0.55 * (cnt / max_c):.2f})" stroke="{C_BORDER}"/>'
        )
        parts.append(f'<text x="{x + bar_w + 12}" y="{y + 26}" font-size="14" fill="{C_TEXT}">{cnt}</text>')
        parts.append(f'<text x="{width - 210}" y="{y + 26}" font-size="13" fill="{C_TER}">{esc(label)}</text>')
        if rate is not None:
            parts.append(
                f'<text x="{x - 8}" y="{y + 26}" font-size="12" fill="{C_GREEN if rate >= 50 else C_RED}" '
                f'text-anchor="end">{rate}%</text>'
            )
    parts.append("</svg>")
    return "".join(parts)


def render_retention_svg(cohorts):
    """留存 cohort 热力图（SVG 网格）。"""
    cols = ["cohort", "D1", "D3", "D7"]
    cell_w, cell_h = 110, 44
    header_h = 44
    pad = 16
    width = 120 + len(cohorts) * cell_w + pad
    height = header_h + (len(cols) - 1) * cell_h + pad
    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']

    def heat_color(v):
        if v <= 0:
            return "rgba(255,255,255,0.04)"
        return f"rgba(16,185,129,{0.15 + 0.75 * min(v / 20, 1):.2f})"

    # 表头
    parts.append(f'<text x="{pad}" y="{26}" font-size="12" fill="{C_TER}">首次访问日</text>')
    for ci, col in enumerate(cols[1:]):
        x = pad + 120 + ci * cell_w
        parts.append(f'<text x="{x}" y="{26}" font-size="12" fill="{C_TER}">{col} 留存</text>')
    # 行
    for ri, c in enumerate(cohorts):
        y = header_h + ri * cell_h
        parts.append(f'<text x="{pad}" y="{y + 28}" font-size="13" fill="{C_SEC}">8/{c["day"]}（{c["size"]}人）</text>')
        vals = [c["d1"], c["d3"], 0]
        for ci, v in enumerate(vals):
            x = pad + 120 + ci * cell_w
            parts.append(
                f'<rect x="{x}" y="{y + 8}" width="{cell_w - 12}" height="{cell_h - 16}" rx="6" fill="{heat_color(v)}"/>'
            )
            label = f"{v}%" if v > 0 else "—"
            parts.append(f'<text x="{x + (cell_w - 12) / 2}" y="{y + 32}" font-size="12" fill="{C_TEXT}" text-anchor="middle">{label}</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_cost_svg(trend):
    """成本趋势折线（SVG，含累计）。"""
    if not trend:
        return '<div style="color:#62666d;padding:20px">暂无数据</div>'
    w, h = 560, 240
    pl, pr, pt, pb = 50, 20, 20, 40
    iw, ih = w - pl - pr, h - pt - pb
    max_cum = max(t["cum"] for t in trend) or 1
    max_cnt = max(t["count"] for t in trend) or 1
    n = len(trend)
    parts = [f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto">']
    # 网格线
    for g in range(5):
        gy = pt + ih * g / 4
        parts.append(f'<line x1="{pl}" y1="{gy}" x2="{w - pr}" y2="{gy}" stroke="rgba(255,255,255,0.05)"/>')
    # 折线（每日成本）
    pts = []
    for i, t in enumerate(trend):
        x = pl + iw * i / max(n - 1, 1)
        y = pt + ih - (t["cost"] / max_cnt) * ih * 0.8
        pts.append(f"{x:.1f},{y:.1f}")
    parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{C_ACCENT}" stroke-width="2"/>')
    # 累计面积
    cpts = [f"{pl},{pt + ih}"]
    for i, t in enumerate(trend):
        x = pl + iw * i / max(n - 1, 1)
        y = pt + ih - (t["cum"] / max_cum) * ih
        cpts.append(f"{x:.1f},{y:.1f}")
    cpts.append(f"{w - pr},{pt + ih}")
    parts.append(f'<polygon points="{" ".join(cpts)}" fill="rgba(94,106,210,0.25)"/>')
    # 标签
    for i, t in enumerate(trend):
        x = pl + iw * i / max(n - 1, 1)
        parts.append(f'<text x="{x}" y="{h - 15}" font-size="11" fill="{C_QUA}" text-anchor="middle">{t["day"]}</text>')
    # 图例
    parts.append(f'<rect x="{pl}" y="{pt - 6}" width="12" height="3" rx="1.5" fill="{C_ACCENT}"/>')
    parts.append(f'<text x="{pl + 18}" y="{pt}" font-size="11" fill="{C_TER}">每日成本（元）</text>')
    parts.append(f'<rect x="{pl + 120}" y="{pt - 6}" width="12" height="3" rx="1.5" fill="rgba(94,106,210,0.35)"/>')
    parts.append(f'<text x="{pl + 138}" y="{pt}" font-size="11" fill="{C_TER}">累计成本（元）</text>')
    parts.append("</svg>")
    return "".join(parts)


def build() -> None:
    labels, counts, rates = get_funnel()
    cohorts = get_retention()
    trend = get_cost_trend()
    total_cost = trend[-1]["cum"] if trend else 0
    total_copy = counts[-1]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AdPrompt Studio — 数据看板</title>
<style>
:root {{ --bg:#{C_BG[1:]}; --panel:#{C_PANEL[1:]}; --text:#{C_TEXT[1:]}; --sec:#{C_SEC[1:]}; --ter:#{C_TER[1:]}; --qua:#{C_QUA[1:]}; --accent:#{C_ACCENT[1:]}; --green:#{C_GREEN[1:]}; --red:#{C_RED[1:]}; }}
* {{ margin:0; padding:0; box-sizing:border-box; font-feature-settings:"cv01","ss03"; }}
body {{ background:var(--bg); color:var(--text); font-family:'Inter',system-ui,-apple-system,sans-serif; -webkit-font-smoothing:antialiased; overflow-x:hidden; }}
body::before {{ content:""; position:fixed; inset:0; z-index:-2;
  background:
    radial-gradient(ellipse 60% 40% at 20% -10%, rgba(139,92,246,0.15), transparent 60%),
    radial-gradient(ellipse 50% 35% at 85% 0%, rgba(6,182,212,0.08), transparent 60%),
    radial-gradient(ellipse 55% 40% at 50% 110%, rgba(99,102,241,0.10), transparent 60%),
    var(--bg); }}
.header {{ border-bottom:1px solid {C_BORDER}; padding:20px 0; background:rgba(10,10,18,0.7); backdrop-filter:blur(20px) saturate(140%); }}
.container {{ max-width:1100px; margin:0 auto; padding:0 32px; }}
h1 {{ font-size:28px; font-weight:600; letter-spacing:-0.5px; }}
.sub {{ color:var(--ter); font-size:14px; margin-top:6px; }}
.stats {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; margin:28px 0; }}
.stat {{ background:rgba(255,255,255,0.04); backdrop-filter:blur(12px); border:1px solid {C_BORDER}; border-radius:12px; padding:18px 20px; box-shadow:0 4px 20px rgba(0,0,0,0.25); }}
.stat .v {{ font-size:26px; font-weight:700; font-family:'JetBrains Mono',monospace; }}
.stat .l {{ color:var(--ter); font-size:12px; margin-top:4px; }}
.card {{ background:rgba(255,255,255,0.04); backdrop-filter:blur(12px); border:1px solid {C_BORDER}; border-radius:14px; padding:22px 24px; margin-bottom:24px; box-shadow:0 4px 20px rgba(0,0,0,0.25); }}
.card h2 {{ font-size:16px; font-weight:600; margin-bottom:4px; }}
.card .desc {{ color:var(--ter); font-size:12px; margin-bottom:16px; }}
.note {{ color:var(--qua); font-size:12px; margin-top:14px; line-height:1.7; }}
.tag {{ display:inline-block; font-size:11px; color:var(--ter); border:1px solid {C_BORDER}; border-radius:4px; padding:2px 8px; margin-left:8px; vertical-align:2px; }}
</style>
</head>
<body>
<div class="header"><div class="container"><h1>AdPrompt Studio 数据看板</h1>
<div class="sub">出海电商广告素材生成工作流 · 埋点 → 漏斗 → 留存 → 成本 → A/B · 数据来源：SQLite（模拟数据 is_simulated=1，结论方向性）</div></div></div>
<div class="container">
  <div class="stats">
    <div class="stat"><div class="v">{counts[0]}</div><div class="l">访问用户（模拟）</div></div>
    <div class="stat"><div class="v">{counts[3]}</div><div class="l">质检通过（有效素材）</div></div>
    <div class="stat"><div class="v">{counts[4]}</div><div class="l">复制 Prompt（转化 21.0%）</div></div>
    <div class="stat"><div class="v">¥{total_cost:.1f}</div><div class="l">估算总成本（预算 ¥20）</div></div>
    <div class="stat"><div class="v">¥{total_cost / max(counts[3], 1):.3f}</div><div class="l">单有效素材成本（/质检通过）</div></div>
  </div>

  <div class="card"><h2>① 激活漏斗<span class="tag">转化率=本层/上层</span></h2>
  <div class="desc">访问 → 点击 → 生成成功 → 质检通过 → 复制，按用户去重</div>
  {render_funnel_svg(labels, counts, rates)}
  <div class="note">最大流失：质检通过 → 复制（{rates[3]}% 转化，流失 {100 - rates[3]:.1f}%）——用户拿到合格素材却没复制，已通过 A/B 实验验证优化方向（方向性）。</div>
  </div>

  <div class="card"><h2>② 留存 Cohort 热力图<span class="tag">D1/D3 留存</span></h2>
  <div class="desc">按首次访问日分组，第 1/3 天回访率（模拟窗口 7 天，D7 无法观测）</div>
  {render_retention_svg(cohorts)}
  <div class="note">⚠️ 样本局限：回访样本小（18 人），晚进组观察窗口不足——结论仅方向性，需真实用户验证。</div>
  </div>

  <div class="card"><h2>③ 成本趋势<span class="tag">估算口径</span></h2>
  <div class="desc">按天生成次数估算成本（每次生成 ≈ 5 次 LLM 调用 × ¥0.012），累计曲线</div>
  {render_cost_svg(trend)}
  <div class="note">⚠️ 成本为估算值（事件表未记精确 token 数）；精确计量需 API 层加 usage 记录（迭代项）。</div>
  </div>
</div>
</body>
</html>"""
    OUT_PATH.write_text(html, encoding="utf-8")
    print(f"看板已生成: {OUT_PATH} ({len(html)} bytes)")
    print(f"漏斗: {counts} 转化率: {rates}")
    print(f"cohort: {len(cohorts)} 天, 成本累计: ¥{total_cost:.2f}")


if __name__ == "__main__":
    build()
