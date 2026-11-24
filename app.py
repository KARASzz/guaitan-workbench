#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
怪谈·交互式文字生产工作台 - 云端服务与数据库内核 (FastAPI + Cloud SQL)
========================================================================
1. 提供生产级 Web 交互工作台服务 (Cloud Run 容器化托管)。
2. 打通 Google Cloud SQL (PostgreSQL 18) 实例 `super-roach-db`。
3. 将怪谈工作台的所有世界观设定、角色档案、协议规程及章节产出，
   作为独立分支深度挂载于个人知识库 `wiki_pages` 表 (`path` 前缀为 `guaitan/`)。
4. 保证工程状态与章节资产持久化，多端无缝协同。
"""

import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

app = FastAPI(title="怪谈·交互式文字生产工作台", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "horror-writing-workbench.html"

# =========================================================================
# 数据库引擎与连接池配置 (Cloud SQL PostgreSQL)
# =========================================================================
_db_engine: Optional[Engine] = None

def get_db_engine() -> Optional[Engine]:
    global _db_engine
    if _db_engine is not None:
        return _db_engine

    db_user = os.getenv("DB_USER", "postgres")
    db_pass = os.getenv("DB_PASSWORD") or os.getenv("DB_PASS", "")
    db_name = os.getenv("DB_NAME", "postgres")
    db_host = os.getenv("DB_HOST", "")
    db_port = os.getenv("DB_PORT", "5432")
    conn_name = os.getenv("INSTANCE_CONNECTION_NAME", "")

    if not db_pass:
        print("[DB] 未提供 DB_PASSWORD，运行于无数据库离线模式")
        return None

    try:
        encoded_pass = urllib.parse.quote_plus(db_pass)
        if conn_name:
            # Cloud Run 直连 Cloud SQL Unix Domain Socket
            socket_path = f"/cloudsql/{conn_name}"
            conn_str = f"postgresql+psycopg2://{db_user}:{encoded_pass}@/{db_name}?host={socket_path}"
        elif db_host and db_host.startswith("/cloudsql/"):
            conn_str = f"postgresql+psycopg2://{db_user}:{encoded_pass}@/{db_name}?host={db_host}"
        elif db_host:
            conn_str = f"postgresql+psycopg2://{db_user}:{encoded_pass}@{db_host}:{db_port}/{db_name}"
        else:
            print("[DB] 未指定 INSTANCE_CONNECTION_NAME 或 DB_HOST")
            return None

        _db_engine = create_engine(conn_str, pool_pre_ping=True, pool_size=5, max_overflow=10)
        print(f"[DB] 数据库连接池已就绪：{db_user}@{db_name} (实例: {conn_name or db_host})")
        return _db_engine
    except Exception as e:
        print(f"[DB] 初始化数据库连接失败: {e}")
        return None


# =========================================================================
# 数据库表结构自动保障 (个人 Wiki 独立分支 + 怪谈状态机)
# =========================================================================
def init_db(engine: Engine):
    """确保 wiki_pages 及怪谈专用状态表存在，并初始化同步本地设定资产"""
    if not engine:
        return
    try:
        with engine.begin() as conn:
            # 1. 确保个人 Wiki 核心主表存在 (与 Super-Roach 架构同轨)
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS wiki_pages (
                id BIGSERIAL PRIMARY KEY,
                path VARCHAR(256) UNIQUE NOT NULL,
                title VARCHAR(256) NOT NULL,
                category VARCHAR(64) NOT NULL,
                type VARCHAR(32) DEFAULT 'note',
                tags JSONB DEFAULT '[]'::JSONB,
                summary TEXT,
                content TEXT NOT NULL,
                sha256 VARCHAR(64),
                line_count INTEGER DEFAULT 0,
                outbound_links JSONB DEFAULT '[]'::JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_title ON wiki_pages(title);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_category ON wiki_pages(category);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_path ON wiki_pages(path);
            CREATE INDEX IF NOT EXISTS idx_wiki_pages_type ON wiki_pages(type);
            """))

            # 2. 确保工作台状态持久化表存在
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS guaitan_state (
                project_id VARCHAR(64) PRIMARY KEY,
                state_data JSONB NOT NULL,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
            """))

            # 3. 确保章节独立台账表存在
            conn.execute(text("""
            CREATE TABLE IF NOT EXISTS guaitan_chapters (
                id BIGSERIAL PRIMARY KEY,
                project_id VARCHAR(64) NOT NULL,
                chapter_index INTEGER NOT NULL,
                title VARCHAR(256) NOT NULL,
                summary TEXT,
                content TEXT NOT NULL,
                delta JSONB,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(project_id, chapter_index)
            );
            CREATE INDEX IF NOT EXISTS idx_guaitan_chapters_proj ON guaitan_chapters(project_id);
            """))
            print("[DB] 表结构校验通过 (wiki_pages, guaitan_state, guaitan_chapters)")

        # 启动时自动将本地 4 份核心规程同步至 wiki_pages 的独立分支
        sync_local_docs_to_wiki(engine)
    except Exception as e:
        print(f"[DB] 初始化表结构异常: {e}")


def sync_local_docs_to_wiki(engine: Engine):
    """将本地规范文档灌入 wiki_pages 表中的 guaitan/ 分支"""
    docs_to_sync = [
        ("01-世界观设定集.md", "guaitan/rules/01-世界观设定集.md", "怪谈世界设定集", "怪谈·世界观设定", "world_setting"),
        ("02-角色档案.md", "guaitan/rules/02-角色档案.md", "主要角色档案与关系阈值", "怪谈·角色档案", "character_profile"),
        ("03-章节写作协议.md", "guaitan/rules/03-章节写作协议.md", "章节写作与状态增量协议", "怪谈·写作协议", "protocol"),
        ("foundry-agent-instructions.md", "guaitan/rules/foundry-agent-instructions.md", "智能体系统指令 (Foundry/OpenAI)", "怪谈·智能体指令", "instructions"),
    ]

    try:
        with engine.begin() as conn:
            for filename, wiki_path, title, category, doc_type in docs_to_sync:
                file_path = BASE_DIR / filename
                if not file_path.exists():
                    continue
                content = file_path.read_text(encoding="utf-8")
                sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                lines = len(content.splitlines())
                
                # 提取第一段或者前150字作为摘要
                summary = re.sub(r"^[#\s\-*]+", "", content.strip()).split("\n")[0][:150]

                conn.execute(text("""
                INSERT INTO wiki_pages (path, title, category, type, tags, summary, content, sha256, line_count, updated_at)
                VALUES (:path, :title, :category, :type, :tags::JSONB, :summary, :content, :sha256, :line_count, CURRENT_TIMESTAMP)
                ON CONFLICT (path) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    type = EXCLUDED.type,
                    summary = EXCLUDED.summary,
                    content = EXCLUDED.content,
                    sha256 = EXCLUDED.sha256,
                    line_count = EXCLUDED.line_count,
                    updated_at = CURRENT_TIMESTAMP;
                """), {
                    "path": wiki_path,
                    "title": title,
                    "category": category,
                    "type": doc_type,
                    "tags": json.dumps(["怪谈", "工作台", doc_type], ensure_ascii=False),
                    "summary": summary,
                    "content": content,
                    "sha256": sha256,
                    "line_count": lines,
                })
        print("[DB] 本地规则与设定文档已同步至 wiki_pages (分支: guaitan/rules/)")
    except Exception as e:
        print(f"[DB] 同步本地设定至 Wiki 失败: {e}")


@app.on_event("startup")
def on_startup():
    engine = get_db_engine()
    if engine:
        init_db(engine)


# =========================================================================
# API 路由设计
# =========================================================================

@app.get("/", response_class=HTMLResponse)
def index_page():
    """返回主生产工作台页面"""
    if not HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="工作台前端文件不存在")
    return HTMLResponse(content=HTML_PATH.read_text(encoding="utf-8"))


@app.get("/health")
@app.get("/_stcore/health")
def health_check():
    """兼容 Cloud Run 健康探活接口"""
    engine = get_db_engine()
    db_ok = False
    if engine:
        try:
            with engine.connect() as conn:
                res = conn.execute(text("SELECT 1;")).scalar()
                db_ok = (res == 1)
        except Exception:
            db_ok = False
    return {"status": "ok", "db_connected": db_ok, "service": "guaitan-workbench"}


@app.get("/api/db/status")
def db_status():
    """返回 Cloud SQL 连接及个人 Wiki 分支统计"""
    engine = get_db_engine()
    if not engine:
        return {"connected": False, "mode": "local_fallback", "message": "未连接 Cloud SQL"}

    try:
        with engine.connect() as conn:
            wiki_count = conn.execute(text("SELECT COUNT(*) FROM wiki_pages WHERE path LIKE 'guaitan/%';")).scalar()
            all_wiki_count = conn.execute(text("SELECT COUNT(*) FROM wiki_pages;")).scalar()
            chapter_count = conn.execute(text("SELECT COUNT(*) FROM guaitan_chapters;")).scalar()
            has_state = conn.execute(text("SELECT COUNT(*) FROM guaitan_state WHERE project_id = 'default';")).scalar()

            return {
                "connected": True,
                "instance": os.getenv("INSTANCE_CONNECTION_NAME", "local-tcp"),
                "database": os.getenv("DB_NAME", "postgres"),
                "guaitan_wiki_pages": wiki_count or 0,
                "total_instance_wiki_pages": all_wiki_count or 0,
                "saved_chapters": chapter_count or 0,
                "has_cloud_state": bool(has_state),
            }
    except Exception as e:
        return {"connected": False, "error": str(e)}


@app.get("/api/state")
def get_project_state(project_id: str = "default"):
    """从 Cloud SQL 获取最新工程全局状态"""
    engine = get_db_engine()
    if not engine:
        return {"status": "offline", "data": None}

    try:
        with engine.connect() as conn:
            row = conn.execute(text(
                "SELECT state_data, updated_at FROM guaitan_state WHERE project_id = :pid;"
            ), {"pid": project_id}).fetchone()

            if row:
                return {
                    "status": "success",
                    "data": row[0],
                    "updated_at": row[1].isoformat() if row[1] else None,
                }
            return {"status": "empty", "data": None}
    except Exception as e:
        return {"status": "error", "message": str(e), "data": None}


@app.post("/api/state")
def save_project_state(payload: Dict[str, Any] = Body(...), project_id: str = "default"):
    """
    保存最新工程状态至 Cloud SQL，并同步回写至个人 Wiki 的 independent branch:
    1. 写入 guaitan_state (完整状态机快照)
    2. 写入 wiki_pages (path: guaitan/state/horror_workbench_v1.json)
    3. 写入 wiki_pages (各已归档章节: guaitan/chapters/chapter_xxx.md)
    """
    engine = get_db_engine()
    if not engine:
        return {"status": "offline_skipped", "message": "本地无数据库环境"}

    try:
        state_json = json.dumps(payload, ensure_ascii=False)
        with engine.begin() as conn:
            # 1. 存储至专用状态表
            conn.execute(text("""
            INSERT INTO guaitan_state (project_id, state_data, updated_at)
            VALUES (:pid, :state::JSONB, CURRENT_TIMESTAMP)
            ON CONFLICT (project_id) DO UPDATE SET
                state_data = EXCLUDED.state_data,
                updated_at = CURRENT_TIMESTAMP;
            """), {"pid": project_id, "state": state_json})

            # 2. 存入个人 Wiki 独立分支下的工程快照
            conn.execute(text("""
            INSERT INTO wiki_pages (path, title, category, type, tags, summary, content, sha256, line_count, updated_at)
            VALUES (:path, :title, :category, 'project_state', :tags::JSONB, :summary, :content, :sha, :lines, CURRENT_TIMESTAMP)
            ON CONFLICT (path) DO UPDATE SET
                title = EXCLUDED.title,
                summary = EXCLUDED.summary,
                content = EXCLUDED.content,
                sha256 = EXCLUDED.sha256,
                line_count = EXCLUDED.line_count,
                updated_at = CURRENT_TIMESTAMP;
            """), {
                "path": "guaitan/state/horror_workbench_v1.json",
                "title": f"怪谈工作台状态快照 · {payload.get('world', {}).get('name', '未命名')}",
                "category": "怪谈·工程状态",
                "tags": json.dumps(["怪谈", "工程状态", payload.get('world', {}).get('seed', '')], ensure_ascii=False),
                "summary": f"第 {payload.get('chapter', {}).get('index', 1)} 章进度，包含 {len(payload.get('characters', []))} 名角色与 {len(payload.get('chapters', []))} 章已归档内容",
                "content": json.dumps(payload, ensure_ascii=False, indent=2),
                "sha": hashlib.sha256(state_json.encode('utf-8')).hexdigest(),
                "lines": len(state_json.splitlines()),
            })

            # 3. 同步已归档章节到 guaitan_chapters 与 wiki_pages
            chapters = payload.get("chapters", [])
            for ch in chapters:
                c_idx = ch.get("index")
                if not c_idx:
                    continue
                c_title = ch.get("title") or f"第{c_idx}章"
                c_text = ch.get("text", "")
                c_sum = ch.get("summary", "")
                c_path = f"guaitan/chapters/chapter_{int(c_idx):03d}.md"

                # 插入章节表
                conn.execute(text("""
                INSERT INTO guaitan_chapters (project_id, chapter_index, title, summary, content, delta, updated_at)
                VALUES (:pid, :c_idx, :title, :summary, :content, :delta::JSONB, CURRENT_TIMESTAMP)
                ON CONFLICT (project_id, chapter_index) DO UPDATE SET
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    content = EXCLUDED.content,
                    delta = EXCLUDED.delta,
                    updated_at = CURRENT_TIMESTAMP;
                """), {
                    "pid": project_id,
                    "c_idx": c_idx,
                    "title": c_title,
                    "summary": c_sum,
                    "content": c_text,
                    "delta": json.dumps(ch.get("delta") or {}, ensure_ascii=False),
                })

                # 同步至个人 Wiki 页面
                conn.execute(text("""
                INSERT INTO wiki_pages (path, title, category, type, tags, summary, content, sha256, line_count, updated_at)
                VALUES (:path, :title, :category, 'chapter', :tags::JSONB, :summary, :content, :sha, :lines, CURRENT_TIMESTAMP)
                ON CONFLICT (path) DO UPDATE SET
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    content = EXCLUDED.content,
                    sha256 = EXCLUDED.sha256,
                    line_count = EXCLUDED.line_count,
                    updated_at = CURRENT_TIMESTAMP;
                """), {
                    "path": c_path,
                    "title": f"第 {c_idx} 章 · {c_title}",
                    "category": "怪谈·章节正文",
                    "tags": json.dumps(["怪谈", "小说正文", f"第{c_idx}章"], ensure_ascii=False),
                    "summary": c_sum,
                    "content": c_text,
                    "sha": hashlib.sha256(c_text.encode('utf-8')).hexdigest(),
                    "lines": len(c_text.splitlines()),
                })

        return {"status": "success", "saved_chapters": len(chapters)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/wiki/pages")
def list_wiki_pages():
    """查看实例内属于怪谈工作台独立分支 (guaitan/%) 的所有 Wiki 文档"""
    engine = get_db_engine()
    if not engine:
        return {"pages": []}

    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
            SELECT path, title, category, type, summary, line_count, updated_at
            FROM wiki_pages
            WHERE path LIKE 'guaitan/%'
            ORDER BY path ASC;
            """)).fetchall()

            pages = [{
                "path": r[0],
                "title": r[1],
                "category": r[2],
                "type": r[3],
                "summary": r[4],
                "line_count": r[5],
                "updated_at": r[6].isoformat() if r[6] else None,
            } for r in rows]
            return {"pages": pages, "total": len(pages)}
    except Exception as e:
        return {"error": str(e), "pages": []}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
