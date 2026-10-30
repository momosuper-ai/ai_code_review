"""A bounded producer contract for lossy overview prose, never source detail."""
import copy
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.project_brain.schema import Compilation


class SummaryProseTooLarge(ValueError):
    """Fixed local diagnostic; generated text must never enter the exception."""

    def __init__(self) -> None:
        super().__init__('summary_prose_too_large')


class SummaryFieldsTooLarge(ValueError):
    """A fixed error; field names and generated strings never enter diagnostics."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        self.fields = fields
        super().__init__("summary_field_budget_exceeded")


@dataclass(frozen=True)
class SummaryFieldBudget:
    """Bound authored characters, including worst-case JSON escaping, before generation."""

    level: int
    limit_bytes: int
    title_chars: int
    text_count: int
    text_chars: int
    unknown_count: int
    unknown_chars: int
    path_chars: int

    def schema(self, original: dict) -> dict:
        """Apply the same field limits to the prompt and structured-output definition."""
        schema = copy.deepcopy(original)
        properties = schema["properties"]

        def string(field: dict, maximum: int) -> None:
            field["maxLength"] = maximum
            field["description"] = f"最多{maximum}个Unicode字符；完整重写，不截断字符串。"

        def array(field: dict, count: int, maximum: int) -> None:
            field["maxItems"] = count
            field["description"] = f"最多{count}条，每条最多{maximum}个Unicode字符；合并重复内容。"
            string(field["items"], maximum)

        string(properties["title"], self.title_chars)
        slot = schema["$defs"]["SlotText"]["properties"]
        array(slot["texts"], self.text_count, self.text_chars)
        array(slot["unknowns"], self.unknown_count, self.unknown_chars)
        array(properties["unknowns"], self.unknown_count, self.unknown_chars)
        if self.path_chars:
            for branch in properties["document_path"]["anyOf"]:
                if branch["type"] == "string":
                    string(branch, self.path_chars)
        else:
            properties["document_path"] = {"type": "null", "description": "当前概览预算只允许null。"}
        return schema

    def require_response(self, response: dict) -> None:
        """Check the already schema-validated response without trusting provider constraints."""
        slot = response["slots"]["S1"]
        checks = {"title_chars": len(response["title"]) > self.title_chars,
                  "text_count": len(slot["texts"]) > self.text_count,
                  "text_chars": any(len(value) > self.text_chars for value in slot["texts"]),
                  "slot_unknown_count": len(slot["unknowns"]) > self.unknown_count,
                  "top_unknown_count": len(response["unknowns"]) > self.unknown_count,
                  "unknown_chars": any(len(value) > self.unknown_chars for value in [*slot["unknowns"], *response["unknowns"]]),
                  "path_chars": response["document_path"] is not None and (
                      not self.path_chars or len(response["document_path"]) > self.path_chars)}
        exceeded = tuple(name for name, failed in checks.items() if failed)
        if exceeded:
            raise SummaryFieldsTooLarge(exceeded)


def summary_field_budget(packet_limit: int, level: int = 0) -> SummaryFieldBudget:
    """Allocate an overview budget; tighter retries never enlarge any field."""
    if type(level) is not int or not 0 <= level <= 2:
        raise ValueError("summary_budget_level_invalid")
    limit = summary_prose_limit(packet_limit)
    text_count, unknown_count = (3, 1) if limit >= 1024 else (1, 0)
    # Every JSON string character needs at most six UTF-8 bytes (control-char
    # escaping); astral characters need four. Null is larger than an empty path.
    empty = {"title": "", "texts": [""] * text_count, "unknowns": [""] * (unknown_count * 2),
             "document_path": None}
    overhead = len(json.dumps(empty, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    characters = (limit - overhead) // 6
    title = min(40, max(1, characters // 12))
    path = min(160, characters // 4) if characters >= 256 else 0
    unknown = characters // 12 if unknown_count else 0
    text = (characters - title - path - unknown_count * 2 * unknown) // text_count
    # Keep path/title stable and spend the correction on the generated prose.
    factor = (4, 3, 2)[level]
    return SummaryFieldBudget(level, limit, title, text_count, max(1, text * factor // 4),
                              unknown_count, unknown * factor // 4, path)


def summary_prose_limit(packet_limit: int) -> int:
    # Leave room for several complete child overviews, canonical wire metadata,
    # and the retained coverage contract. This does not increase packet limits.
    if type(packet_limit) is not int or packet_limit < 1:
        raise ValueError('summary_packet_limit_invalid')
    return max(128, min(8192, packet_limit // 8))


def summary_prose_size(compilation) -> int:
    # Machine-assigned evidence/scope IDs can be large; they are authenticated
    # separately and are not prose authored by the model. Every authored string
    # is counted together, including repeated text and JSON escaping.
    prose = {'title': compilation.title, 'texts': [claim.text for claim in compilation.claims],
             'unknowns': list(compilation.unknowns), 'document_path': compilation.document_path}
    return len(json.dumps(prose,ensure_ascii=False,sort_keys=True,separators=(',', ':')).encode('utf-8'))


def require_summary_prose(compilation, packet_limit: int) -> None:
    if summary_prose_size(compilation) > summary_prose_limit(packet_limit):
        raise SummaryProseTooLarge()


def summary_prose_instruction(packet_limit: int, level: int = 0) -> str:
    budget = summary_field_budget(packet_limit, level)
    return (f'\n概览收敛约束：summary_budget_level={level}。模型新增title、全部texts、全部unknowns和document_path合计，'
            f'按JSON序列化的UTF-8字节计不得超过{budget.limit_bytes}。'
            f'title最多{budget.title_chars}字；S1.texts为1至{budget.text_count}条，每条最多{budget.text_chars}字；'
            f'S1.unknowns及顶层unknowns各最多{budget.unknown_count}条，每条最多{budget.unknown_chars}字；'
            f'document_path建议null，非空时最多{budget.path_chars}字。标点也计入字符数。'
            '概览只综合关键职责、边界和重要未知，不逐条重述子概览。'
            '完整结论、来源和所有历史未知项由服务单独保留，不得声称它们已解决。'
            '必须返回完整Schema，禁止截断字符串、删除固定槽位或生成来源标识。')


def summary_size_feedback(compilation: "Compilation", packet_limit: int) -> str:
    """Return only bounded field categories and numeric sizes, never model text."""
    def size(value: object) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    return (f"actual_bytes={summary_prose_size(compilation)} limit_bytes={summary_prose_limit(packet_limit)} "
            f"title_bytes={size(compilation.title)} texts_count={len(compilation.claims)} "
            f"texts_bytes={size([claim.text for claim in compilation.claims])} "
            f"unknowns_count={len(compilation.unknowns)} unknowns_bytes={size(compilation.unknowns)} "
            f"path_bytes={size(compilation.document_path)}。按更新后的字段条数和字符上限完整重写。")
