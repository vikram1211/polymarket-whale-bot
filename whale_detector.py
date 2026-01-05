"""
Polymarket Whale Detector Bot (v2)
New algorithm with LP detection, signal-based filtering, and rate limiting.
Sends alerts via Telegram.
"""

import os
import time
import json
import threading
import requests
from datetime import datetime, timezone
from collections import deque
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

# Detection thresholds
MIN_TRADE_SIZE = int(os.getenv("MIN_TRADE_SIZE", 2000))  # Minimum trade size in USD
MAX_ACCOUNT_AGE_DAYS = int(os.getenv("MAX_ACCOUNT_AGE_DAYS", 30))  # Fresh wallet threshold
STATS_INTERVAL = int(os.getenv("STATS_INTERVAL", 15))  # Seconds between stats prints

# API endpoints
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

# Excluded market categories (tag IDs from Polymarket)
EXCLUDED_TAG_IDS = [int(x) for x in os.getenv("EXCLUDED_TAG_IDS", "1,235").split(",")]
EXCLUDED_CACHE_TTL = int(os.getenv("EXCLUDED_CACHE_TTL", 3600))

# Caches
wallet_cache = {}
seen_trades = set()
excluded_markets_cache = {"condition_ids": set(), "last_updated": 0}

# Rate limiting for Telegram
telegram_queue = deque()
last_telegram_send = 0
TELEGRAM_RATE_LIMIT = 1.0  # seconds between messages

# Stats
stats = {
    "trades_received": 0,
    "size_filtered": 0,
    "excluded_market": 0,
    "lp_filtered": 0,
    "no_signals": 0,
    "alerts_sent": 0
}


def send_telegram_message(message: str) -> bool:
    """Send a message via Telegram bot with rate limiting."""
    global last_telegram_send
    
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[TELEGRAM] Missing credentials, logging instead:")
        print(message[:200] + "..." if len(message) > 200 else message)
        return False

    # Rate limiting
    now = time.time()
    wait_time = TELEGRAM_RATE_LIMIT - (now - last_telegram_send)
    if wait_time > 0:
        time.sleep(wait_time)

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        last_telegram_send = time.time()
        if response.status_code == 200:
            print(f"[TELEGRAM] Alert sent!")
            return True
        else:
            print(f"[TELEGRAM ERROR] Status: {response.status_code}")
            return False
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False


def get_markets_by_tag(tag_id: int) -> set:
    """Fetch all market condition IDs for a given tag."""
    condition_ids = set()
    offset = 0
    limit = 100

    while True:
        try:
            response = requests.get(
                f"{GAMMA_API}/markets",
                params={"tag_id": tag_id, "limit": limit, "offset": offset, "closed": "false"},
                timeout=30
            )
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
                break
        except Exception as e:
            print(f"[ERROR] Failed to fetch markets for tag {tag_id}: {e}")
            break

    return condition_ids


def refresh_excluded_markets():
    """Refresh the cache of excluded market condition IDs."""
    global excluded_markets_cache

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
    if wallet in wallet_cache:
        cached = wallet_cache[wallet]
        if time.time() - cached.get("last_updated", 0) < 3600:
            return cached.get("profile")

    try:
        response = requests.get(
            f"{GAMMA_API}/public-profile",
            params={"address": wallet},
            timeout=10
        )
        if response.status_code == 200:
            profile = response.json()
            if wallet not in wallet_cache:
                wallet_cache[wallet] = {}
            wallet_cache[wallet]["profile"] = profile
            wallet_cache[wallet]["last_updated"] = time.time()
            return profile
        return None
    except Exception:
        return None


def get_wallet_positions(wallet: str) -> list:
    """Get wallet's current positions."""
    try:
        response = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "limit": 100},
            timeout=10
        )
        if response.status_code == 200:
            return response.json()
        return []
    except Exception:
        return []


def calculate_account_age(profile: dict) -> int:
    """Calculate account age in days."""
    created_at = profile.get("createdAt")
    if not created_at:
        return 9999

    try:
        created_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        return (now - created_date).days
    except Exception:
        return 9999


def is_liquidity_provider(wallet: str, condition_id: str) -> bool:
    """
    Check if wallet appears to be a liquidity provider.
    LPs typically have balanced YES/NO positions.
    """
    positions = get_wallet_positions(wallet)
    
    # Find positions in this market
    market_positions = [p for p in positions if p.get("conditionId") == condition_id]
    
    if len(market_positions) < 2:
        return False
    
    # Check if they have both YES and NO positions that are roughly balanced
    yes_value = 0
    no_value = 0
    
    for pos in market_positions:
        outcome = pos.get("outcome", "").lower()
        value = abs(float(pos.get("currentValue", 0)))
        
        if "yes" in outcome:
            yes_value += value
        elif "no" in outcome:
            no_value += value
    
    # If both sides have significant value and are within 50% of each other, likely LP
    if yes_value > 100 and no_value > 100:
        ratio = min(yes_value, no_value) / max(yes_value, no_value)
        if ratio > 0.5:  # Within 50% = balanced = likely LP
            return True
    
    return False


def detect_signals(trade_data: dict, profile: dict, positions: list) -> list:
    """
    Detect trading signals that indicate potential informed trading.
    Returns list of detected signal names.
    """
    signals = []
    
    # Signal 1: Fresh wallet
    account_age = calculate_account_age(profile)
    if account_age <= MAX_ACCOUNT_AGE_DAYS:
        signals.append(f"fresh_wallet ({account_age}d)")
    
    # Signal 2: Size anomaly (large trade relative to typical)
    trade_size = float(trade_data.get("size", 0)) * float(trade_data.get("price", 0))
    if trade_size >= 5000:
        signals.append(f"large_trade (${trade_size:,.0f})")
    
    # Signal 3: Contrarian bet (betting on low odds)
    price = float(trade_data.get("price", 0))
    if price < 0.30:  # Betting on <30% odds
        signals.append(f"contrarian ({price*100:.0f}%)")
    
    # Signal 4: High concentration (most of portfolio in one market)
    condition_id = trade_data.get("condition_id") or trade_data.get("conditionId", "")
    total_value = sum(abs(float(p.get("currentValue", 0))) for p in positions)
    market_value = sum(
        abs(float(p.get("currentValue", 0))) 
        for p in positions 
        if p.get("conditionId") == condition_id
    )
    if total_value > 0 and market_value / total_value > 0.7:
        signals.append(f"concentrated ({market_value/total_value*100:.0f}%)")
    
    return signals


def get_market_info(condition_id: str) -> dict | None:
    """Fetch market info to get the title."""
    try:
        response = requests.get(
            f"{GAMMA_API}/markets",
            params={"condition_id": condition_id},
            timeout=10
        )
        if response.status_code == 200:
            markets = response.json()
            if markets and len(markets) > 0:
                return markets[0]
        return None
    except Exception:
        return None


def format_alert(trade_data: dict, profile: dict, signals: list, market_info: dict) -> str:
    """Format the alert message for Telegram."""
    size = float(trade_data.get("size", 0))
    price = float(trade_data.get("price", 0))
    trade_amount = size * price
    
    # Calculate odds
    decimal_odds = (1 / price) if price > 0 else 0
    
    # Get market title
    market_title = market_info.get("question", "Unknown Market") if market_info else "Unknown Market"
    
    # Account age
    account_age = calculate_account_age(profile)
    username = profile.get("pseudonym") or profile.get("name") or "Unknown"
    
    # Format signals
    signal_list = "\n".join([f"  • {s}" for s in signals])
    
    message = f"""
🚨 <b>WHALE ALERT</b>

<b>Market:</b> {market_title}
<b>Bet:</b> {trade_data.get('outcome', 'Unknown')} ({trade_data.get('side', 'Unknown')})
<b>Odds:</b> {decimal_odds:.2f} ({price*100:.0f}% implied)
<b>Size:</b> {size:,.0f} shares
<b>Cost:</b> ${trade_amount:,.2f}

<b>Trader:</b>
  • Username: {username}
  • Account Age: {account_age} days

<b>Signals Detected:</b>
{signal_list}

🔗 <a href="https://polygonscan.com/tx/{trade_data.get('transaction_hash', '')}">View TX</a>
"""
    return message.strip()


def process_trade(trade_data: dict) -> None:
    """Process a single trade through the detection pipeline."""
    global stats
    
    stats["trades_received"] += 1
    
    # Dedup
    trade_id = f"{trade_data.get('transaction_hash', '')}-{trade_data.get('asset', '')}"
    if trade_id in seen_trades:
        return
    seen_trades.add(trade_id)
    
    # Cleanup seen_trades periodically
    if len(seen_trades) > 50000:
        seen_list = list(seen_trades)
        seen_trades.clear()
        for t in seen_list[-25000:]:
            seen_trades.add(t)
    
    # === FILTER 1: Size Filter ===
    size = float(trade_data.get("size", 0))
    price = float(trade_data.get("price", 0))
    trade_amount = size * price
    
    if trade_amount < MIN_TRADE_SIZE:
        return
    
    stats["size_filtered"] += 1
    
    # === FILTER 2: Market Filter (Sports/Crypto) ===
    condition_id = trade_data.get("condition_id") or trade_data.get("conditionId", "")
    if is_excluded_market(condition_id):
        stats["excluded_market"] += 1
        return
    
    # Get wallet
    wallet = trade_data.get("proxy_wallet") or trade_data.get("proxyWallet")
    if not wallet:
        return
    
    # === FILTER 3: LP Detection ===
    if is_liquidity_provider(wallet, condition_id):
        stats["lp_filtered"] += 1
        return
    
    # === SIGNAL DETECTION ===
    profile = get_wallet_profile(wallet)
    if not profile:
        return
    
    positions = get_wallet_positions(wallet)
    signals = detect_signals(trade_data, profile, positions)
    
    # Need at least 1 signal to alert
    if len(signals) < 1:
        stats["no_signals"] += 1
        return
    
    # === ENRICH & ALERT ===
    market_info = get_market_info(condition_id)
    
    print(f"\n{'='*40}")
    print(f"🚨 WHALE DETECTED!")
    print(f"Amount: ${trade_amount:,.2f}")
    print(f"Signals: {', '.join(signals)}")
    print(f"{'='*40}")
    
    # Format and send alert
    message = format_alert(trade_data, profile, signals, market_info)
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
        self.stale_threshold = 30

    def on_open(self, ws):
        print("[WS] Connected to Polymarket RTDS")
        self.connected = True
        self.reconnect_delay = 5
        self.last_message_time = time.time()

        subscribe_msg = {
            "action": "subscribe",
            "subscriptions": [{"topic": "activity", "type": "trades"}]
        }
        ws.send(json.dumps(subscribe_msg))
        print("[WS] Subscribed to activity/trades")
        self.start_health_thread()

    def on_message(self, ws, message):
        self.last_message_time = time.time()
        try:
            data = json.loads(message)
            if data.get("topic") == "activity" and data.get("type") == "trades":
                payload = data.get("payload", {})
                if payload:
                    process_trade(payload)
        except json.JSONDecodeError:
            pass
        except Exception as e:
            print(f"[WS ERROR] {e}")

    def on_error(self, ws, error):
        print(f"[WS ERROR] {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"[WS] Connection closed: {close_status_code}")
        self.connected = False
        if self.should_run:
            print(f"[WS] Reconnecting in {self.reconnect_delay}s...")
            time.sleep(self.reconnect_delay)
            self.reconnect_delay = min(self.reconnect_delay * 2, 60)
            self.connect()

    def start_health_thread(self):
        def health_loop():
            while self.connected and self.should_run:
                time.sleep(10)
                if not self.connected:
                    break
                seconds_since = time.time() - self.last_message_time
                if seconds_since > self.stale_threshold:
                    print(f"[WS HEALTH] No messages for {seconds_since:.0f}s - reconnecting")
                    self.connected = False
                    if self.ws:
                        self.ws.close()
                    break

        self.health_thread = threading.Thread(target=health_loop, daemon=True)
        self.health_thread.start()

    def connect(self):
        self.ws = websocket.WebSocketApp(
            RTDS_WS_URL,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def stop(self):
        self.should_run = False
        if self.ws:
            self.ws.close()


def print_stats_periodically():
    """Print stats at configured interval."""
    while True:
        time.sleep(STATS_INTERVAL)
        print(f"\n[STATS] Recv: {stats['trades_received']} | "
              f"$2k+: {stats['size_filtered']} | "
              f"Excluded: {stats['excluded_market']} | "
              f"LP: {stats['lp_filtered']} | "
              f"NoSig: {stats['no_signals']} | "
              f"Alerts: {stats['alerts_sent']}")
        refresh_excluded_markets()


def run_detector():
    """Main detection loop using WebSocket."""
    print("=" * 60)
    print("🐋 Polymarket Whale Detector v2 Starting...")
    print("=" * 60)
    print(f"Settings:")
    print(f"  • Min Trade Size: ${MIN_TRADE_SIZE:,}")
    print(f"  • Fresh Wallet: <{MAX_ACCOUNT_AGE_DAYS} days")
    print(f"  • Excluded Tags: {EXCLUDED_TAG_IDS}")
    print(f"  • Mode: Real-time WebSocket")
    print("=" * 60)

    print("[INFO] Loading excluded markets...")
    refresh_excluded_markets()

    send_telegram_message("🐋 Whale Detector v2 Started!\n\nMonitoring for large trades with signal detection...")

    stats_thread = threading.Thread(target=print_stats_periodically, daemon=True)
    stats_thread.start()

    client = WebSocketClient()
    try:
        client.connect()
    except KeyboardInterrupt:
        print("\n🛑 Stopping...")
        client.stop()
        send_telegram_message("🛑 Whale Detector Stopped")


if __name__ == "__main__":
    run_detector()
