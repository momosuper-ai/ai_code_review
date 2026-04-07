"""Context length management — estimate tokens and trim messages before LLM calls."""

import copy
import json

from src.config import settings
from src.utils.logger import logger


def estimate_tokens(text: str) -> int:
    """基于字符数估算 token 数量。"""
    if not text:
        return 0
    return max(1, int(len(text) / settings.LLM_CHARS_PER_TOKEN))


def estimate_message_tokens(message: dict) -> int:
    """估算单条消息的 token 数（含角色标记等结构开销，约 4 tokens）。

    支持 dict 和 ChatCompletionMessage 对象。
    """
    overhead = 4  # role + 结构标记
    msg = _as_dict(message)

    tokens = overhead + estimate_tokens(msg.get("role", ""))
    content = msg.get("content")
    if content:
        tokens += estimate_tokens(content)

    # assistant 消息可能有 tool_calls
    tool_calls = msg.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            if isinstance(tc, dict):
                func = tc.get("function", {})
                tokens += estimate_tokens(func.get("name", ""))
                tokens += estimate_tokens(func.get("arguments", ""))
            else:
                # ChatCompletionMessageToolCall 对象
                tokens += estimate_tokens(tc.function.name or "")
                tokens += estimate_tokens(tc.function.arguments or "")

    return tokens


def trim_messages(
    messages: list[dict],
    reserve_for_completion: int = 8192,
) -> list[dict]:
    """裁剪消息列表使其不超过上下文窗口限制。返回新列表，不修改原始 messages。"""
    budget = int(
        settings.LLM_MAX_CONTEXT_TOKENS * settings.LLM_CONTEXT_THRESHOLD
    ) - reserve_for_completion

    total = sum(estimate_message_tokens(m) for m in messages)
    if total <= budget:
        return messages

    logger.info(
        "上下文裁剪触发: 估算token=%d 预算=%d 消息数=%d",
        total, budget, len(messages),
    )

    # 深拷贝，避免修改原始 messages
    trimmed = [_copy_message(m) for m in messages]

    # 保护区边界：前 2 条（system + user）+ 尾部最多 4 条
    # 确保至少留 1 条中间消息可裁剪（len >= 4 时才有中间区）
    mid_start = min(2, len(trimmed))
    tail_count = min(4, max(0, len(trimmed) - mid_start - 1))
    mid_end = len(trimmed) - tail_count  # exclusive

    # 构建 tool_call_id 索引（供阶段 2 使用）
    tool_call_index = _build_tool_call_index(trimmed)

    # 阶段 1 - 压缩工具结果：截断中间区 tool 消息到 2000 字符
    trimmed = _phase1_truncate_tool_results(trimmed, mid_start, mid_end)
    total = sum(estimate_message_tokens(m) for m in trimmed)
    if total <= budget:
        logger.debug("裁剪阶段%d: %s", 1, "压缩工具结果后满足预算")
        _log_trim_done(trimmed, messages)
        return trimmed

    # 阶段 2 - 参数感知的摘要替换
    trimmed = _phase2_summarize_tool_results(
        trimmed, mid_start, mid_end, tool_call_index,
    )
    total = sum(estimate_message_tokens(m) for m in trimmed)
    if total <= budget:
        logger.debug("裁剪阶段%d: %s", 2, "摘要替换后满足预算")
        _log_trim_done(trimmed, messages)
        return trimmed

    # 阶段 3 - 移除历史对话组
    trimmed = _phase3_remove_tool_call_groups(trimmed, mid_start, tail_count, budget)
    total = sum(estimate_message_tokens(m) for m in trimmed)
    if total <= budget:
        logger.debug("裁剪阶段%d: %s", 3, "移除历史组后满足预算")
        _log_trim_done(trimmed, messages)
        return trimmed

    # 阶段 4 - 截断 Diff
    trimmed = _phase4_truncate_diff(trimmed, budget, total)
    _log_trim_done(trimmed, messages)
    return trimmed


def _log_trim_done(trimmed: list[dict], original: list[dict]) -> None:
    total = sum(estimate_message_tokens(m) for m in trimmed)
    logger.info(
        "上下文裁剪完成: 裁剪后token=%d 消息数=%d (原=%d)",
        total, len(trimmed), len(original),
    )


# ---------------------------------------------------------------------------
# Message conversion helpers
# ---------------------------------------------------------------------------

def _as_dict(message) -> dict:
    """将消息转为 dict 供读取（不拷贝）。"""
    if isinstance(message, dict):
        return message
    # ChatCompletionMessage 对象
    result = {"role": getattr(message, "role", "assistant")}
    if message.content is not None:
        result["content"] = message.content
    if getattr(message, "tool_calls", None):
        result["tool_calls"] = message.tool_calls
    if getattr(message, "tool_call_id", None):
        result["tool_call_id"] = message.tool_call_id
    return result


def _copy_message(message) -> dict:
    """将消息统一深拷贝为 dict 格式。"""
    if isinstance(message, dict):
        return copy.deepcopy(message)
    # ChatCompletionMessage → dict
    result: dict = {"role": getattr(message, "role", "assistant")}
    if message.content is not None:
        result["content"] = message.content
    if getattr(message, "tool_call_id", None):
        result["tool_call_id"] = message.tool_call_id
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        result["tool_calls"] = []
        for tc in tool_calls:
            if isinstance(tc, dict):
                result["tool_calls"].append(copy.deepcopy(tc))
            else:
                result["tool_calls"].append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
    return result


# ---------------------------------------------------------------------------
# Tool call index
# ---------------------------------------------------------------------------

def _build_tool_call_index(messages: list[dict]) -> dict[str, tuple[str, dict]]:
    """构建 tool_call_id -> (function_name, arguments_dict) 映射表。"""
    index: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        tool_calls = msg.get("tool_calls")
        if not tool_calls or msg.get("role") != "assistant":
            continue
        for tc in tool_calls:
            if isinstance(tc, dict):
                tc_id = tc.get("id", "")
                func = tc.get("function", {})
                name = func.get("name", "")
                args_str = func.get("arguments", "{}")
            else:
                tc_id = tc.id
                name = tc.function.name
                args_str = tc.function.arguments
            try:
                args = json.loads(args_str) if args_str else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            index[tc_id] = (name, args)
    return index


def _summarize_tool_result(
    tool_call_id: str,
    original_length: int,
    index: dict[str, tuple[str, dict]],
) -> str:
    """根据工具调用参数生成描述性摘要。"""
    info = index.get(tool_call_id)
    if not info:
        return f"[已省略: 工具返回结果，共 {original_length} 字符]"

    name, args = info

    if name == "read_file":
        path = args.get("path", "未知文件")
        start = args.get("start_line")
        end = args.get("end_line")
        if start and end:
            return f"[已省略: 读取了 {path} 第{start}-{end}行的内容，共 {original_length} 字符]"
        return f"[已省略: 读取了 {path} 的内容，共 {original_length} 字符]"

    if name == "search_code":
        pattern = args.get("pattern", "")
        file_pattern = args.get("file_pattern", "*")
        return f"[已省略: 在 {file_pattern} 中搜索 '{pattern}' 的结果，共 {original_length} 字符]"

    if name == "find_definition":
        symbol = args.get("symbol", "")
        return f"[已省略: 查找 {symbol} 的定义，共 {original_length} 字符]"

    if name == "list_directory":
        path = args.get("path", ".")
        return f"[已省略: 列出 {path} 目录内容，共 {original_length} 字符]"

    if name == "get_file_info":
        path = args.get("path", "未知文件")
        return f"[已省略: 获取 {path} 的文件信息，共 {original_length} 字符]"

    # 未知工具 fallback
    args_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return f"[已省略: {name}({args_str}) 的结果，共 {original_length} 字符]"


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------

def _phase1_truncate_tool_results(
    messages: list[dict], mid_start: int, mid_end: int,
) -> list[dict]:
    """阶段 1: 将中间区 role=tool 的消息内容截断到 2000 字符。"""
    max_chars = 2000
    for i in range(mid_start, min(mid_end, len(messages))):
        msg = messages[i]
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            if len(content) > max_chars:
                msg["content"] = content[:max_chars] + f"\n...[内容已截断，原始长度 {len(content)} 字符]"
    return messages


def _phase2_summarize_tool_results(
    messages: list[dict],
    mid_start: int,
    mid_end: int,
    tool_call_index: dict[str, tuple[str, dict]],
) -> list[dict]:
    """阶段 2: 将中间区 role=tool 的消息替换为描述性摘要。"""
    for i in range(mid_start, min(mid_end, len(messages))):
        msg = messages[i]
        if msg.get("role") == "tool":
            content = msg.get("content", "")
            # 计算原始内容长度（去掉阶段1可能追加的截断提示）
            original_length = len(content)
            tool_call_id = msg.get("tool_call_id", "")
            msg["content"] = _summarize_tool_result(
                tool_call_id, original_length, tool_call_index,
            )
    return messages


def _identify_tool_call_groups(
    messages: list[dict], start: int, end: int,
) -> list[tuple[int, list[int]]]:
    """识别中间区的工具调用组: (assistant_idx, [tool_result_idx, ...])。

    从最老的开始排列。
    """
    groups: list[tuple[int, list[int]]] = []
    i = start
    while i < end:
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            assistant_idx = i
            tool_indices: list[int] = []
            j = i + 1
            while j < end and messages[j].get("role") == "tool":
                tool_indices.append(j)
                j += 1
            if tool_indices:
                groups.append((assistant_idx, tool_indices))
            i = j
        else:
            i += 1
    return groups


def _phase3_remove_tool_call_groups(
    messages: list[dict],
    mid_start: int,
    tail_count: int,
    budget: int,
) -> list[dict]:
    """阶段 3: 从最老的开始移除完整的工具调用组。"""
    while True:
        mid_end = len(messages) - tail_count
        if mid_end <= mid_start:
            break

        groups = _identify_tool_call_groups(messages, mid_start, mid_end)
        if not groups:
            break

        # 移除最老的一组
        assistant_idx, tool_indices = groups[0]
        indices_to_remove = sorted([assistant_idx] + tool_indices, reverse=True)
        for idx in indices_to_remove:
            messages.pop(idx)

        total = sum(estimate_message_tokens(m) for m in messages)
        if total <= budget:
            break

    return messages


def _phase4_truncate_diff(
    messages: list[dict], budget: int, current_total: int,
) -> list[dict]:
    """阶段 4: 如果 diff（第一条 user message）过大，截断它。"""
    if len(messages) < 2:
        return messages

    user_msg = messages[1]
    content = user_msg.get("content", "")
    diff_tokens = estimate_tokens(content)
    diff_budget = int(budget * 0.6)

    if diff_tokens <= diff_budget:
        return messages

    logger.warning("Diff 内容过大 (%d tokens)，已截断至 %d tokens", diff_tokens, diff_budget)

    # 按文件块分割 diff
    lines = content.split("\n")
    header_lines: list[str] = []
    file_blocks: list[list[str]] = []
    current_block: list[str] = []

    for line in lines:
        if line.startswith("### ") and current_block:
            file_blocks.append(current_block)
            current_block = [line]
        elif not file_blocks and not current_block and not line.startswith("### "):
            header_lines.append(line)
        else:
            current_block.append(line)
    if current_block:
        file_blocks.append(current_block)

    # 逐个文件块加入，直到达到预算
    target_chars = int(diff_budget * settings.LLM_CHARS_PER_TOKEN)
    kept_blocks: list[list[str]] = []
    current_chars = len("\n".join(header_lines))

    for block in file_blocks:
        block_text = "\n".join(block)
        if current_chars + len(block_text) > target_chars and kept_blocks:
            break
        kept_blocks.append(block)
        current_chars += len(block_text)

    total_files = len(file_blocks)
    kept_files = len(kept_blocks)

    result_parts = header_lines.copy()
    for block in kept_blocks:
        result_parts.extend(block)

    if kept_files < total_files:
        result_parts.append(
            f"\n[...diff 过长已截断，共 {total_files} 个文件，保留了前 {kept_files} 个...]"
        )

    user_msg["content"] = "\n".join(result_parts)
    return messages
