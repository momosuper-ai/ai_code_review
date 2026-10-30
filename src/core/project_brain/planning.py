"""Explain semantic workload without confusing estimates with provider billing."""

from src.config import settings
from src.core.context_manager import estimate_tokens
from src.core.project_brain.compiler import compilation_transport, encoded, semantic_request, token_bound
from src.core.project_brain.context import compact_packet


def compilation_plan(packets: list[dict]) -> dict:
    """Report complete transport size and approximate input, never trim facts.

    Reservation uses conservative UTF-8 units; billing uses provider usage.
    This estimate excludes output, retries, privacy and project summarization.
    It therefore cannot assert that an entire project fits its hard budget.
    """
    canonical_bytes = wire_bytes = input_estimate = reservation_envelope = 0
    for packet in packets:
        canonical_bytes += len(encoded(packet).encode())
        context = compact_packet(packet)
        messages, kwargs, _ = compilation_transport(packet, context)
        wire_bytes += len(messages[1]["content"].encode())
        request = encoded(semantic_request(messages, kwargs))
        input_estimate += estimate_tokens(request)
        reservation_envelope += token_bound(request) + settings.PROJECT_BRAIN_LLM_MAX_TOKENS
    return {"semantic_packets": len(packets), "canonical_bytes": canonical_bytes,
            "wire_bytes": wire_bytes, "estimated_semantic_input_tokens": input_estimate,
            "reservation_envelope": reservation_envelope,
            "estimate_method": "characters/LLM_CHARS_PER_TOKEN; not provider tokenizer",
            "excluded_costs": ["semantic_output", "privacy", "retries", "project_summary", "assembly"],
            "budget_tokens": settings.PROJECT_BRAIN_MAX_BUILD_TOKENS,
            "quality_policy": "all facts, relations, candidate states and source evidence retained"}
