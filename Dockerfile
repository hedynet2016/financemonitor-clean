# Dockerfile for Render deployment
FROM python:3.13-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Playwright browsers (if needed)
# RUN pip install playwright && playwright install chromium

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application files
COPY . .

# Create necessary directories
RUN mkdir -p .workbuddy/memory .workbuddy/automation-backups logs

# Expose port (Render will use this)
EXPOSE 8000

# Generate config.json from environment variables
RUN python scripts/generate_config.py || true

# Start the application
CMD ["bash", "scripts/render_start.sh"]
