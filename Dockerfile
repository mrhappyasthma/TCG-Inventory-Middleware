FROM python:3.11-slim

WORKDIR /app

# Install system utilities if needed
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency specifications
COPY requirements.txt .
COPY tcg_engine/ ./tcg_engine/

# Install python dependencies and core tcg-engine library
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app/ ./app/

# Environment defaults
EXPOSE 8080
VOLUME /data

ENV DATABASE_URL="/data/inventory.db"
ENV USER_DATABASE_URL="/data/users.db"
ENV PORT=8080
ENV AUTH_METHOD="local"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
