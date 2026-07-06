# Dockerfile for Render deployment
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Set timezone explicitly
ENV TZ=Asia/Taipei
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# Set working directory
WORKDIR /app

# Copy requirements first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Firefox browser + system dependencies
# Note: apt-get update is needed because we cleaned the cache above.
# || true ensures the build doesn't fail if Playwright can't install
# (product monitor will gracefully skip if Playwright is unavailable)
RUN apt-get update && playwright install --with-deps firefox || echo "WARN: Playwright install failed, product monitor will be disabled"

# Copy all application files
COPY . .

# Create necessary directories
RUN mkdir -p .workbuddy/memory .workbuddy/automation-backups logs

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Taipei
ENV PORT=10000

# Expose port (Render uses PORT env var)
EXPOSE 10000

# Generate config.json from environment variables
RUN python scripts/generate_config.py || true

# Start the application
CMD ["bash", "scripts/render_start.sh"]
