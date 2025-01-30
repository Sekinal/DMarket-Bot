@echo off
echo Installing DMarket Bot...

REM Check if Docker is installed
docker --version >nul 2>&1
if errorlevel 1 (
    echo Docker is not installed. Please install Docker Desktop first.
    echo Download from: https://www.docker.com/products/docker-desktop
    pause
    exit
)

REM Create .env file if it doesn't exist
if not exist .env (
    echo Creating .env file...
    echo DASHBOARD_USER=admin> .env
    echo DASHBOARD_PASSWORD=changeme>> .env
    echo SECRET_KEY=your-secret-key>> .env
)

REM Create config directory if it doesn't exist
if not exist config mkdir config

REM Start the application
echo Starting DMarket Bot...
docker-compose up -d

echo.
echo Installation complete!
echo Access the dashboard at http://localhost:5000
echo Default login credentials:
echo Username: admin
echo Password: changeme
echo.
echo Please change these credentials in the .env file!
pause