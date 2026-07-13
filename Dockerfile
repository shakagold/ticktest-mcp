FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the package
COPY . .
RUN pip install --no-cache-dir -e .

# Expose the port
EXPOSE 8000

# Run the HTTP MCP server
CMD ["python", "-m", "ticktest_mcp.server_http"]
