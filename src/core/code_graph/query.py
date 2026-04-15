"""代码图的高层查询 API。"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.core.code_graph.store import CodeGraphStore, NodeRow
from src.utils.logger import logger


@dataclass
class BlastRadius:
    """单个符号的影响面分析结果。"""
    target: str                                     # 目标符号的简要描述
    target_location: str = ""                       # 目标位置 file:line
    upstream_callers: list[dict] = field(default_factory=list)   # 上游调用者
    downstream_callees: list[dict] = field(default_factory=list) # 下游被调用者
    siblings: list[dict] = field(default_factory=list)           # 同文件兄弟符号


class CodeGraph:
    """代码图查询门面，组合 store 提供高层 API。"""

    def __init__(self, repo_dir: str):
        self.repo_dir = repo_dir
        self.store = CodeGraphStore(repo_dir)

    def close(self) -> None:
        self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def available(self) -> bool:
        """代码图是否可用（DB 存在且至少包含一个节点）。"""
        try:
            return not self.store.is_empty()
        except Exception:
            return False

    # ── 基础查询 ────────────────────────────────────────────────

    def find_definition(
        self, symbol: str, lang: str | None = None, limit: int = 10,
    ) -> list[dict]:
        """查找符号的定义位置列表。"""
        rows = self.store.find_nodes_by_name(symbol, limit=limit)
        return [self._node_to_dict(r) for r in rows]

    def find_references(self, symbol: str, limit: int = 50) -> list[dict]:
        """查找所有引用（调用/继承）该符号的节点。"""
        rows = self.store.find_callers_of(symbol, limit=limit)
        return [self._node_to_dict(r) for r in rows]

    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        """基于 FTS5 的符号全文检索。"""
        rows = self.store.fts_search(query, limit=limit)
        return [self._node_to_dict(r) for r in rows]

    # ── 影响面分析 ──────────────────────────────────────────────

    def get_blast_radius(
        self,
        file_path: str,
        symbol: str | None = None,
        depth: int = 2,
    ) -> list[BlastRadius]:
        """计算某个文件中一个或多个符号的影响面。

        - ``symbol`` 给定：仅分析该符号
        - ``symbol`` 为 None：分析文件中所有 function/method/class
        """
        depth = max(1, depth)
        file_nodes = self.store.find_nodes_by_path(file_path)
        if symbol:
            file_nodes = [n for n in file_nodes if n.name == symbol]
        if not file_nodes:
            return []

        # 同文件兄弟节点（供 Agent 理解代码组织）
        sibling_map = {n.id: [self._node_to_dict(s) for s in file_nodes if s.id != n.id]
                       for n in file_nodes}

        results: list[BlastRadius] = []
        for node in file_nodes:
            if node.kind not in ("function", "method", "class"):
                continue

            upstream = self._walk_upstream(node.name, depth)
            downstream = self._walk_downstream(node.id, depth)

            results.append(BlastRadius(
                target=f"{node.kind} {node.name}",
                target_location=f"{node.path}:{node.line_start}",
                upstream_callers=upstream,
                downstream_callees=downstream,
                siblings=sibling_map.get(node.id, [])[:5],
            ))
        return results

    def get_blast_radius_for_nodes(
        self,
        file_path: str,
        nodes: list[NodeRow],
        depth: int = 2,
    ) -> list[BlastRadius]:
        """对指定节点列表计算影响面（跳过 find_nodes_by_path 全量查询）。"""
        depth = max(1, depth)
        if not nodes:
            return []

        sibling_map = {n.id: [self._node_to_dict(s) for s in nodes if s.id != n.id]
                       for n in nodes}

        results: list[BlastRadius] = []
        for node in nodes:
            if node.kind not in ("function", "method", "class"):
                continue
            upstream = self._walk_upstream(node.name, depth)
            downstream = self._walk_downstream(node.id, depth)
            results.append(BlastRadius(
                target=f"{node.kind} {node.name}",
                target_location=f"{node.path}:{node.line_start}",
                upstream_callers=upstream,
                downstream_callees=downstream,
                siblings=sibling_map.get(node.id, [])[:5],
            ))
        return results

    def _walk_upstream(self, name: str, depth: int) -> list[dict]:
        """BFS 向上追溯：所有（传递地）调用了 ``name`` 的节点。"""
        seen: set[int] = set()
        frontier: list[str] = [name]
        results: list[dict] = []
        for _ in range(depth):
            next_frontier: list[str] = []
            for target_name in frontier:
                for caller in self.store.find_callers_of(target_name, limit=20):
                    if caller.id in seen:
                        continue
                    seen.add(caller.id)
                    results.append(self._node_to_dict(caller))
                    next_frontier.append(caller.name)
            frontier = next_frontier
            if not frontier:
                break
        return results[:30]

    def _walk_downstream(self, node_id: int, depth: int) -> list[dict]:
        """BFS 向下追溯：从 ``node_id`` 出发传递性调用到的函数。"""
        seen_names: set[str] = set()
        seen_ids: set[int] = {node_id}
        frontier_ids = [node_id]
        results: list[dict] = []
        for _ in range(depth):
            next_ids: list[int] = []
            for nid in frontier_ids:
                for callee_name in self.store.get_callees_of(nid, limit=20):
                    if callee_name in seen_names:
                        continue
                    seen_names.add(callee_name)
                    # 尝试解析到具体节点（stdlib/第三方符号可能找不到）
                    nodes = self.store.find_nodes_by_name(callee_name, limit=3)
                    if nodes:
                        for n in nodes:
                            if n.id in seen_ids:
                                continue
                            seen_ids.add(n.id)
                            results.append(self._node_to_dict(n))
                            next_ids.append(n.id)
                    else:
                        # 未解析的外部符号：仅记录名称
                        results.append({
                            "name": callee_name,
                            "kind": "external",
                            "location": "",
                            "signature": "",
                        })
            frontier_ids = next_ids
            if not frontier_ids:
                break
        return results[:30]

    @staticmethod
    def _node_to_dict(row: NodeRow) -> dict:
        """NodeRow → 可序列化 dict，供 Agent prompt 和工具返回使用。"""
        return {
            "name": row.name,
            "kind": row.kind,
            "location": f"{row.path}:{row.line_start}",
            "signature": row.signature,
        }
