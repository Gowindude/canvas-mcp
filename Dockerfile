FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the package — never the .env file (see .dockerignore)
COPY canvas_mcp/ ./canvas_mcp/

# PORT is injected by Render at runtime; EXPOSE is documentation only.
EXPOSE 8000

CMD ["python", "-m", "canvas_mcp.server"]
