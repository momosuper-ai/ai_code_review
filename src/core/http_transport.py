"""Typed transport compatibility for supported OpenAI SDK HTTP backends.

Older SDKs (and explicitly supplied clients) use httpx; newer SDKs use httpx2.
Keep the accepted classes explicit: status-shaped objects are not evidence of
a received response, and starting to consume a stream does not prove EOF.
"""

import httpx

try:
    import httpx2
except ModuleNotFoundError as exc:
    if exc.name != "httpx2":
        raise
    httpx2 = None


_BACKENDS = (httpx,) if httpx2 is None else (httpx, httpx2)
RESPONSE_TYPES = tuple(backend.Response for backend in _BACKENDS)
_NOT_READ_ERRORS = tuple(backend.ResponseNotRead for backend in _BACKENDS)
TIMEOUT_PHASES = tuple(
    (getattr(backend, kind), phase)
    for backend in _BACKENDS
    for kind, phase in (("ConnectTimeout", "connect"), ("ReadTimeout", "read"),
                        ("WriteTimeout", "write"), ("PoolTimeout", "pool"))
)


def complete_response(response: object) -> bool:
    """Check cached completion without reading I/O, parsing or logging a body."""
    if (not isinstance(response, RESPONSE_TYPES) or not response.is_closed
            or not response.is_stream_consumed):
        return False
    try:
        # Both flags may be true after only the first chunk and aclose().
        # .content is a cache-only property; it raises when aread() did not
        # finish. Never call read/aread here to make an incomplete error valid.
        return isinstance(response.content, bytes)
    except _NOT_READ_ERRORS:
        return False
