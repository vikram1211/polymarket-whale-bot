# Polymarket Whale Detector

Monitors Polymarket for large trades from new accounts with high portfolio concentration. Sends alerts via Telegram.

## Detection Criteria

- Trade amount > $500 (configurable)
- Account age < 60 days (configurable)
- Portfolio concentration > 50% in single market (configurable)
- Total markets traded < 10 (configurable)

## Setup

### 1. Create Telegram Bot

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Choose a name: `Polymarket Whale Alerts`
4. Choose a username: `polywhale_yourname_bot`
5. Save the API token (looks like `123456789:ABCdefGHI...`)

### 2. Get Your Chat ID

1. Start a chat with your new bot and send any message (like "hello")
2. Open this URL in browser (replace YOUR_TOKEN):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
3. Find `"chat":{"id":XXXXXXXXX}` - that number is your Chat ID

### 3. Deploy to Railway (Free)

1. Go to [railway.app](https://railway.app) and sign up with GitHub
2. Click "New Project" > "Deploy from GitHub repo"
3. Connect your GitHub and select this repository
4. Go to "Variables" tab and add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
5. Railway will auto-deploy. Check "Deployments" tab for logs.

### Optional: Adjust Detection Settings

Add these variables in Railway to customize:

| Variable | Default | Description |
|----------|---------|-------------|
| `MIN_TRADE_AMOUNT` | 500 | Minimum trade size in USD |
| `MAX_ACCOUNT_AGE_DAYS` | 60 | Max account age to flag |
| `MIN_CONCENTRATION` | 50 | Min % of portfolio in one market |
| `MAX_MARKETS_TRADED` | 10 | Max markets for account to qualify |
| `POLL_INTERVAL` | 30 | Seconds between API checks |

## Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Copy env file and fill in your tokens
cp .env.example .env

# Run
python whale_detector.py
```

## Alert Format

```
WHALE ALERT [HIGH]

Market: Will X happen by Y?
Bet: Yes (BUY)
Amount: $2,500.00
Price: 0.35 (35% implied)

Trader Profile:
- Username: Frozen-Technician
- Account Age: 8 days
- Portfolio Focus: 94% in this market
- Total Markets: 2

Whale Score: 78/100
```

## How Whale Score Works

- Newer account = higher score (max 30 points)
- Higher concentration = higher score (max 30 points)
- Larger trade = higher score (max 20 points)
- Fewer markets traded = higher score (max 20 points)

Alerts are sent when score >= 40.
