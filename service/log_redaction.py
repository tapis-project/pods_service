"""Keep credentials out of logs (R1).

Two layers:
- scrub_headers()/scrub_cookies() for call sites that WANT to log request
  context — values of credential-carrying keys are masked, names kept.
- JWTRedactionFilter as a process-wide backstop: any 'eyJ…' JWT-shaped blob
  that still reaches a handler is masked before it hits disk/stdout.
"""
import logging
import re

TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]{16,}(?:\.[A-Za-z0-9_\-]+){0,2}")
_SENSITIVE_HEADERS = {"x-tapis-token", "authorization", "cookie", "set-cookie", "x-tapis-access-token"}


def scrub_headers(headers) -> dict:
    """Header map safe to log: credential-carrying values masked, names kept."""
    out = {}
    for k, v in dict(headers).items():
        if k.lower() in _SENSITIVE_HEADERS or (isinstance(v, str) and v.startswith("eyJ")):
            v = "***"
        out[k] = v
    return out


def scrub_cookies(cookies) -> dict:
    """Cookie names only — every cookie value here is some kind of secret
    (JWTs, gate session secrets), so none of them belong in a log line."""
    return {k: "***" for k in dict(cookies)}


class JWTRedactionFilter(logging.Filter):
    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        if "eyJ" in msg:
            record.msg = TOKEN_RE.sub("eyJ***REDACTED***", msg)
            record.args = ()
        return True


def install_redaction_filter():
    """Attach the backstop to every handler of every logger created so far.
    Called at the end of process setup (api.py / health entrypoints), after all
    module imports have run get_logger() — tapisservice attaches handlers
    per-logger, so root-only filtering would miss them."""
    f = JWTRedactionFilter()
    loggers = [logging.getLogger()] + [
        l for l in logging.Logger.manager.loggerDict.values() if isinstance(l, logging.Logger)
    ]
    for lg in loggers:
        for h in lg.handlers:
            if not any(isinstance(x, JWTRedactionFilter) for x in h.filters):
                h.addFilter(f)
