# DMarket Bot Installation Guide

## Prerequisites
- Docker Desktop ([Download here](https://www.docker.com/products/docker-desktop))

## Installation Steps

### Windows Users
1. Install Docker Desktop
2. Double-click the `install.bat` file
3. Follow the on-screen instructions

### Linux/Mac Users
1. Install Docker
2. Open terminal in the bot directory
3. Run: `chmod +x install.sh`
4. Run: `./install.sh`
5. Follow the on-screen instructions

## Accessing the Dashboard
- Open your web browser and go to: http://localhost:5000
- Default login credentials:
  - Username: admin
  - Password: changeme

## Important Security Notes
1. Change the default login credentials in the `.env` file
2. Keep your `.env` file secure and never share it

## Stopping the Bot
- Windows: Run `docker-compose down` in the bot directory
- Linux/Mac: Run `docker-compose down` in the bot directory

## Support
If you need assistance, please contact [Your Contact Information]