# AdPrompt Studio

**出海电商广告素材生成智能体 + 数据验证闭环**

输入商品信息，一键生成完整的广告素材包：卖点分析、广告文案、视觉方案、分镜脚本、三模型视频 prompt（即梦 / 可灵 / Seedance 2），内置 LLM-as-judge 质检与低分自动重写。

> 这不是一个"AI demo"，而是一个按真实产品标准做完闭环的作品集项目：埋点 → SQL 漏斗/留存 → 成本核算 → A/B 实验 → 数据看板。

---

## 为什么做这个项目（背景）

- **目标用户**：出海电商中小卖家（TikTok Shop / Amazon），无专业视频与设计团队
- **核心痛点**：会卖货但不会写视频生成 prompt → 生成不可控 → 反复抽卡浪费钱
- **价值主张**：把创作从「从 0 到 1」降维成「从 1 到 1.1」——商品信息 → 结构化、质检过的 prompt → 直接粘贴生成
- **差异化**：面向卖家的结构化工作流 + 质检闭环 + 多模型模板适配（区别于即梦的通用对话式提示词优化）

## 核心能力（六步工作流）

```
商品信息输入
  → ① 卖点分析（AI 自动从描述提取）
  → ② 广告文案（TikTok 短文案 / Amazon 五点描述）
  → ③ 视觉方案（风格 / 色板 / 构图）
  → ④ 分镜生成（6-8 镜头结构化 JSON）
  → ⑤ 多模型模板适配（即梦 / 可灵 / Seedance 2）
  → ⑥ LLM-as-judge 质检（风格匹配 + 可用性，<3.5 自动重写 ≤2 轮）
```

## 数据闭环（作品集核心价值）

| 环节 | 做了什么 | 产出 |
|---|---|---|
| 埋点 | 8 类事件（page_view / generate / quality / copy / feedback）落 SQLite | events 表 |
| 漏斗 | 激活漏斗：访问→点击→成功→质检通过→复制，按用户去重 | [sql/01](sql/01_activation_funnel.sql) |
| 留存 | D1/D3/D7 cohort 分析 | [sql/02](sql/02_retention_cohort.sql) |
| 成本 | 总调用 × 单价 / 有效产出 | [sql/03](sql/03_cost_analysis.sql) |
| A/B | 自动重写 vs 手动提示（800 用户 50/50 分组，主指标=复制率） | [docs/AB实验设计.md](docs/AB实验设计.md) |
| 看板 | 漏斗 / cohort 热力图 / 成本趋势 | [build_dashboard.py](scripts/build_dashboard.py) |

## 技术栈

| 用途 | 方案 | 说明 |
|---|---|---|
| 工作流 | Python 手写（六步管线） | 代码进 repo，面试讲架构 |
| LLM | DeepSeek API（OpenAI 兼容） | 成本极低 |
| RAG 知识库 | bge-small-zh 本地 embedding + numpy 向量检索 | 400 条 Seedance 范文 |
| 前端 | FastAPI + 自研单页（深空紫视觉） | 弃 Streamlit，样式可控 + 埋点自然 |
| 数据库 | SQLite | 零部署 |
| 埋点 | 自建 events 表 | 不引 PostHog |

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置 DeepSeek API key（.env，已被 .gitignore 排除）
# DEEPSEEK_API_KEY=sk-xxx

# 3. 数据管线（幂等）
python scripts/download_data.py        # 下载公开数据集（走 hf-mirror 镜像）
python scripts/clean_data.py           # 清洗 → 分层抽样 → 落库 SQLite
python scripts/build_knowledge_base.py # 构建 RAG 知识库

# 4. 模拟用户数据（A/B 实验数据）
python scripts/simulate_users.py       # 200 模拟用户（is_simulated=1）
python scripts/run_ab_experiment.py    # A/B 实验（800 用户）

# 5. 启动产品 + 看板
python -m uvicorn src.api:app --port 8000
# 产品: http://127.0.0.1:8000
# 看板: http://127.0.0.1:8000/dashboard

# 6. 分析
sqlite3 data/adprompt.db < sql/01_activation_funnel.sql
```

## 演示截图

| 产品主页 | 数据看板 |
|---|---|
| ![主页](demo/homepage_v2.png) | ![看板](demo/dashboard_v2.png) |

## 数据结论（模拟数据，结论方向性）

- **激活漏斗**：200 访问 → 142 点击（71%）→ 113 成功（79.6%）→ 73 质检通过（64.6%）→ 42 复制（57.5%），总转化 21%
- **最大流失点**：「质检通过 → 复制」流失 42.5%——用户拿到合格素材却没复制
- **A/B 实验**：自动重写使质检通过率 +48%（116→172），但主指标复制率仅 +2.4%（p=0.68 不显著）→ 证据不足维持现状；反直觉洞察：通过人数变多稀释复制率，「提升质量」≠「提升转化」
- **成本**：单有效素材成本约 **¥0.093**（估算口径），全项目预算 ¥20 的 35%

## 诚实声明（数据口径）

- 本项目的分析数据以**模拟数据为主**（`is_simulated=1` 标注，D7 脚本生成 200 用户），结论仅标「方向性」
- 真实数据（开发期 + 朋友试用）通过 `is_simulated=0` 区分，复盘报告写明构成
- 成本为估算值（事件表未记精确 token 数），精确计量为迭代项
- 不涉及任何公司数据，全部公开数据集 + 自产数据

## 文档

- [PRD（需求统一）](docs/PRD.md)
- [A/B 实验设计](docs/AB实验设计.md)
- [数据分析报告](https://github.com/yourname/adprompt-studio/blob/main/数据分析报告.md)（作品集文件夹）
- [面试拷打问题应对](https://github.com/yourname/adprompt-studio/blob/main/拷打问题.md)（作品集文件夹）

## License

MIT
