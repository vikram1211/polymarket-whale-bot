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
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Debug: Print what we loaded (masked for security)
def mask_token(token):
    if not token or len(token) < 10:
        return f"EMPTY or TOO SHORT (len={len(token) if token else 0})"
    return f"{token[:8]}...{token[-4:]} (len={len(token)})"

print(f"[CONFIG] TELEGRAM_BOT_TOKEN: {mask_token(TELEGRAM_BOT_TOKEN)}")
print(f"[CONFIG] TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")

# Detection thresholds (adjustable)
MIN_TRADE_AMOUNT = int(os.getenv("MIN_TRADE_AMOUNT", 500))  # Minimum trade size in USD
MAX_ACCOUNT_AGE_DAYS = int(os.getenv("MAX_ACCOUNT_AGE_DAYS", 60))  # Account must be newer than this
MIN_CONCENTRATION = float(os.getenv("MIN_CONCENTRATION", 50))  # Min % of portfolio in single market
MAX_MARKETS_TRADED = int(os.getenv("MAX_MARKETS_TRADED", 10))  # Max markets they've traded
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", 30))  # Seconds between checks
MIN_TRADES_ON_MARKET = int(os.getenv("MIN_TRADES_ON_MARKET", 3))  # Min trades on same market
MIN_TOTAL_AMOUNT_ON_MARKET = int(os.getenv("MIN_TOTAL_AMOUNT_ON_MARKET", 5000))  # Min total $ on same market

# API endpoints
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Excluded market categories (tag IDs from Polymarket)
# Tag 1 = Sports, Tag 235 = Bitcoin/Crypto
EXCLUDED_TAG_IDS = [int(x) for x in os.getenv("EXCLUDED_TAG_IDS", "1,235").split(",")]
EXCLUDED_CACHE_TTL = int(os.getenv("EXCLUDED_CACHE_TTL", 3600))  # Refresh excluded markets every hour

# Cache to avoid redundant API calls and duplicate alerts
wallet_cache = {}  # wallet -> {profile, positions, last_updated}
seen_trades = set()  # Set of transaction hashes we've already processed
excluded_markets_cache = {"condition_ids": set(), "last_updated": 0}  # Excluded market condition IDs


def send_telegram_message(message: str) -> bool:
    """Send a message via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM ERROR] Missing credentials!")
        print(f"  BOT_TOKEN set: {bool(TELEGRAM_BOT_TOKEN)}")
        print(f"  CHAT_ID set: {bool(TELEGRAM_CHAT_ID)}")
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
        if response.status_code == 200:
            print(f"[TELEGRAM] Message sent successfully!")
            return True
        else:
            print(f"[TELEGRAM ERROR] Status: {response.status_code}")
            print(f"[TELEGRAM ERROR] Response: {response.text}")
            return False
    except Exception as e:
        print(f"[TELEGRAM ERROR] Exception: {e}")
        return False


def get_markets_by_tag(tag_id: int) -> set:
    """Fetch all market condition IDs for a given tag."""
    condition_ids = set()
    offset = 0
    limit = 100

    while True:
        url = f"{GAMMA_API}/markets"
        params = {
            "tag_id": tag_id,
            "limit": limit,
            "offset": offset,
            "closed": "false"  # Only active markets
        }

        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == 200:
                markets = response.json()
                if not markets:
                    break

                for market in markets:
                    cond_id = market.get("conditionId")
                    if cond_id:
                        condition_ids.add(cond_id)

                if len(markets) < limit:
                    break
                offset += limit
            else:
                print(f"[ERROR] Failed to fetch markets for tag {tag_id}: {response.status_code}")
                break
        except Exception as e:
            print(f"[ERROR] Failed to fetch markets for tag {tag_id}: {e}")
            break

    return condition_ids


def refresh_excluded_markets() -> None:
    """Refresh the cache of excluded market condition IDs."""
    global excluded_markets_cache

    # Check if cache is still valid
    if time.time() - excluded_markets_cache["last_updated"] < EXCLUDED_CACHE_TTL:
        return

    print(f"[INFO] Refreshing excluded markets cache for tags: {EXCLUDED_TAG_IDS}")
    all_excluded = set()

    for tag_id in EXCLUDED_TAG_IDS:
        condition_ids = get_markets_by_tag(tag_id)
        print(f"[INFO] Found {len(condition_ids)} markets for tag {tag_id}")
        all_excluded.update(condition_ids)

    excluded_markets_cache["condition_ids"] = all_excluded
    excluded_markets_cache["last_updated"] = time.time()
    print(f"[INFO] Total excluded markets: {len(all_excluded)}")


def is_excluded_market(condition_id: str) -> bool:
    """Check if a market is in the excluded categories."""
    return condition_id in excluded_markets_cache["condition_ids"]


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


def get_wallet_trades_on_market(wallet: str, market_id: str) -> dict:
    """
    Get wallet's trade history on a specific market.
    Returns: {"trade_count": int, "total_amount": float, "net_amount": float, "dominant_side": str}
    """
    url = f"{DATA_API}/trades"
    params = {
        "user": wallet,
        "market": market_id,
        "limit": 100
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            trades = response.json()

            trade_count = len(trades)
            total_buy_amount = 0
            total_sell_amount = 0

            for t in trades:
                size = float(t.get("size", 0))
                price = float(t.get("price", 0))
                amount = size * price

                if t.get("side") == "BUY":
                    total_buy_amount += amount
                else:
                    total_sell_amount += amount

            # Net position (buys - sells)
            net_amount = total_buy_amount - total_sell_amount
            dominant_side = "BUY" if net_amount > 0 else "SELL"

            return {
                "trade_count": trade_count,
                "total_amount": total_buy_amount + total_sell_amount,
                "net_amount": abs(net_amount),
                "dominant_side": dominant_side,
                "buy_amount": total_buy_amount,
                "sell_amount": total_sell_amount
            }
        return {"trade_count": 0, "total_amount": 0, "net_amount": 0, "dominant_side": "UNKNOWN", "buy_amount": 0, "sell_amount": 0}
    except Exception as e:
        print(f"[ERROR] Failed to fetch trades for {wallet} on market: {e}")
        return {"trade_count": 0, "total_amount": 0, "net_amount": 0, "dominant_side": "UNKNOWN", "buy_amount": 0, "sell_amount": 0}


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
    trade_price = float(trade.get("price", 0))
    market_id = trade.get("conditionId", "")

    # Skip trades with >50% implied probability (focus on longshots)
    if trade_price > 0.5:
        return None

    # Get wallet profile
    profile = get_wallet_profile(wallet)
    if not profile:
        return None

    # Calculate account age
    account_age = calculate_account_age(profile)

    # Skip if account is too old
    if account_age > MAX_ACCOUNT_AGE_DAYS:
        return None

    # Get wallet's trade history on THIS specific market
    market_trades = get_wallet_trades_on_market(wallet, market_id)
    trades_on_market = market_trades["trade_count"]
    total_amount_on_market = market_trades["total_amount"]

    # Skip if not enough trades on this market
    if trades_on_market < MIN_TRADES_ON_MARKET:
        return None

    # Skip if total amount on market is less than threshold
    if total_amount_on_market < MIN_TOTAL_AMOUNT_ON_MARKET:
        return None

    # Get positions and calculate concentration
    positions = get_wallet_positions(wallet)
    concentration = calculate_portfolio_concentration(positions, market_id)

    # Get total markets traded
    stats = get_wallet_stats(wallet)
    markets_traded = stats.get("traded", 0) if stats else 0

    # Calculate a "whale score" (higher = more suspicious)
    whale_score = 0
    whale_score += min(25, (MAX_ACCOUNT_AGE_DAYS - account_age) / 2)  # Newer = higher score
    whale_score += min(25, concentration / 4)  # Higher concentration = higher score
    whale_score += min(25, total_amount_on_market / 1000)  # Larger total on market = higher score
    whale_score += min(15, trades_on_market * 3)  # More trades on same market = higher score
    whale_score += min(10, (MAX_MARKETS_TRADED - markets_traded)) if markets_traded < MAX_MARKETS_TRADED else 0

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
        "market_activity": {
            "trades_on_market": trades_on_market,
            "total_amount_on_market": round(total_amount_on_market, 2),
            "net_position": round(market_trades["net_amount"], 2),
            "dominant_side": market_trades["dominant_side"]
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
    market_activity = analysis["market_activity"]

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

    # Calculate decimal odds (sports betting format)
    decimal_odds = (1 / trade['price']) if trade['price'] > 0 else 0

    message = f"""
{emoji} <b>WHALE ALERT</b> [{urgency}]

<b>Market:</b> {trade['market']}
<b>Bet:</b> {trade['outcome']} ({trade['side']})
<b>Odds:</b> {decimal_odds:.2f} ({trade['price']*100:.0f}% implied)
<b>Shares:</b> {trade['size']:,.0f}
<b>Cost:</b> ${trade['amount']:,.2f}

<b>Activity on This Market:</b>
• Total Trades: {market_activity['trades_on_market']}
• Total Invested: ${market_activity['total_amount_on_market']:,.2f}
• Net Position: ${market_activity['net_position']:,.2f} {market_activity['dominant_side']}

<b>Trader Profile:</b>
• Username: {analysis['username']}
• Account Age: {analysis['account_age_days']} days
• Focus: {portfolio['concentration']:.0f}% portfolio in this market
• Total Markets: {portfolio['markets_traded']}

<b>Whale Score:</b> {analysis['whale_score']}/100

🔗 <a href="https://polygonscan.com/tx/{analysis['tx_hash']}">View TX</a>
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
    print(f"  • Excluded Tags: {EXCLUDED_TAG_IDS} (Sports=1, Crypto=235)")
    print("=" * 60)

    # Load excluded markets cache
    print("[INFO] Loading excluded markets (sports, crypto)...")
    refresh_excluded_markets()

    # Send startup message
    send_telegram_message("🐋 Whale Detector Bot Started!\n\nMonitoring Polymarket for suspicious large trades (excluding sports & crypto)...")

    while True:
        try:
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Fetching trades...")

            # Refresh excluded markets cache periodically
            refresh_excluded_markets()

            # Fetch large trades
            trades = get_large_trades(min_amount=MIN_TRADE_AMOUNT)
            new_trades = 0
            skipped_excluded = 0
            alerts_sent = 0

            for trade in trades:
                tx_hash = trade.get("transactionHash", "")

                # Skip if we've already processed this trade
                if tx_hash in seen_trades:
                    continue

                seen_trades.add(tx_hash)
                new_trades += 1

                # Keep seen_trades from growing indefinitely
                if len(seen_trades) > 100000:
                    seen_trades.clear()

                # Skip excluded markets (sports, crypto)
                condition_id = trade.get("conditionId", "")
                if is_excluded_market(condition_id):
                    skipped_excluded += 1
                    continue

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

            print(f"Processed {new_trades} new trades, skipped {skipped_excluded} (sports/crypto), sent {alerts_sent} alerts")

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
