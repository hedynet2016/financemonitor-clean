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
# Split into steps so build logs show exactly what failed
# Step 1: install system libraries required by Firefox
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 libasound2 libdbus-glib-1-2 \
    libx11-xcb1 libxcomposite1 libxdamage1 \
    libxrandr2 libxss1 libxcursor1 libxinerama1 \
    libpangocairo-1.0-0 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libgbm1 libnspr4 libnss3 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Step 2: install Firefox browser binary
RUN playwright install firefox || echo "WARN: Playwright Firefox install failed, product monitor will be disabled"

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
