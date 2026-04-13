"""持久化代码关系图，用于影响面（blast-radius）分析。

构建一个轻量级、绑定单个 repo 的 SQLite 代码图：
- 节点：函数、类、方法等代码实体
- 边：调用（calls）、继承（inherits）等关系

用于支持"谁调用了这个函数"、"某个变更的影响面"等结构化查询，
把 Agent 的代码探索从正则 grep 升级为结构化图查询。
"""

from src.core.code_graph.store import CodeGraphStore
from src.core.code_graph.query import CodeGraph
from src.core.code_graph.incremental import update_graph_incremental

__all__ = ["CodeGraph", "CodeGraphStore", "update_graph_incremental"]
