#!/bin/bash

# Deployment script for cybervilla-affiliates-api (no sudo required)
# Assumes directories already exist

set -e

echo "=== Deploying cybervilla-affiliates-api ==="

# Configuration
APP_DIR="/home/odooaffiliate/cybervilla-affiliates-api"
SERVICE_NAME="cybervilla-api"
PYTHON_VERSION="python3"

# Navigate to app directory
cd $APP_DIR

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating Python virtual environment..."
    $PYTHON_VERSION -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Run database migrations
echo "Running database migrations..."
flask db upgrade

# Create admin account
echo "Creating admin account..."
flask create-admin

# Stop existing PM2 process if running
pm2 stop $SERVICE_NAME 2>/dev/null || true
pm2 delete $SERVICE_NAME 2>/dev/null || true

# Start with PM2
echo "Starting application with PM2..."
pm2 start gunicorn --name $SERVICE_NAME -- \
    --bind 127.0.0.1:5000 \
    --workers 4 \
    --worker-class sync \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    --log-level info \
    wsgi:app

# Save PM2 configuration
pm2 save

echo "=== Backend deployment complete ==="
echo "API running on http://127.0.0.1:5000"
echo "Check status: pm2 status"
echo "View logs: pm2 logs $SERVICE_NAME"
