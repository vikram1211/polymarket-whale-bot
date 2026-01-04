"""
Polymarket Whale Detector Bot
Monitors large trades from new accounts with high portfolio concentration.
Sends alerts via Telegram.
"""

import os
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Detection thresholds (adjustable)
MIN_TRADE_AMOUNT = int(os.getenv("MIN_TRADE_AMOUNT", 500))  # Minimum trade size in USD
MAX_ACCOUNT_AGE_DAYS = int(os.getenv("MAX_ACCOUNT_AGE_DAYS", 60))  # Account must be newer than this
MIN_CONCENTRATION = float(os.getenv("MIN_CONCENTRATION", 50))  # Min % of portfolio in single market
MAX_MARKETS_TRADED = int(os.getenv("MAX_MARKETS_TRADED", 10))  # Max markets they've traded
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 30))  # Seconds between checks

# API endpoints
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Cache to avoid redundant API calls and duplicate alerts
wallet_cache = {}  # wallet -> {profile, positions, last_updated}
seen_trades = set()  # Set of transaction hashes we've already processed


def send_telegram_message(message: str) -> bool:
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[ALERT] {message}")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        print(f"[ERROR] Failed to send Telegram message: {e}")
        return False


def get_large_trades(min_amount: int = 500, limit: int = 100) -> list:
    """Fetch recent large trades from Polymarket."""
    url = f"{DATA_API}/trades"
    params = {
        "filterType": "CASH",
        "filterAmount": min_amount,
        "limit": limit
    }

    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[ERROR] Failed to fetch trades: {e}")
        return []


def get_wallet_profile(wallet: str) -> dict | None:
    """Get wallet profile including creation date."""
    # Check cache first (valid for 1 hour)
    if wallet in wallet_cache:
        cached = wallet_cache[wallet]
        if time.time() - cached.get("last_updated", 0) < 3600:
            return cached.get("profile")

    url = f"{GAMMA_API}/public-profile"
    params = {"address": wallet}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            profile = response.json()
            # Update cache
            if wallet not in wallet_cache:
                wallet_cache[wallet] = {}
            wallet_cache[wallet]["profile"] = profile
            wallet_cache[wallet]["last_updated"] = time.time()
            return profile
        return None
    except Exception as e:
        print(f"[ERROR] Failed to fetch profile for {wallet}: {e}")
        return None


def get_wallet_positions(wallet: str) -> list:
    """Get wallet's current positions."""
    # Check cache first
    if wallet in wallet_cache:
        cached = wallet_cache[wallet]
        if time.time() - cached.get("positions_updated", 0) < 300:  # 5 min cache
            return cached.get("positions", [])

    url = f"{DATA_API}/positions"
    params = {"user": wallet, "limit": 100}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            positions = response.json()
            # Update cache
            if wallet not in wallet_cache:
                wallet_cache[wallet] = {}
            wallet_cache[wallet]["positions"] = positions
            wallet_cache[wallet]["positions_updated"] = time.time()
            return positions
        return []
    except Exception as e:
        print(f"[ERROR] Failed to fetch positions for {wallet}: {e}")
        return []


def get_wallet_stats(wallet: str) -> dict | None:
    """Get aggregated stats for a wallet."""
    url = f"{DATA_API}/traded"
    params = {"user": wallet}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        return None
    except Exception as e:
        print(f"[ERROR] Failed to fetch stats for {wallet}: {e}")
        return None


def calculate_account_age(profile: dict) -> int:
    """Calculate account age in days."""
    created_at = profile.get("createdAt")
    if not created_at:
        return 9999  # Unknown age, assume old

    try:
        created_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age = (now - created_date).days
        return age
    except Exception:
        return 9999


def calculate_portfolio_concentration(positions: list, target_market: str) -> float:
    """Calculate what % of portfolio is in the target market."""
    if not positions:
        return 100.0  # If no other positions, this trade is 100% concentration

    total_value = 0
    target_value = 0

    for pos in positions:
        value = abs(float(pos.get("currentValue", 0)))
        total_value += value

        # Check if this position is in the target market
        condition_id = pos.get("conditionId", "")
        if condition_id == target_market:
            target_value += value

    if total_value == 0:
        return 100.0

    return (target_value / total_value) * 100


def analyze_trade(trade: dict) -> dict | None:
    """
    Analyze a trade to determine if it's from a potential informed trader.
    Returns analysis dict if suspicious, None otherwise.
    """
    wallet = trade.get("proxyWallet")
    if not wallet:
        return None

    # Get trade details
    trade_amount = float(trade.get("size", 0)) * float(trade.get("price", 0))
    market_id = trade.get("conditionId", "")

    # Get wallet profile
    profile = get_wallet_profile(wallet)
    if not profile:
        return None

    # Calculate account age
    account_age = calculate_account_age(profile)

    # Skip if account is too old
    if account_age > MAX_ACCOUNT_AGE_DAYS:
        return None

    # Get positions and calculate concentration
    positions = get_wallet_positions(wallet)
    concentration = calculate_portfolio_concentration(positions, market_id)

    # Get total markets traded
    stats = get_wallet_stats(wallet)
    markets_traded = stats.get("total", 0) if stats else 0

    # Check if it meets our criteria
    if concentration < MIN_CONCENTRATION and markets_traded > MAX_MARKETS_TRADED:
        return None

    # Calculate a "whale score" (higher = more suspicious)
    whale_score = 0
    whale_score += min(30, (MAX_ACCOUNT_AGE_DAYS - account_age) / 2)  # Newer = higher score
    whale_score += min(30, concentration / 3)  # Higher concentration = higher score
    whale_score += min(20, trade_amount / 500)  # Larger trades = higher score
    whale_score += min(20, (MAX_MARKETS_TRADED - markets_traded) * 2) if markets_traded < MAX_MARKETS_TRADED else 0

    return {
        "wallet": wallet,
        "username": profile.get("pseudonym") or profile.get("name") or "Unknown",
        "account_age_days": account_age,
        "trade": {
            "market": trade.get("title", "Unknown Market"),
            "outcome": trade.get("outcome", "Unknown"),
            "side": trade.get("side", "Unknown"),
            "amount": round(trade_amount, 2),
            "price": float(trade.get("price", 0)),
            "size": float(trade.get("size", 0))
        },
        "portfolio": {
            "concentration": round(concentration, 1),
            "markets_traded": markets_traded,
            "total_positions": len(positions)
        },
        "whale_score": round(whale_score, 1),
        "tx_hash": trade.get("transactionHash", "")
    }


def format_alert_message(analysis: dict) -> str:
    """Format analysis into a Telegram alert message."""
    trade = analysis["trade"]
    portfolio = analysis["portfolio"]

    # Determine emoji based on whale score
    if analysis["whale_score"] >= 70:
        emoji = "🚨🐋"
        urgency = "HIGH"
    elif analysis["whale_score"] >= 50:
        emoji = "⚠️🐋"
        urgency = "MEDIUM"
    else:
        emoji = "📊"
        urgency = "LOW"

    # Calculate implied probability
    if trade["side"] == "BUY":
        implied_prob = trade["price"] * 100
    else:
        implied_prob = (1 - trade["price"]) * 100

    message = f"""
{emoji} <b>WHALE ALERT</b> [{urgency}]

<b>Market:</b> {trade['market']}
<b>Bet:</b> {trade['outcome']} ({trade['side']})
<b>Amount:</b> ${trade['amount']:,.2f}
<b>Price:</b> {trade['price']:.2f} ({implied_prob:.0f}% implied)

<b>Trader Profile:</b>
• Username: {analysis['username']}
• Account Age: {analysis['account_age_days']} days
• Portfolio Focus: {portfolio['concentration']:.0f}% in this market
• Total Markets: {portfolio['markets_traded']}

<b>Whale Score:</b> {analysis['whale_score']}/100

🔗 <a href="https://polygonscan.com/tx/{analysis['tx_hash']}">View Transaction</a>
"""
    return message.strip()


def run_detector():
    """Main detection loop."""
    print("=" * 60)
    print("🐋 Polymarket Whale Detector Starting...")
    print("=" * 60)
    print(f"Settings:")
    print(f"  • Min Trade Amount: ${MIN_TRADE_AMOUNT}")
    print(f"  • Max Account Age: {MAX_ACCOUNT_AGE_DAYS} days")
    print(f"  • Min Concentration: {MIN_CONCENTRATION}%")
    print(f"  • Max Markets Traded: {MAX_MARKETS_TRADED}")
    print(f"  • Poll Interval: {POLL_INTERVAL}s")
    print("=" * 60)

    # Send startup message
    send_telegram_message("🐋 Whale Detector Bot Started!\n\nMonitoring Polymarket for suspicious large trades...")

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching trades...")

            # Fetch large trades
            trades = get_large_trades(min_amount=MIN_TRADE_AMOUNT)
            new_trades = 0
            alerts_sent = 0

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")

                # Skip if we've already processed this trade
                if tx_hash in seen_trades:
                    continue

                seen_trades.add(tx_hash)
                new_trades += 1

                # Keep seen_trades from growing indefinitely
                if len(seen_trades) > 10000:
                    seen_trades.clear()

                # Analyze the trade
                analysis = analyze_trade(trade)

                if analysis and analysis["whale_score"] >= 40:
                    # Format and send alert
                    message = format_alert_message(analysis)
                    print(f"\n{'='*40}")
                    print(f"🚨 WHALE DETECTED!")
                    print(f"Market: {analysis['trade']['market']}")
                    print(f"Amount: ${analysis['trade']['amount']}")
                    print(f"Account Age: {analysis['account_age_days']} days")
                    print(f"Whale Score: {analysis['whale_score']}")
                    print(f"{'='*40}")

                    if send_telegram_message(message):
                        alerts_sent += 1

            print(f"Processed {new_trades} new trades, sent {alerts_sent} alerts")

            # Wait before next poll
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("\n\n🛑 Stopping whale detector...")
            send_telegram_message("🛑 Whale Detector Bot Stopped")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(60)  # Wait longer on error


if __name__ == "__main__":
    run_detector()
