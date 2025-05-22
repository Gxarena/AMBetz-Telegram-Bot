# Telegram Bot with FastAPI and MongoDB

A simple Telegram bot built with FastAPI and MongoDB.

## Features

- FastAPI backend with webhook endpoint for Telegram bot
- MongoDB integration for storing user data
- Telegram bot messaging functionality
- Environment variable configuration via dotenv
- Scheduled tasks to manage expired subscriptions
- Automatic removal of users with expired subscriptions from groups

## Setup

### Prerequisites

- Python 3.8+
- MongoDB instance
- Telegram Bot token (from @BotFather)

### Installation

1. Clone the repository:
   ```
   git clone <repository-url>
   cd <repository-directory>
   ```

2. Create and activate a virtual environment:
   ```
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

4. Create a `.env` file in the project root with the following variables:
   ```
   # Telegram Bot Configuration
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
   WEBHOOK_URL=https://your-domain.com
   VIP_CHAT_ID=-1001234567890  # ID of your VIP group

   # MongoDB Configuration
   MONGO_URI=mongodb://localhost:27017
   DB_NAME=telegram_bot_db
   ```

### Environment Variables

The application uses the following environment variables:

| Variable | Description | Required | Default |
|----------|-------------|----------|---------|
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token from BotFather | Yes | None |
| `WEBHOOK_URL` | Public URL for webhook (e.g., https://your-domain.com) | Yes | None |
| `VIP_CHAT_ID` | ID of the VIP Telegram group | Yes | None |
| `MONGO_URI` | MongoDB connection string | No | mongodb://localhost:27017 |
| `DB_NAME` | MongoDB database name | No | telegram_bot_db |

### Running the application

```
python main.py
```

The application will start at `http://localhost:8000`.

## Development

For local development without a public webhook URL, you can use tools like ngrok:

```
ngrok http 8000
```

Then update your `.env` file with the ngrok URL:

```
WEBHOOK_URL=https://your-ngrok-url.ngrok.io
```

## API Endpoints

- `/webhook` - Telegram bot webhook endpoint
- `/health` - Health check endpoint

## Bot Commands

- `/start` - Start the bot
- `/help` - Show help message
- `/status` - Check subscription status

## Scheduled Tasks

The bot runs the following scheduled tasks:

- **Check Expired Subscriptions** - Runs every 24 hours to find and manage expired subscriptions
  - Updates subscription status to "expired" in the database
  - Removes users with expired subscriptions from the VIP group
  - Notifies users that their subscription has expired

## Project Structure

- `main.py` - FastAPI application setup and main entry point
- `bot.py` - Telegram bot functionality
- `db.py` - MongoDB connection and database operations 