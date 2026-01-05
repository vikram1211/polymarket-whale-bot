"""
Polymarket Whale Detector v4
Optimized filter pipeline with signal-based detection.
"""

import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
    import requests

try:
    import websocket
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client"])
    import websocket

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv optional

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout
)
log = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    telegram_token: str = ""
    telegram_chat_id: str = ""
    min_trade_size: int = 2000
    min_alert_score: int = 40
    max_fresh_wallet_age: int = 45
    size_anomaly_multiplier: float = 2.0
    timing_hours_before_end: int = 48
    longshot_threshold: float = 0.50
    excluded_tag_ids: list = field(default_factory=lambda: [1, 235])
    stats_interval: int = 15

    @classmethod
    def from_env(cls) -> "Config":
        excluded = os.getenv("EXCLUDED_TAG_IDS", "1,235")
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
            min_trade_size=int(os.getenv("MIN_TRADE_SIZE", 2000)),
            min_alert_score=int(os.getenv("MIN_ALERT_SCORE", 40)),
            max_fresh_wallet_age=int(os.getenv("MAX_FRESH_WALLET_AGE", 45)),
            size_anomaly_multiplier=float(os.getenv("SIZE_ANOMALY_MULTIPLIER", 2.0)),
            timing_hours_before_end=int(os.getenv("TIMING_HOURS_BEFORE_END", 48)),
            longshot_threshold=float(os.getenv("LONGSHOT_THRESHOLD", 0.50)),
            excluded_tag_ids=[int(x) for x in excluded.split(",") if x],
            stats_interval=int(os.getenv("STATS_INTERVAL", 15)),
        )


# =============================================================================
# Simple TTL Cache
# =============================================================================

class TTLCache:
    def __init__(self, ttl: int, maxsize: int = 5000):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: dict = {}
        self._timestamps: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._data:
                return None
            if time.time() - self._timestamps[key] > self.ttl:
                del self._data[key]
                del self._timestamps[key]
                return None
            return self._data[key]

    def set(self, key, value):
        with self._lock:
            if len(self._data) >= self.maxsize and key not in self._data:
                oldest = min(self._timestamps, key=self._timestamps.get)
                del self._data[oldest]
                del self._timestamps[oldest]
            self._data[key] = value
            self._timestamps[key] = time.time()

    def __contains__(self, key):
        return self.get(key) is not None


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class Trade:
    tx_hash: str
    condition_id: str
    market_slug: str
    wallet: str
    side: str
    outcome: str
    size: float
    price: float

    @property
    def amount_usd(self) -> float:
        return self.size * self.price

    @property
    def decimal_odds(self) -> float:
        return 1 / self.price if self.price > 0 else 0

    @classmethod
    def from_ws(cls, data: dict) -> Optional["Trade"]:
        wallet = data.get("proxy_wallet") or data.get("proxyWallet")
        if not wallet:
            return None
        return cls(
            tx_hash=data.get("transaction_hash", ""),
            condition_id=data.get("condition_id") or data.get("conditionId", ""),
            market_slug=data.get("market_slug") or data.get("slug", ""),
            wallet=wallet,
            side=data.get("side", ""),
            outcome=data.get("outcome", ""),
            size=float(data.get("size", 0)),
            price=float(data.get("price", 0)),
        )


@dataclass
class Signal:
    name: str
    score: float
    detail: str


@dataclass
class Stats:
    received: int = 0
    filtered_market: int = 0
    filtered_size: int = 0
    filtered_lp: int = 0
    filtered_score: int = 0
    alerts: int = 0

    def log(self):
        log.info(
            f"Received: {self.received} | "
            f"Market: -{self.filtered_market} | "
            f"Size: -{self.filtered_size} | "
            f"LP: -{self.filtered_lp} | "
            f"Score: -{self.filtered_score} | "
            f"Alerts: {self.alerts}"
        )


# =============================================================================
# API Client
# =============================================================================

class PolymarketAPI:
    DATA_API = "https://data-api.polymarket.com"
    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self, config: Config):
        self.config = config
        self._excluded_markets: set = set()
        self._profile_cache = TTLCache(ttl=3600, maxsize=5000)
        self._positions_cache = TTLCache(ttl=300, maxsize=5000)
        self._market_cache = TTLCache(ttl=3600, maxsize=5000)
        self._trades_cache = TTLCache(ttl=300, maxsize=5000)

    def load_excluded_markets(self):
        log.info(f"Loading excluded markets for tags: {self.config.excluded_tag_ids}")
        for tag_id in self.config.excluded_tag_ids:
            count = self._load_tag_markets(tag_id)
            log.info(f"Tag {tag_id}: {count} markets")
        log.info(f"Total excluded: {len(self._excluded_markets)}")

    def _load_tag_markets(self, tag_id: int) -> int:
        count = 0
        offset = 0
        while True:
            try:
                resp = requests.get(
                    f"{self.GAMMA_API}/markets",
                    params={"tag_id": tag_id, "limit": 100, "offset": offset, "closed": "false"},
                    timeout=30
                )
                if resp.status_code != 200:
                    break
                markets = resp.json()
                if not markets:
                    break
                for m in markets:
                    cid = m.get("conditionId")
                    if cid:
                        self._excluded_markets.add(cid)
                        count += 1
                if len(markets) < 100:
                    break
                offset += 100
            except Exception:
                break
        return count

    def is_excluded(self, condition_id: str) -> bool:
        return condition_id in self._excluded_markets

    def get_profile(self, wallet: str) -> Optional[dict]:
        cached = self._profile_cache.get(wallet)
        if cached:
            return cached
        try:
            resp = requests.get(
                f"{self.GAMMA_API}/public-profile",
                params={"address": wallet},
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                self._profile_cache.set(wallet, data)
                return data
        except Exception:
            pass
        return None

    def get_positions(self, wallet: str) -> list:
        cached = self._positions_cache.get(wallet)
        if cached is not None:
            return cached
        try:
            resp = requests.get(
                f"{self.DATA_API}/positions",
                params={"user": wallet, "limit": 100},
                timeout=10
            )
            if resp.status_code == 200:
                positions = resp.json()
                self._positions_cache.set(wallet, positions)
                return positions
        except Exception:
            pass
        return []

    def get_market(self, condition_id: str) -> Optional[dict]:
        cached = self._market_cache.get(condition_id)
        if cached:
            return cached
        try:
            resp = requests.get(
                f"{self.GAMMA_API}/markets",
                params={"condition_id": condition_id},
                timeout=10
            )
            if resp.status_code == 200:
                markets = resp.json()
                if markets:
                    self._market_cache.set(condition_id, markets[0])
                    return markets[0]
        except Exception:
            pass
        return None

    def get_trades(self, wallet: str) -> list:
        cached = self._trades_cache.get(wallet)
        if cached is not None:
            return cached
        try:
            resp = requests.get(
                f"{self.DATA_API}/trades",
                params={"user": wallet, "limit": 20},
                timeout=10
            )
            if resp.status_code == 200:
                trades = resp.json()
                self._trades_cache.set(wallet, trades)
                return trades
        except Exception:
            pass
        return []


# =============================================================================
# Signal Detector
# =============================================================================

class SignalDetector:
    def __init__(self, config: Config, api: PolymarketAPI):
        self.config = config
        self.api = api

    def detect(self, trade: Trade) -> tuple[float, list[Signal]]:
        signals = []

        # 1. Fresh wallet (30 pts max)
        profile = self.api.get_profile(trade.wallet)
        if profile:
            age = self._calc_age(profile.get("createdAt"))
            if age <= self.config.max_fresh_wallet_age:
                score = 30 * (1 - age / self.config.max_fresh_wallet_age)
                signals.append(Signal("fresh_wallet", score, f"{age}d old"))

        # 2. Longshot / Odds movement (25 pts max)
        if trade.price < self.config.longshot_threshold:
            score = 25 * (1 - trade.price / self.config.longshot_threshold)
            signals.append(Signal("longshot", score, f"{trade.price*100:.0f}% odds"))

        # 3. Size anomaly (25 pts max)
        past_trades = self.api.get_trades(trade.wallet)
        if past_trades:
            avg_size = sum(float(t.get("size", 0)) * float(t.get("price", 0)) for t in past_trades) / len(past_trades)
            if avg_size > 0 and trade.amount_usd > avg_size * self.config.size_anomaly_multiplier:
                ratio = min(trade.amount_usd / avg_size, 10)
                score = min(25, 25 * (ratio - self.config.size_anomaly_multiplier) / 7)
                signals.append(Signal("size_anomaly", max(score, 10), f"{ratio:.1f}x avg"))

        # 4. Timing - near market end (20 pts max)
        market = self.api.get_market(trade.condition_id)
        if market:
            end_date = market.get("endDate")
            if end_date:
                hours_left = self._hours_until(end_date)
                if hours_left is not None and 0 < hours_left <= self.config.timing_hours_before_end:
                    score = 20 * (1 - hours_left / self.config.timing_hours_before_end)
                    signals.append(Signal("timing", score, f"{hours_left:.0f}h left"))

        total = sum(s.score for s in signals)
        return total, signals

    def is_lp(self, positions: list, condition_id: str) -> bool:
        market_pos = [p for p in positions if p.get("conditionId") == condition_id]
        if len(market_pos) < 2:
            return False
        yes_val = sum(abs(float(p.get("currentValue", 0))) for p in market_pos if "yes" in p.get("outcome", "").lower())
        no_val = sum(abs(float(p.get("currentValue", 0))) for p in market_pos if "no" in p.get("outcome", "").lower())
        if yes_val > 100 and no_val > 100:
            return min(yes_val, no_val) / max(yes_val, no_val) > 0.5
        return False

    @staticmethod
    def _calc_age(created_at: Optional[str]) -> int:
        if not created_at:
            return 9999
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return 9999

    @staticmethod
    def _hours_until(end_date: str) -> Optional[float]:
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            delta = dt - datetime.now(timezone.utc)
            return delta.total_seconds() / 3600
        except Exception:
            return None


# =============================================================================
# Telegram Alerter
# =============================================================================

class TelegramAlerter:
    def __init__(self, config: Config):
        self.config = config
        self._last_send = 0.0
        self._lock = threading.Lock()

    def send(self, message: str) -> bool:
        if not self.config.telegram_token or not self.config.telegram_chat_id:
            log.warning("Telegram not configured")
            print(message[:300])
            return False

        with self._lock:
            wait = 1.0 - (time.time() - self._last_send)
            if wait > 0:
                time.sleep(wait)
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage",
                    json={"chat_id": self.config.telegram_chat_id, "text": message, "parse_mode": "HTML"},
                    timeout=10
                )
                self._last_send = time.time()
                return resp.status_code == 200
            except Exception as e:
                log.error(f"Telegram error: {e}")
                return False

    def format_alert(self, trade: Trade, score: float, signals: list[Signal], profile: Optional[dict], market: Optional[dict]) -> str:
        market_title = market.get("question", "Unknown") if market else "Unknown"
        username = profile.get("pseudonym") or profile.get("name") or "Unknown" if profile else "Unknown"
        signal_lines = "\n".join(f"  - {s.name}: {s.detail} (+{s.score:.0f})" for s in sorted(signals, key=lambda x: -x.score))

        return f"""
🐋 <b>WHALE ALERT</b> (Score: {score:.0f})

<b>Market:</b> {market_title}
<b>Bet:</b> {trade.outcome} ({trade.side})
<b>Odds:</b> {trade.decimal_odds:.2f} ({trade.price*100:.0f}% implied)
<b>Size:</b> {trade.size:,.0f} shares
<b>Cost:</b> ${trade.amount_usd:,.2f}

<b>Trader:</b> {username}

<b>Signals:</b>
{signal_lines}

🔗 <a href="https://polygonscan.com/tx/{trade.tx_hash}">TX</a>
""".strip()


# =============================================================================
# Trade Processor
# =============================================================================

class TradeProcessor:
    def __init__(self, config: Config, api: PolymarketAPI, detector: SignalDetector, alerter: TelegramAlerter):
        self.config = config
        self.api = api
        self.detector = detector
        self.alerter = alerter
        self.stats = Stats()
        self._seen: set = set()
        self._seen_max = 100000

    def process(self, data: dict):
        self.stats.received += 1

        trade = Trade.from_ws(data)
        if not trade:
            return

        # Dedup
        trade_id = f"{trade.tx_hash}-{trade.condition_id}"
        if trade_id in self._seen:
            return
        self._seen.add(trade_id)
        if len(self._seen) > self._seen_max:
            self._seen.clear()

        # Filter 1: Market (excluded tags) - NO API CALL
        if self.api.is_excluded(trade.condition_id):
            self.stats.filtered_market += 1
            return

        # Filter 2: Size - NO API CALL
        if trade.amount_usd < self.config.min_trade_size:
            self.stats.filtered_size += 1
            return

        # Filter 3: LP Detection - uses cached positions
        positions = self.api.get_positions(trade.wallet)
        if self.detector.is_lp(positions, trade.condition_id):
            self.stats.filtered_lp += 1
            return

        # Signal detection (API calls happen here)
        score, signals = self.detector.detect(trade)
        if score < self.config.min_alert_score:
            self.stats.filtered_score += 1
            return

        # Alert
        log.info(f"🐋 WHALE: ${trade.amount_usd:,.0f} | Score: {score:.0f}")
        profile = self.api.get_profile(trade.wallet)
        market = self.api.get_market(trade.condition_id)
        message = self.alerter.format_alert(trade, score, signals, profile, market)
        if self.alerter.send(message):
            self.stats.alerts += 1


# =============================================================================
# WebSocket Client
# =============================================================================

class WebSocketClient:
    URL = "wss://ws-live-data.polymarket.com"

    def __init__(self, processor: TradeProcessor):
        self.processor = processor
        self.ws = None
        self._running = True
        self._last_msg = 0.0
        self._delay = 5

    def start(self):
        while self._running:
            try:
                self._connect()
            except Exception as e:
                log.error(f"WS error: {e}")
            if self._running:
                log.info(f"Reconnecting in {self._delay}s...")
                time.sleep(self._delay)
                self._delay = min(self._delay * 2, 60)

    def stop(self):
        self._running = False
        if self.ws:
            self.ws.close()

    def _connect(self):
        self.ws = websocket.WebSocketApp(
            self.URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        log.info("Connected to Polymarket RTDS")
        self._delay = 5
        self._last_msg = time.time()
        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "activity", "type": "trades"}]
        }))
        log.info("Subscribed to activity/trades")
        threading.Thread(target=self._health_check, daemon=True).start()

    def _on_message(self, ws, message: str):
        self._last_msg = time.time()
        try:
            data = json.loads(message)
            if data.get("topic") == "activity" and data.get("type") == "trades":
                payload = data.get("payload", {})
                if payload:
                    self.processor.process(payload)
        except json.JSONDecodeError:
            pass

    def _on_error(self, ws, error):
        log.error(f"WS error: {error}")

    def _on_close(self, ws, status_code, msg):
        log.info(f"WS closed: {status_code}")

    def _health_check(self):
        while self._running and self.ws:
            time.sleep(10)
            if time.time() - self._last_msg > 30:
                log.warning("No messages for 30s, reconnecting...")
                if self.ws:
                    self.ws.close()
                break


# =============================================================================
# Main
# =============================================================================

def main():
    config = Config.from_env()

    log.info("=" * 50)
    log.info("🐋 Polymarket Whale Detector v4")
    log.info("=" * 50)
    log.info(f"Min Trade: ${config.min_trade_size:,}")
    log.info(f"Min Score: {config.min_alert_score}")
    log.info(f"Fresh Wallet: <{config.max_fresh_wallet_age} days")
    log.info(f"Longshot: <{config.longshot_threshold*100:.0f}%")
    log.info(f"Size Anomaly: >{config.size_anomaly_multiplier}x avg")
    log.info(f"Timing: <{config.timing_hours_before_end}h before end")
    log.info(f"Excluded Tags: {config.excluded_tag_ids}")
    log.info("=" * 50)

    api = PolymarketAPI(config)
    api.load_excluded_markets()

    detector = SignalDetector(config, api)
    alerter = TelegramAlerter(config)
    processor = TradeProcessor(config, api, detector, alerter)
    client = WebSocketClient(processor)

    def print_stats():
        while True:
            time.sleep(config.stats_interval)
            processor.stats.log()

    threading.Thread(target=print_stats, daemon=True).start()
    alerter.send("🐋 Whale Detector v4 Started!")

    try:
        client.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        client.stop()
        alerter.send("🛑 Whale Detector stopped")


if __name__ == "__main__":
    main()
