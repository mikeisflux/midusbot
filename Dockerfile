FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Persistent volumes for logs and learned data
VOLUME ["/app/logs", "/app/data"]

CMD ["python", "main.py", "--no-dashboard"]
