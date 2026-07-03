# Image for the audit bridge (default) and the raw MCP server.
#   default CMD  -> uvicorn bridge:app  (the browser-facing audit bridge, :8100)
#   raw MCP mode -> override: fastmcp run pods_mcp.py --transport http --port 8000
#
# The OpenAPI spec is NOT baked in — mount it (compose does this) or set PODS_SPEC
# to a URL, so a spec refresh doesn't need a rebuild.
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt bridge-requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r bridge-requirements.txt
COPY *.py ./

ENV PYTHONUNBUFFERED=1
EXPOSE 8100

CMD ["uvicorn", "bridge:app", "--host", "0.0.0.0", "--port", "8100"]
