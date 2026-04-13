"""代码图的 SQLite 持久化层。

数据库 schema：
    nodes (id, path, kind, name, line_start, line_end, signature, content_hash)
    edges (src_id, dst_name, kind)
    files (path, content_hash, language)
    nodes_fts FTS5 虚表，索引 (name, signature)

每个 repo 一份独立数据库文件，存放在 ``<repo_dir>/.code_graph.db``。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from src.core.code_graph.parser import ParsedFile, ParsedNode


DB_FILENAME = ".code_graph.db"


# 基础表 schema（FTS5 虚表单独建，以便老版本 SQLite 兼容降级）
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    name         TEXT    NOT NULL,
    line_start   INTEGER NOT NULL,
    line_end     INTEGER NOT NULL,
    signature    TEXT    NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);

CREATE TABLE IF NOT EXISTS edges (
    src_id   INTEGER NOT NULL,
    dst_name TEXT    NOT NULL,
    kind     TEXT    NOT NULL,
    FOREIGN KEY(src_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_name);

CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language     TEXT NOT NULL
);
"""

# FTS5 虚表（部分 SQLite 构建缺失 FTS5 模块，失败则降级使用 LIKE）
_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, signature, content='nodes', content_rowid='id'
);
"""


@dataclass
class NodeRow:
    """代码图节点的查询结果行。"""
    id: int
    path: str
    kind: str
    name: str
    line_start: int
    line_end: int
    signature: str


class CodeGraphStore:
    """SQLite 存储封装（非线程安全；调用方通过文件锁串行化）。"""

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self.db_path = os.path.join(repo_dir, DB_FILENAME)
        self._conn: sqlite3.Connection | None = None

    # ── 连接管理 ────────────────────────────────────────────────

    def connect(self) -> sqlite3.Connection:
        """首次调用时建立连接并初始化 schema。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._init_schema()
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def _init_schema(self) -> None:
        """创建基础表和（若可用）FTS5 虚表。"""
        assert self._conn is not None
        self._conn.executescript(_SCHEMA_SQL)
        # FTS5 可选：老版本 SQLite 可能缺失该模块
        try:
            self._conn.executescript(_FTS_SQL)
        except sqlite3.OperationalError:
            pass
        self._conn.commit()

    # ── 查询接口 ────────────────────────────────────────────────

    def is_empty(self) -> bool:
        """判断数据库是否为空（用于首次触发全量扫描）。"""
        conn = self.connect()
        row = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()
        return (row["c"] or 0) == 0

    def get_file_hash(self, rel_path: str) -> str | None:
        """获取某个文件上次解析时的内容哈希，用于跳过未变更文件。"""
        conn = self.connect()
        row = conn.execute(
            "SELECT content_hash FROM files WHERE path = ?", (rel_path,),
        ).fetchone()
        return row["content_hash"] if row else None

    def find_nodes_by_name(self, name: str, limit: int = 50) -> list[NodeRow]:
        """按符号名精确查找节点。"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT * FROM nodes WHERE name = ? LIMIT ?", (name, limit),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def find_nodes_by_path(self, rel_path: str) -> list[NodeRow]:
        """列出某个文件的所有代码节点，按行号升序返回。"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT * FROM nodes WHERE path = ? ORDER BY line_start", (rel_path,),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def find_callers_of(self, name: str, limit: int = 50) -> list[NodeRow]:
        """查找所有调用了 ``name`` 的节点（边类型为 calls）。"""
        conn = self.connect()
        rows = conn.execute(
            """
            SELECT DISTINCT n.*
            FROM edges e
            JOIN nodes n ON n.id = e.src_id
            WHERE e.dst_name = ? AND e.kind = 'calls'
            LIMIT ?
            """,
            (name, limit),
        ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_callees_of(self, src_id: int, limit: int = 50) -> list[str]:
        """获取某个节点调用的所有符号名。"""
        conn = self.connect()
        rows = conn.execute(
            "SELECT DISTINCT dst_name FROM edges WHERE src_id = ? AND kind = 'calls' LIMIT ?",
            (src_id, limit),
        ).fetchall()
        return [r["dst_name"] for r in rows]

    def fts_search(self, query: str, limit: int = 20) -> list[NodeRow]:
        """基于 FTS5 的符号全文检索；若 FTS5 不可用则降级为 LIKE 模糊匹配。"""
        conn = self.connect()
        try:
            rows = conn.execute(
                """
                SELECT n.* FROM nodes_fts
                JOIN nodes n ON n.id = nodes_fts.rowid
                WHERE nodes_fts MATCH ?
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # FTS5 不可用，降级为 LIKE 查询
            rows = conn.execute(
                "SELECT * FROM nodes WHERE name LIKE ? OR signature LIKE ? LIMIT ?",
                (f"%{query}%", f"%{query}%", limit),
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    # ── 写入接口 ────────────────────────────────────────────────

    def delete_file(self, rel_path: str) -> None:
        """删除某个文件对应的所有节点、边和 FTS 记录。"""
        conn = self.connect()
        conn.execute(
            "DELETE FROM edges WHERE src_id IN (SELECT id FROM nodes WHERE path = ?)",
            (rel_path,),
        )
        try:
            conn.execute(
                "DELETE FROM nodes_fts WHERE rowid IN (SELECT id FROM nodes WHERE path = ?)",
                (rel_path,),
            )
        except sqlite3.OperationalError:
            pass
        conn.execute("DELETE FROM nodes WHERE path = ?", (rel_path,))
        conn.execute("DELETE FROM files WHERE path = ?", (rel_path,))

    def upsert_file(self, parsed: ParsedFile) -> int:
        """清除旧记录 → 插入新解析结果，返回写入的节点数。"""
        conn = self.connect()
        self.delete_file(parsed.path)

        conn.execute(
            "INSERT INTO files (path, content_hash, language) VALUES (?, ?, ?)",
            (parsed.path, parsed.content_hash, parsed.language),
        )

        count = 0
        for n in parsed.nodes:
            cursor = conn.execute(
                """
                INSERT INTO nodes (path, kind, name, line_start, line_end, signature, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    parsed.path, n.kind, n.name,
                    n.line_start, n.line_end, n.signature, parsed.content_hash,
                ),
            )
            node_id = cursor.lastrowid
            count += 1

            # FTS5 索引写入（失败静默降级）
            try:
                conn.execute(
                    "INSERT INTO nodes_fts (rowid, name, signature) VALUES (?, ?, ?)",
                    (node_id, n.name, n.signature),
                )
            except sqlite3.OperationalError:
                pass

            # 调用边（src_id -> dst_name 文本，待查询时再解析到具体节点）
            for callee in n.calls:
                conn.execute(
                    "INSERT INTO edges (src_id, dst_name, kind) VALUES (?, ?, 'calls')",
                    (node_id, callee),
                )
            # 继承边
            for base in n.bases:
                conn.execute(
                    "INSERT INTO edges (src_id, dst_name, kind) VALUES (?, ?, 'inherits')",
                    (node_id, base),
                )
        return count

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    # ── 内部辅助 ────────────────────────────────────────────────

    @staticmethod
    def _row_to_node(row: sqlite3.Row) -> NodeRow:
        """sqlite3.Row → NodeRow dataclass。"""
        return NodeRow(
            id=row["id"],
            path=row["path"],
            kind=row["kind"],
            name=row["name"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            signature=row["signature"],
        )
