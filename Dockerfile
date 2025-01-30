FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt update && apt install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first to leverage Docker cache
COPY pyproject.toml .
COPY uv.lock .

# Install Python dependencies
RUN pip install --no-cache-dir uv && \
    uv pip install --system -r uv.lock

# Copy the application
COPY . .

# Create config directory
RUN mkdir -p config

# Expose the port the app runs on
EXPOSE 5000

# Command to run the application
CMD ["python", "dashboard/app.py"]