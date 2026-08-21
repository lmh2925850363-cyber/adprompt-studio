# -*- coding: utf-8 -*-
"""
AdPrompt Studio - D1 数据清洗与落库脚本
======================================
输入：data/raw/ 下已下载的原始文件（由 download_data.py 产出）
输出：data/adprompt.db（SQLite，真实数据 is_simulated=0）

清洗内容：
1. EAVSD：读取 8 个 category parquet 合并 → 过滤无场景/无 prompt 的行 →
   按 category 分层抽样 50 个 case → 每个 case 的 scene_prompts 展开为场景级行
   （1 case → N scene，为 D2 知识库按场景分块做准备）
2. Seedance-2：读取 metadata.jsonl → 提取 prompt 文本（i18n.en.p 为主，raw_p 兜底）→
   过滤空值/过短 → 按内容 hash 去重 → 按 category 分层抽样 400 条

数据表：
- eavsd_cases      case 级（id/category/场景数/prompt 列表）
- eavsd_scenes     场景级（case_id/scene_index/prompt）
- seedance_prompts prompt 级（id/category/prompt/来源字段）
- data_sources     数据来源与抽样记录（诚实可溯源）

用法：python scripts/clean_data.py
"""
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

REPO_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_DIR / "data" / "raw"
DB_PATH = REPO_DIR / "data" / "adprompt.db"

EAVSD_CASE_TARGET = 50    # 目标 case 数（分层抽样，8 类）
SEEDANCE_TARGET = 400     # 目标 prompt 条数（分层抽样）
MIN_PROMPT_LEN = 50       # Seedance prompt 最短长度（过短无知识价值）


def load_eavsd() -> pd.DataFrame:
    """合并 8 个 category parquet 为一个 DataFrame。"""
    frames = []
    for c in "abcdefgh":
        p = RAW_DIR / f"category_{c}.parquet"
        if not p.exists():
            print(f"  [warn] 缺少 {p.name}，跳过该类")
            continue
        df = pd.read_parquet(p)
        frames.append(df)
    if not frames:
        raise FileNotFoundError(f"{RAW_DIR} 下没有 EAVSD parquet，先运行 download_data.py")
    return pd.concat(frames, ignore_index=True)


def stratified_sample(df: pd.DataFrame, col: str, n_total: int, seed: int = 42) -> pd.DataFrame:
    """按某列分层抽样，共 n_total 行，尽量均匀分布到各层。"""
    if len(df) <= n_total:
        return df
    groups = df.groupby(col, group_keys=False)
    per = n_total // groups.ngroups
    rest = n_total % groups.ngroups
    parts = []
    for i, (_, g) in enumerate(groups):
        k = per + (1 if i < rest else 0)
        parts.append(g.sample(min(k, len(g)), random_state=seed))
    return pd.concat(parts, ignore_index=True)


def clean_eavsd() -> tuple[pd.DataFrame, pd.DataFrame]:
    """EAVSD 清洗：过滤 → 分层抽样 case → 展开场景行。返回 (cases, scenes)。"""
    print("\n[EAVSD] 读取 parquet...")
    df = load_eavsd()
    print(f"  raw 行数: {len(df)}")

    # 过滤：有生成场景且场景数 >=1
    df = df[df["n_scenes_generated"].fillna(0) >= 1].copy()
    # 过滤：scene_prompts 非空（展开后至少一条有效 prompt）
    # 注意：parquet 数组列读出是 numpy.ndarray，不是 list/tuple，需统一转 list
    def _to_list(x):
        try:
            return list(x) if x is not None else []
        except TypeError:
            return []

    df["scene_prompts"] = df["scene_prompts"].apply(_to_list)
    df["n_valid_prompts"] = df["scene_prompts"].apply(
        lambda ps: sum(1 for p in ps if isinstance(p, str) and p.strip())
    )
    df = df[df["n_valid_prompts"] >= 1].copy()
    print(f"  过滤后: {len(df)} 行（去掉无场景/无 prompt）")

    # 分层抽样 50 case（8 类均匀）
    cases = stratified_sample(df, "category", EAVSD_CASE_TARGET)
    print(f"  抽样 case: {len(cases)}，分布: {dict(Counter(cases['category']))}")

    # 展开场景行
    scene_rows = []
    for _, row in cases.iterrows():
        for idx, prompt in enumerate(row["scene_prompts"], start=1):
            if isinstance(prompt, str) and prompt.strip():
                scene_rows.append({
                    "case_id": row["id"],
                    "category": row["category"],
                    "scene_index": idx,
                    "scene_prompt": prompt.strip(),
                })
    scenes = pd.DataFrame(scene_rows)
    print(f"  场景展开: {len(scenes)} 行（平均每 case {len(scenes)/len(cases):.1f} 个场景）")

    case_cols = ["id", "category", "source", "n_scenes_generated", "n_scenes_total"]
    cases = cases[case_cols].copy().rename(columns={"id": "case_id"})
    return cases, scenes


def load_seedance() -> pd.DataFrame:
    """读取 metadata.jsonl，提取 prompt 文本。"""
    p = RAW_DIR / "metadata.jsonl"
    if not p.exists():
        raise FileNotFoundError(f"{p} 不存在，先运行 download_data.py")
    records = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # prompt 提取：i18n.en.p 为主，raw_p 兜底
            prompt = None
            i18n = obj.get("i18n") or {}
            en = i18n.get("en") or {}
            if isinstance(en.get("p"), str) and en["p"].strip():
                prompt = en["p"].strip()
            elif isinstance(obj.get("raw_p"), str) and obj["raw_p"].strip():
                prompt = obj["raw_p"].strip()
            if not prompt:
                continue
            records.append({
                "id": str(obj.get("id") or obj.get("slug") or obj.get("file_name") or ""),
                "category": str(obj.get("category") or "Unknown"),
                "platform": str(obj.get("platform") or ""),
                "date": str(obj.get("date") or ""),
                "prompt": prompt,
            })
    return pd.DataFrame(records)


def clean_seedance() -> pd.DataFrame:
    """Seedance 清洗：过滤 → 去重 → 分层抽样 400。"""
    print("\n[Seedance-2] 解析 metadata.jsonl...")
    df = load_seedance()
    print(f"  raw 记录: {len(df)}")

    # 过滤过短 prompt
    before = len(df)
    df = df[df["prompt"].str.len() >= MIN_PROMPT_LEN].copy()
    print(f"  长度过滤(<{MIN_PROMPT_LEN}字符): {before} → {len(df)}")

    # 内容 hash 去重
    df["_hash"] = df["prompt"].apply(lambda s: hashlib.md5(s.encode("utf-8")).hexdigest())
    before = len(df)
    df = df.drop_duplicates(subset="_hash", keep="first")
    print(f"  去重: {before} → {len(df)}")

    # 分层抽样 400
    df = stratified_sample(df, "category", SEEDANCE_TARGET)
    print(f"  抽样: {len(df)} 条，分布: {dict(Counter(df['category']))}")
    return df.drop(columns=["_hash"]).rename(columns={"id": "source_id"}).reset_index(drop=True)


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS data_sources (
        dataset     TEXT PRIMARY KEY,
        source_url  TEXT,
        raw_file    TEXT,
        raw_rows    INTEGER,
        kept_rows   INTEGER,
        sampled_rows INTEGER,
        is_simulated INTEGER DEFAULT 0,
        note        TEXT
    );
    CREATE TABLE IF NOT EXISTS eavsd_cases (
        case_id         TEXT PRIMARY KEY,
        category        TEXT,
        source          TEXT,
        n_scenes_generated INTEGER,
        n_scenes_total  INTEGER,
        is_simulated    INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS eavsd_scenes (
        scene_id    INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id     TEXT,
        category    TEXT,
        scene_index INTEGER,
        scene_prompt TEXT,
        is_simulated INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS seedance_prompts (
        prompt_id   INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id   TEXT,
        category    TEXT,
        platform    TEXT,
        date        TEXT,
        prompt      TEXT,
        is_simulated INTEGER DEFAULT 0
    );
    """)


def main() -> None:
    cases, scenes = clean_eavsd()
    seedance = clean_seedance()

    print("\n[落库] 写入 SQLite...")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        init_db(conn)
        conn.execute("DELETE FROM eavsd_cases")
        conn.execute("DELETE FROM eavsd_scenes")
        conn.execute("DELETE FROM seedance_prompts")
        conn.execute("DELETE FROM data_sources")

        cases.to_sql("eavsd_cases", conn, if_exists="append", index=False)
        scenes.to_sql("eavsd_scenes", conn, if_exists="append", index=False)
        seedance.to_sql("seedance_prompts", conn, if_exists="append", index=False)

        raw_sources = [
            ("EAVSD", "https://huggingface.co/datasets/zjyao-PKU/EAVSD",
             "metadata/category_a..h.parquet", len(load_eavsd()), len(cases), len(cases),
             "真实数据，分层抽样 50 case，展开为场景级"),
            ("Seedance-2 prompts", "https://huggingface.co/datasets/GokuScraper/seedance-2-prompts-datasets",
             "metadata.jsonl", len(load_seedance()), len(seedance), len(seedance),
             "真实数据，去重后分层抽样 400 条"),
        ]
        conn.executemany(
            "INSERT INTO data_sources(dataset, source_url, raw_file, raw_rows, kept_rows, sampled_rows, is_simulated, note) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [(*r[:6], 0, r[6]) for r in raw_sources],
        )
        conn.commit()

        print("\n===== 落库验证 =====")
        for table in ["eavsd_cases", "eavsd_scenes", "seedance_prompts", "data_sources"]:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {n} 行")
        print("\n===== 样例：EAVSD case =====")
        row = conn.execute("SELECT case_id, category, n_scenes_generated FROM eavsd_cases LIMIT 3").fetchall()
        for r in row:
            print("  ", r)
        print("\n===== 样例：Seedance prompt =====")
        for r in conn.execute("SELECT source_id, category, substr(prompt,1,80) FROM seedance_prompts LIMIT 3"):
            print("  ", r)
    finally:
        conn.close()
    print(f"\n数据库已写入 {DB_PATH}")
    print("D1 数据管线完成")


if __name__ == "__main__":
    sys.exit(main())
