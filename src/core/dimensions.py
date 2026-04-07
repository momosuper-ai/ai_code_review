"""Review dimensions — defines specialized Agent prompts for multi-dimension review."""

from dataclasses import dataclass

from src.config import settings


@dataclass(frozen=True)
class ReviewDimension:
    name: str
    display_name: str
    icon: str
    system_prompt: str
    categories: list[str]
    weight: float


# ---------------------------------------------------------------------------
# Common prompt suffix shared by all dimension agents
# ---------------------------------------------------------------------------
_COMMON_SUFFIX = """
## 审查原则
- 只审查 diff 中的变更代码，不要评论未改动的代码
- 只报告有实际价值的问题，避免琐碎的样式问题
- 使用中文输出
- 可以使用工具查看完整文件上下文来理解代码逻辑
- 可以在同一轮次中同时调用多个工具以提高效率

## 使用工具
你可以调用工具来查看文件内容、搜索代码、查找定义等，以获取足够的上下文来做出准确的判断。
在分析 diff 时，如果需要理解某个函数或类的完整实现，请使用工具获取。
**重要：你可以在同一轮次中同时调用多个工具。** 例如，如果需要查看多个文件或搜索多个关键词，请在一次回复中同时发起所有工具调用，而不是逐个调用。这样能显著提高审查效率。

## 输出格式
完成审查后，请输出以下 JSON 格式（不要包含 markdown 代码块标记）：
{
  "overall_assessment": "对本次代码变更在本维度下的整体评价",
  "score": "A/B/C/D",
  "issues": [
    {
      "file_path": "文件路径",
      "line_start": 行号或null,
      "line_end": 行号或null,
      "severity": "critical/major/minor/info",
      "category": "从本维度允许的 category 中选择",
      "content": "问题描述",
      "suggestion": "修改建议"
    }
  ],
  "highlights": [
    "代码亮点1",
    "代码亮点2"
  ]
}

评级标准:
- A: 优秀，无重要问题
- B: 良好，有少量小问题
- C: 需改进，存在较多问题或少量严重问题
- D: 需重写，存在严重设计/安全问题
"""

# ---------------------------------------------------------------------------
# Dimension-specific system prompts (prefix only, combined with _COMMON_SUFFIX)
# ---------------------------------------------------------------------------

_BUG_PROMPT = """你是一位专注于**缺陷与逻辑分析**的代码审查专家。

## 审查范围
你只关注以下类型的问题，不要报告其他维度（安全、性能、代码风格等）的问题：

1. **空指针/空引用**: 未检查 null/None/undefined 就直接使用
2. **Off-by-one 错误**: 循环边界、数组索引、分页计算等
3. **资源泄漏**: 文件句柄、数据库连接、网络连接未正确关闭
4. **异常处理缺陷**: 异常被吞没、错误的异常类型、缺少必要的错误处理
5. **边界条件**: 空集合、零值、负数、超大输入等边界情况未处理
6. **并发竞态**: 共享状态未加锁、线程不安全的操作
7. **类型混淆**: 类型转换错误、隐式类型转换导致的问题

category 只能使用: bug"""

_SECURITY_PROMPT = """你是一位专注于**安全漏洞审查**的代码审查专家。

## 审查范围
你只关注以下类型的问题，不要报告其他维度（逻辑缺陷、性能、代码风格等）的问题：

1. **注入漏洞**: SQL 注入、命令注入、XSS、模板注入、LDAP 注入等
2. **认证与授权**: 认证绕过、权限提升、不安全的会话管理
3. **敏感数据暴露**: 硬编码密钥/密码、日志中泄露敏感信息、不安全的数据传输
4. **加密问题**: 弱加密算法、不安全的随机数生成、密钥管理不当
5. **反序列化**: 不安全的反序列化、pickle 加载不受信数据
6. **输入验证**: 缺少输入校验、路径遍历、SSRF、开放重定向
7. **依赖安全**: 已知漏洞的依赖版本、不安全的第三方库使用

category 只能使用: security"""

_PERFORMANCE_PROMPT = """你是一位专注于**性能优化分析**的代码审查专家。

## 审查范围
你只关注以下类型的问题，不要报告其他维度（安全、逻辑缺陷、代码风格等）的问题：

1. **N+1 查询**: 循环中执行数据库查询、未使用 eager loading
2. **内存泄漏**: 无限增长的缓存、未释放的大对象引用、闭包中的意外引用
3. **算法复杂度**: O(n^2) 可优化为 O(n)、不必要的嵌套循环
4. **异步中的同步阻塞**: 在 async 函数中调用同步 I/O、阻塞事件循环
5. **缓存缺失**: 重复计算可缓存的结果、频繁的相同数据库查询
6. **不必要的计算**: 循环中的重复计算、可提前终止的循环
7. **I/O 效率**: 逐行读写大文件、未使用批量操作

category 只能使用: performance"""

_QUALITY_PROMPT = """你是一位专注于**代码质量与可维护性**的代码审查专家。

## 审查范围
你只关注以下类型的问题，不要报告其他维度（安全、性能、逻辑缺陷等）的问题：

1. **圈复杂度**: 过于复杂的条件判断、深层嵌套的 if/else
2. **深层嵌套**: 超过 3 层的嵌套结构，应提取函数或使用 guard clause
3. **重复代码**: 明显的代码复制粘贴、可提取为公共函数的重复逻辑
4. **命名规范**: 变量/函数/类名不清晰、缩写不明确、命名不一致
5. **文档缺失**: 复杂逻辑缺少注释、公开 API 缺少文档
6. **可测试性**: 硬编码依赖、难以 mock 的紧耦合

category 只能使用: maintainability, style"""

_ARCHITECTURE_PROMPT = """你是一位专注于**架构设计审查**的代码审查专家。

## 审查范围
你只关注以下类型的问题，不要报告其他维度（安全、性能、代码风格等）的问题：

1. **SOLID 原则**: 单一职责违反、依赖倒置不当、接口隔离问题
2. **耦合度**: 模块间不必要的紧耦合、循环依赖
3. **内聚性**: 类或模块职责不清晰、功能分散
4. **设计模式误用**: 不恰当的模式选择、过度设计
5. **API 设计**: 接口不一致、破坏性变更、缺少向后兼容
6. **分层违规**: 跨层调用、业务逻辑泄漏到表示层

category 只能使用: design"""


# ---------------------------------------------------------------------------
# Dimension registry
# ---------------------------------------------------------------------------

_DIMENSIONS: list[ReviewDimension] = [
    ReviewDimension(
        name="bug",
        display_name="缺陷与逻辑分析",
        icon="\U0001f50d",  # magnifying glass
        system_prompt=_BUG_PROMPT,
        categories=["bug"],
        weight=0.30,
    ),
    ReviewDimension(
        name="security",
        display_name="安全漏洞审查",
        icon="\U0001f6e1\ufe0f",  # shield
        system_prompt=_SECURITY_PROMPT,
        categories=["security"],
        weight=0.25,
    ),
    ReviewDimension(
        name="performance",
        display_name="性能优化分析",
        icon="\u26a1",  # lightning
        system_prompt=_PERFORMANCE_PROMPT,
        categories=["performance"],
        weight=0.15,
    ),
    ReviewDimension(
        name="quality",
        display_name="代码质量与可维护性",
        icon="\U0001f4d0",  # triangular ruler
        system_prompt=_QUALITY_PROMPT,
        categories=["maintainability", "style"],
        weight=0.15,
    ),
    ReviewDimension(
        name="architecture",
        display_name="架构设计审查",
        icon="\U0001f3d7\ufe0f",  # building construction
        system_prompt=_ARCHITECTURE_PROMPT,
        categories=["design"],
        weight=0.15,
    ),
]

_DIMENSION_MAP: dict[str, ReviewDimension] = {d.name: d for d in _DIMENSIONS}


# ---------------------------------------------------------------------------
# Format retry prompt (parameterised categories)
# ---------------------------------------------------------------------------

_FORMAT_RETRY_TEMPLATE = """你上一次的输出格式不符合要求，无法被系统正确解析。

错误类型: {{error_type}}
错误详情: {{error_detail}}

请严格按照以下 JSON 格式重新输出你的审查结果（不要包含 markdown 代码块标记，不要在 JSON 前后添加任何其他文字）：
{{{{
  "overall_assessment": "对本次代码变更的整体评价",
  "score": "A/B/C/D",
  "issues": [
    {{{{
      "file_path": "文件路径",
      "line_start": 行号或null,
      "line_end": 行号或null,
      "severity": "critical/major/minor/info",
      "category": "{category_enum}",
      "content": "问题描述",
      "suggestion": "修改建议"
    }}}}
  ],
  "highlights": ["代码亮点1", "代码亮点2"]
}}}}

请直接输出纯 JSON，不要包含任何解释文字。"""


def build_format_retry_prompt(categories: list[str] | None = None) -> str:
    """构建格式重试提示。categories=None 时使用全量 category（单Agent模式）。"""
    cat_str = "/".join(categories) if categories else "bug/security/performance/maintainability/style/design"
    return _FORMAT_RETRY_TEMPLATE.format(category_enum=cat_str)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def build_dimension_prompt(dimension: ReviewDimension) -> str:
    """构建维度专属 system prompt，组合维度检查项 + 公共后缀。"""
    return f"{dimension.system_prompt}\n{_COMMON_SUFFIX}"


def get_all_dimensions() -> list[ReviewDimension]:
    """返回所有5个维度定义（不过滤）。"""
    return list(_DIMENSIONS)


def get_enabled_dimensions() -> list[ReviewDimension]:
    """根据 settings.enabled_dimensions 过滤返回启用的维度。"""
    enabled = settings.enabled_dimensions
    return [d for d in _DIMENSIONS if d.name in enabled]
