FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
ENV MCP_TRANSPORT=http PORT=8000
EXPOSE 8000
CMD ["odoo-mcp"]
