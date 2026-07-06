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
RUN playwright install --with-deps firefox

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
