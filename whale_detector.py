"""
Polymarket Whale Detector Bot
Monitors large trades from new accounts with high portfolio concentration.
Sends alerts via Telegram.
Uses WebSocket for real-time trade streaming.
"""

import os
import time
import json
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

try:
    import websocket
except ImportError:
    print("Installing websocket-client...")
    import subprocess
    subprocess.check_call(["pip", "install", "websocket-client"])
    import websocket

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
MIN_TRADES_ON_MARKET = int(os.getenv("MIN_TRADES_ON_MARKET", 3))  # Min trades on same market
MIN_TOTAL_AMOUNT_ON_MARKET = int(os.getenv("MIN_TOTAL_AMOUNT_ON_MARKET", 5000))  # Min total $ on same market

# API endpoints
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

# Excluded market categories (tag IDs from Polymarket)
# Tag 1 = Sports, Tag 235 = Bitcoin/Crypto
EXCLUDED_TAG_IDS = [int(x) for x in os.getenv("EXCLUDED_TAG_IDS", "1,235").split(",")]
EXCLUDED_CACHE_TTL = int(os.getenv("EXCLUDED_CACHE_TTL", 3600))  # Refresh excluded markets every hour

# Cache to avoid redundant API calls and duplicate alerts
wallet_cache = {}  # wallet -> {profile, positions, last_updated}
seen_trades = set()  # Set of trade identifiers we've already processed
excluded_markets_cache = {"condition_ids": set(), "last_updated": 0}  # Excluded market condition IDs

# Stats
stats = {"trades_received": 0, "trades_processed": 0, "alerts_sent": 0, "skipped_excluded": 0}


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


def get_market_info(condition_id: str) -> dict | None:
    """Fetch market info to get the title."""
    url = f"{GAMMA_API}/markets"
    params = {"condition_id": condition_id}

    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            markets = response.json()
            if markets and len(markets) > 0:
                return markets[0]
        return None
    except Exception as e:
        print(f"[ERROR] Failed to fetch market info: {e}")
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
    # Handle both REST API and WebSocket field names
    wallet = trade.get("proxyWallet") or trade.get("proxy_wallet")
    if not wallet:
        return None

    # Get trade details
    size = float(trade.get("size", 0))
    price = float(trade.get("price", 0))
    trade_amount = size * price
    market_id = trade.get("conditionId") or trade.get("condition_id", "")

    # Skip small trades
    if trade_amount < MIN_TRADE_AMOUNT:
        return None

    # Skip trades with >50% implied probability (focus on longshots)
    if price > 0.5:
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
    wallet_stats = get_wallet_stats(wallet)
    markets_traded = wallet_stats.get("traded", 0) if wallet_stats else 0

    # Get market title
    market_title = trade.get("title") or trade.get("market_slug", "")
    if not market_title:
        market_info = get_market_info(market_id)
        market_title = market_info.get("question", "Unknown Market") if market_info else "Unknown Market"

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
            "market": market_title,
            "outcome": trade.get("outcome", "Unknown"),
            "side": trade.get("side", "Unknown"),
            "amount": round(trade_amount, 2),
            "price": price,
            "size": size
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
        "tx_hash": trade.get("transactionHash") or trade.get("transaction_hash", "")
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


def process_trade(trade_data: dict) -> None:
    """Process a single trade from WebSocket."""
    global stats

    stats["trades_received"] += 1

    # Create unique trade ID
    trade_id = f"{trade_data.get('transaction_hash', '')}-{trade_data.get('asset', '')}-{trade_data.get('timestamp', '')}"

    # Skip if already processed
    if trade_id in seen_trades:
        return

    seen_trades.add(trade_id)

    # Keep seen_trades from growing indefinitely
    if len(seen_trades) > 100000:
        # Remove oldest half
        seen_list = list(seen_trades)
        seen_trades.clear()
        seen_trades.update(seen_list[50000:])

    # Calculate trade amount
    size = float(trade_data.get("size", 0))
    price = float(trade_data.get("price", 0))
    trade_amount = size * price

    # Skip small trades early
    if trade_amount < MIN_TRADE_AMOUNT:
        return

    stats["trades_processed"] += 1

    # Check if excluded market
    condition_id = trade_data.get("condition_id") or trade_data.get("conditionId", "")
    if is_excluded_market(condition_id):
        stats["skipped_excluded"] += 1
        return

    # Normalize field names for analyze_trade
    normalized_trade = {
        "proxyWallet": trade_data.get("proxy_wallet") or trade_data.get("proxyWallet"),
        "size": size,
        "price": price,
        "conditionId": condition_id,
        "side": trade_data.get("side", ""),
        "outcome": trade_data.get("outcome", ""),
        "title": trade_data.get("market_slug") or trade_data.get("slug", ""),
        "transactionHash": trade_data.get("transaction_hash") or trade_data.get("transactionHash", "")
    }

    # Analyze the trade
    analysis = analyze_trade(normalized_trade)

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
            stats["alerts_sent"] += 1


class WebSocketClient:
    """WebSocket client for Polymarket RTDS with connection health monitoring."""

    def __init__(self):
        self.ws = None
        self.connected = False
        self.health_thread = None
        self.should_run = True
        self.reconnect_delay = 5
        self.last_message_time = 0
        self.stale_threshold = 30  # Force reconnect if no messages for 30s

    def on_open(self, ws):
        """Called when WebSocket connection is established."""
        print("[WS] Connected to Polymarket RTDS")
        self.connected = True
        self.reconnect_delay = 5  # Reset reconnect delay
        self.last_message_time = time.time()

        # Subscribe to trades
        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": "activity",
                    "type": "trades"
                }
            ]
        }
        ws.send(json.dumps(subscribe_msg))
        print("[WS] Subscribed to activity/trades")

        # Start health monitoring thread
        self.start_health_thread()

    def on_message(self, ws, message):
        """Called when a message is received."""
        self.last_message_time = time.time()  # Update on ANY message
        
        try:
            data = json.loads(message)

            # Handle trade messages
            if data.get("topic") == "activity" and data.get("type") == "trades":
                payload = data.get("payload", {})
                if payload:
                    process_trade(payload)

        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"[WS ERROR] Processing message: {e}")

    def on_error(self, ws, error):
        """Called when an error occurs."""
        print(f"[WS ERROR] {error}")

    def on_close(self, ws, close_status_code, close_msg):
        """Called when connection is closed."""
        print(f"[WS] Connection closed: {close_status_code} - {close_msg}")
        self.connected = False

        if self.should_run:
            print(f"[WS] Reconnecting in {self.reconnect_delay}s...")
            time.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, 60)  # Exponential backoff
            self.connect()

    def start_health_thread(self):
        """Start a thread to monitor connection health and force reconnect if stale."""
        def health_loop():
            while self.connected and self.should_run:
                try:
                    time.sleep(10)  # Check every 10 seconds
                    
                    if not self.connected:
                        break
                    
                    # Check if connection is stale
                    seconds_since_message = time.time() - self.last_message_time
                    
                    if seconds_since_message > self.stale_threshold:
                        print(f"[WS HEALTH] No messages for {seconds_since_message:.0f}s - forcing reconnect")
                        self.connected = False
                        if self.ws:
                            self.ws.close()
                        break
                        
                except Exception as e:
                    print(f"[WS HEALTH ERROR] {e}")
                    break

        self.health_thread = threading.Thread(target=health_loop, daemon=True)
        self.health_thread.start()

    def connect(self):
        """Connect to WebSocket with ping/pong enabled."""
        self.ws = websocket.WebSocketApp(
            RTDS_WS_URL,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        # Enable WebSocket protocol-level ping/pong (every 20s, timeout 10s)
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def stop(self):
        """Stop the WebSocket client."""
        self.should_run = False
        if self.ws:
            self.ws.close()


def print_stats_periodically():
    """Print stats every 60 seconds."""
    while True:
        time.sleep(60)
        print(f"\n[STATS] Received: {stats['trades_received']} | Processed: {stats['trades_processed']} | Excluded: {stats['skipped_excluded']} | Alerts: {stats['alerts_sent']}")

        # Refresh excluded markets cache
        refresh_excluded_markets()


def run_detector():
    """Main detection loop using WebSocket."""
    print("=" * 60)
    print("🐋 Polymarket Whale Detector Starting (WebSocket Mode)...")
    print("=" * 60)
    print(f"Settings:")
    print(f"  • Min Trade Amount: ${MIN_TRADE_AMOUNT}")
    print(f"  • Max Account Age: {MAX_ACCOUNT_AGE_DAYS} days")
    print(f"  • Min Concentration: {MIN_CONCENTRATION}%")
    print(f"  • Max Markets Traded: {MAX_MARKETS_TRADED}")
    print(f"  • Excluded Tags: {EXCLUDED_TAG_IDS} (Sports=1, Crypto=235)")
    print(f"  • Mode: Real-time WebSocket")
    print("=" * 60)

    # Load excluded markets cache
    print("[INFO] Loading excluded markets (sports, crypto)...")
    refresh_excluded_markets()

    # Send startup message
    send_telegram_message("🐋 Whale Detector Bot Started (WebSocket Mode)!\n\nMonitoring Polymarket in real-time for suspicious large trades (excluding sports & crypto)...")

    # Start stats printer thread
    stats_thread = threading.Thread(target=print_stats_periodically, daemon=True)
    stats_thread.start()

    # Connect to WebSocket
    client = WebSocketClient()

    try:
        client.connect()
    except KeyboardInterrupt:
        print("\n\n🛑 Stopping whale detector...")
        client.stop()
        send_telegram_message("🛑 Whale Detector Bot Stopped")


if __name__ == "__main__":
    run_detector()
