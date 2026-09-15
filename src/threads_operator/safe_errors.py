"""Small deterministic helpers for credential-safe error reporting."""
from __future__ import annotations

import re
from collections.abc import Iterable

_QUERY_SECRET_RE = re.compile(
    r"(?i)\b(access_token|apikey|api_key)=([^&\s'\";,]+)"
)
_AUTH_RE = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*(?:bearer\s+)?)([^\s,'\";]+)"
)


def redact_error(value: object, secrets: Iterable[str | None] = ()) -> str:
    """Return a log-safe error string without known credential values."""
    text = str(value)
    known = sorted(
        {str(secret) for secret in secrets if secret and str(secret)},
        key=len,
        reverse=True,
    )
    for secret in known:
        text = text.replace(secret, "[REDACTED]")
    text = _QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    text = _AUTH_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    return text
