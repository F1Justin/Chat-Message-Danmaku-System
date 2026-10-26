"""Redact credential query parameters before Uvicorn formats request logs."""
import logging
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def redact_url(value):
    if not isinstance(value, str) or "?" not in value:
        return value
    parts = urlsplit(value)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(k == "token" for k, _ in pairs):
        return value
    query = urlencode([(k, "[redacted]" if k == "token" else v) for k, v in pairs])
    return urlunsplit(parts._replace(query=query))


class RedactTokens(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(redact_url(arg) for arg in record.args)
        return True


def install_redaction():
    for name in ("uvicorn.access", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in logger.filters):
            logger.addFilter(RedactTokens())
