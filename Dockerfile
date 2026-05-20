# trading_agent_mcp 的容器 image(repo root,Zeabur 自動偵測)。
# 跑 FastMCP server,對 Claude.ai / Desktop 開放 Streamable HTTP transport。
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# 預設 port(Zeabur 會以 $PORT 蓋過,settings.py 會讀 PORT env)。
ENV PORT=8000
EXPOSE 8000

CMD ["python", "-m", "trading_agent_mcp"]
