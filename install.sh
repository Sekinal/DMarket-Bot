#!/bin/bash

echo "Installing DMarket Bot..."

# Check if Docker is installed
if ! command -v docker &> /dev/null; then
    echo "Docker is not installed. Please install Docker first."
    exit 1
fi

# Create .env file if it doesn't exist
if [ ! -f .env ]; then
    echo "Creating .env file..."
    echo "DASHBOARD_USER=admin" > .env
    echo "DASHBOARD_PASSWORD=changeme" >> .env
    echo "SECRET_KEY=your-secret-key" >> .env
fi

# Create config directory if it doesn't exist
mkdir -p config

# Start the application
echo "Starting DMarket Bot..."
docker-compose up -d

echo
echo "Installation complete!"
echo "Access the dashboard at http://localhost:5000"
echo "Default login credentials:"
echo "Username: admin"
echo "Password: changeme"
echo
echo "Please change these credentials in the .env file!"