"""
Polymarket Whale Detector v3
Clean architecture with weighted signal scoring.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable

import requests
from dotenv import load_dotenv

try:
    from cachetools import LRUCache, TTLCache
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "cachetools"])
    from cachetools import LRUCache, TTLCache

try:
    import websocket
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "websocket-client"])
    import websocket

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class Config:
    """All configuration loaded from environment."""
    # Telegram
    telegram_token: str = ""
    telegram_chat_id: str = ""
    telegram_rate_limit: float = 1.0

    # Detection thresholds
    min_trade_size: int = 2000
    max_account_age_days: int = 45
    min_odds: float = 1.8
    min_alert_score: int = 40

    # Market exclusions
    excluded_tag_ids: list[int] = field(default_factory=lambda: [1, 235])
    excluded_cache_ttl: int = 3600

    # Cache sizes
    wallet_cache_size: int = 10000
    wallet_cache_ttl: int = 3600
    position_cache_ttl: int = 300
    dedup_cache_size: int = 100000

    # Operational
    stats_interval: int = 15
    ws_stale_threshold: int = 30

    @classmethod
    def from_env(cls) -> Config:
        excluded = os.getenv("EXCLUDED_TAG_IDS", "1,235")
        return cls(
            telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            min_trade_size=int(os.getenv("MIN_TRADE_SIZE", 2000)),
            max_account_age_days=int(os.getenv("MAX_ACCOUNT_AGE_DAYS", 45)),
            min_odds=float(os.getenv("MIN_ODDS", 1.8)),
            min_alert_score=int(os.getenv("MIN_ALERT_SCORE", 40)),
            excluded_tag_ids=[int(x) for x in excluded.split(",") if x],
            stats_interval=int(os.getenv("STATS_INTERVAL", 15)),
        )


# =============================================================================
# Data Models
# =============================================================================

class SignalType(Enum):
    FRESH_WALLET = "fresh_wallet"
    LARGE_TRADE = "large_trade"
    CONTRARIAN = "contrarian"
    CONCENTRATED = "concentrated"
    FIRST_TRADE = "first_trade"


@dataclass
class Signal:
    type: SignalType
    score: float
    detail: str


@dataclass
class Trade:
    """Normalized trade from WebSocket."""
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

    @property
    def implied_prob(self) -> float:
        return self.price * 100

    @classmethod
    def from_ws_payload(cls, data: dict) -> Trade | None:
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
class WalletProfile:
    address: str
    username: str
    age_days: int
    created_at: datetime | None


@dataclass
class TradeContext:
    """Enriched trade with wallet data."""
    trade: Trade
    profile: WalletProfile
    positions: list[dict]
    market_info: dict | None = None

    @property
    def market_value(self) -> float:
        return sum(
            abs(float(p.get("currentValue", 0)))
            for p in self.positions
            if p.get("conditionId") == self.trade.condition_id
        )

    @property
    def total_value(self) -> float:
        return sum(abs(float(p.get("currentValue", 0))) for p in self.positions)

    @property
    def concentration(self) -> float:
        if self.total_value == 0:
            return 0
        return self.market_value / self.total_value


@dataclass
class Stats:
    trades_received: int = 0
    passed_size: int = 0
    passed_market: int = 0
    passed_odds: int = 0
    passed_lp: int = 0
    alerts_sent: int = 0

    def log(self):
        log.info(
            f"Trades Received: {self.trades_received} | "
            f"Passed Size: {self.passed_size} | "
            f"Passed Market: {self.passed_market} | "
            f"Passed Odds: {self.passed_odds} | "
            f"Passed LP Check: {self.passed_lp} | "
            f"Alerts Sent: {self.alerts_sent}"
        )


# =============================================================================
# API Client with Caching
# =============================================================================

class PolymarketAPI:
    DATA_API = "https://data-api.polymarket.com"
    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self, config: Config):
        self.config = config
        self._profile_cache: TTLCache = TTLCache(
            maxsize=config.wallet_cache_size,
            ttl=config.wallet_cache_ttl
        )
        self._position_cache: TTLCache = TTLCache(
            maxsize=config.wallet_cache_size,
            ttl=config.position_cache_ttl
        )
        self._market_cache: TTLCache = TTLCache(maxsize=5000, ttl=3600)
        self._excluded_markets: set[str] = set()
        self._excluded_loaded = False

    def get_profile(self, wallet: str) -> WalletProfile | None:
        if wallet in self._profile_cache:
            return self._profile_cache[wallet]

        try:
            resp = requests.get(
                f"{self.GAMMA_API}/public-profile",
                params={"address": wallet},
                timeout=10
            )
            if resp.status_code != 200:
                return None

            data = resp.json()
            age = self._calc_age(data.get("createdAt"))
            profile = WalletProfile(
                address=wallet,
                username=data.get("pseudonym") or data.get("name") or "Unknown",
                age_days=age,
                created_at=self._parse_date(data.get("createdAt"))
            )
            self._profile_cache[wallet] = profile
            return profile
        except Exception:
            return None

    def get_positions(self, wallet: str) -> list[dict]:
        if wallet in self._position_cache:
            return self._position_cache[wallet]

        try:
            resp = requests.get(
                f"{self.DATA_API}/positions",
                params={"user": wallet, "limit": 100},
                timeout=10
            )
            if resp.status_code == 200:
                positions = resp.json()
                self._position_cache[wallet] = positions
                return positions
        except Exception:
            pass
        return []

    def get_market_info(self, condition_id: str) -> dict | None:
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        try:
            resp = requests.get(
                f"{self.GAMMA_API}/markets",
                params={"condition_id": condition_id},
                timeout=10
            )
            if resp.status_code == 200:
                markets = resp.json()
                if markets:
                    self._market_cache[condition_id] = markets[0]
                    return markets[0]
        except Exception:
            pass
        return None

    def get_trade_count(self, wallet: str, market_id: str) -> int:
        """Get number of trades wallet has made on this market."""
        try:
            resp = requests.get(
                f"{self.DATA_API}/trades",
                params={"user": wallet, "market": market_id, "limit": 100},
                timeout=10
            )
            if resp.status_code == 200:
                return len(resp.json())
        except Exception:
            pass
        return 0

    def is_excluded_market(self, condition_id: str) -> bool:
        if not self._excluded_loaded:
            self._load_excluded_markets()
        return condition_id in self._excluded_markets

    def _load_excluded_markets(self):
        log.info(f"Loading excluded markets for tags: {self.config.excluded_tag_ids}")
        for tag_id in self.config.excluded_tag_ids:
            count = self._load_tag_markets(tag_id)
            log.info(f"Tag {tag_id}: {count} markets excluded")
        log.info(f"Total excluded: {len(self._excluded_markets)}")
        self._excluded_loaded = True

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

    @staticmethod
    def _calc_age(created_at: str | None) -> int:
        if not created_at:
            return 9999
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - dt).days
        except Exception:
            return 9999

    @staticmethod
    def _parse_date(created_at: str | None) -> datetime | None:
        if not created_at:
            return None
        try:
            return datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except Exception:
            return None


# =============================================================================
# Filters
# =============================================================================

class Filter:
    """Base filter - returns True to PASS, False to REJECT."""
    def __call__(self, trade: Trade) -> bool:
        raise NotImplementedError


class SizeFilter(Filter):
    def __init__(self, min_usd: float):
        self.min_usd = min_usd

    def __call__(self, trade: Trade) -> bool:
        return trade.amount_usd >= self.min_usd


class OddsFilter(Filter):
    def __init__(self, min_odds: float):
        self.min_odds = min_odds

    def __call__(self, trade: Trade) -> bool:
        return trade.decimal_odds >= self.min_odds


class MarketFilter(Filter):
    def __init__(self, api: PolymarketAPI):
        self.api = api

    def __call__(self, trade: Trade) -> bool:
        return not self.api.is_excluded_market(trade.condition_id)


# =============================================================================
# Signal Analyzer
# =============================================================================

class SignalAnalyzer:
    """Weighted scoring for whale signals."""

    def __init__(self, config: Config):
        self.config = config

    def analyze(self, ctx: TradeContext) -> tuple[float, list[Signal]]:
        signals: list[Signal] = []

        # Fresh wallet (0-30 points)
        age = ctx.profile.age_days
        if age <= self.config.max_account_age_days:
            score = 30 * (1 - age / self.config.max_account_age_days)
            signals.append(Signal(
                SignalType.FRESH_WALLET, score,
                f"{age}d old"
            ))

        # Large trade (0-25 points, log scale)
        amount = ctx.trade.amount_usd
        if amount >= self.config.min_trade_size:
            # Log scale: $2k=0, $5k=10, $10k=17, $50k=25
            ratio = amount / self.config.min_trade_size
            score = min(25, 10 * math.log2(ratio))
            if score >= 5:
                signals.append(Signal(
                    SignalType.LARGE_TRADE, score,
                    f"${amount:,.0f}"
                ))

        # Contrarian bet (0-20 points)
        price = ctx.trade.price
        if price < 0.5:
            score = min(20, (0.5 - price) * 40)
            signals.append(Signal(
                SignalType.CONTRARIAN, score,
                f"{price*100:.0f}% odds"
            ))

        # Concentrated portfolio (0-15 points)
        conc = ctx.concentration
        if conc > 0.5:
            score = min(15, (conc - 0.5) * 30)
            signals.append(Signal(
                SignalType.CONCENTRATED, score,
                f"{conc*100:.0f}% portfolio"
            ))

        # First trade on market (10 points)
        # This is checked via positions - if no position exists yet, it's first trade
        if ctx.market_value == 0:
            signals.append(Signal(
                SignalType.FIRST_TRADE, 10,
                "first position"
            ))

        total = sum(s.score for s in signals)
        return total, signals

    def is_liquidity_provider(self, positions: list[dict], condition_id: str) -> bool:
        """Check if wallet has balanced YES/NO positions (LP behavior)."""
        market_pos = [p for p in positions if p.get("conditionId") == condition_id]
        if len(market_pos) < 2:
            return False

        yes_val = sum(
            abs(float(p.get("currentValue", 0)))
            for p in market_pos if "yes" in p.get("outcome", "").lower()
        )
        no_val = sum(
            abs(float(p.get("currentValue", 0)))
            for p in market_pos if "no" in p.get("outcome", "").lower()
        )

        if yes_val > 100 and no_val > 100:
            ratio = min(yes_val, no_val) / max(yes_val, no_val)
            return ratio > 0.5
        return False


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
            log.warning("Telegram not configured, logging alert instead")
            print(message[:300])
            return False

        with self._lock:
            wait = self.config.telegram_rate_limit - (time.time() - self._last_send)
            if wait > 0:
                time.sleep(wait)

            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{self.config.telegram_token}/sendMessage",
                    json={
                        "chat_id": self.config.telegram_chat_id,
                        "text": message,
                        "parse_mode": "HTML"
                    },
                    timeout=10
                )
                self._last_send = time.time()
                return resp.status_code == 200
            except Exception as e:
                log.error(f"Telegram send failed: {e}")
                return False

    def format_alert(self, ctx: TradeContext, score: float, signals: list[Signal]) -> str:
        trade = ctx.trade
        market_title = ctx.market_info.get("question", "Unknown") if ctx.market_info else "Unknown"

        signal_lines = "\n".join(
            f"  • {s.type.value}: {s.detail} (+{s.score:.0f})"
            for s in sorted(signals, key=lambda x: -x.score)
        )

        return f"""
🐋 <b>WHALE ALERT</b> (Score: {score:.0f})

<b>Market:</b> {market_title}
<b>Bet:</b> {trade.outcome} ({trade.side})
<b>Odds:</b> {trade.decimal_odds:.2f} ({trade.implied_prob:.0f}% implied)
<b>Size:</b> {trade.size:,.0f} shares
<b>Cost:</b> ${trade.amount_usd:,.2f}

<b>Trader:</b> {ctx.profile.username}
<b>Account Age:</b> {ctx.profile.age_days} days

<b>Signals:</b>
{signal_lines}

🔗 <a href="https://polygonscan.com/tx/{trade.tx_hash}">View TX</a>
""".strip()


# =============================================================================
# Trade Processor
# =============================================================================

class TradeProcessor:
    def __init__(
        self,
        config: Config,
        api: PolymarketAPI,
        analyzer: SignalAnalyzer,
        alerter: TelegramAlerter
    ):
        self.config = config
        self.api = api
        self.analyzer = analyzer
        self.alerter = alerter
        self.stats = Stats()
        self._seen: LRUCache = LRUCache(maxsize=config.dedup_cache_size)

        # Fast filters (no API calls)
        self.filters: list[tuple[str, Filter]] = [
            ("size", SizeFilter(config.min_trade_size)),
            ("odds", OddsFilter(config.min_odds)),
            ("market", MarketFilter(api)),
        ]

    def process(self, data: dict) -> None:
        self.stats.trades_received += 1

        # Parse trade
        trade = Trade.from_ws_payload(data)
        if not trade:
            return

        # Dedup
        trade_id = f"{trade.tx_hash}-{trade.condition_id}"
        if trade_id in self._seen:
            return
        self._seen[trade_id] = True

        # Fast filters
        if not self._apply_filters(trade):
            return

        # Enrich (API calls)
        ctx = self._enrich(trade)
        if not ctx:
            return

        # LP check
        if self.analyzer.is_liquidity_provider(ctx.positions, trade.condition_id):
            return
        self.stats.passed_lp += 1

        # Signal analysis
        score, signals = self.analyzer.analyze(ctx)
        if score < self.config.min_alert_score:
            return

        # Enrich with market info
        ctx.market_info = self.api.get_market_info(trade.condition_id)

        # Alert
        log.info(f"🐋 WHALE: ${trade.amount_usd:,.0f} | Score: {score:.0f}")
        message = self.alerter.format_alert(ctx, score, signals)
        if self.alerter.send(message):
            self.stats.alerts_sent += 1

    def _apply_filters(self, trade: Trade) -> bool:
        for name, filt in self.filters:
            if not filt(trade):
                return False
            if name == "size":
                self.stats.passed_size += 1
            elif name == "odds":
                self.stats.passed_odds += 1
            elif name == "market":
                self.stats.passed_market += 1
        return True

    def _enrich(self, trade: Trade) -> TradeContext | None:
        profile = self.api.get_profile(trade.wallet)
        if not profile:
            return None
        positions = self.api.get_positions(trade.wallet)
        return TradeContext(trade=trade, profile=profile, positions=positions)


# =============================================================================
# WebSocket Client
# =============================================================================

class WebSocketClient:
    WS_URL = "wss://ws-live-data.polymarket.com"

    def __init__(self, processor: TradeProcessor, config: Config):
        self.processor = processor
        self.config = config
        self.ws: websocket.WebSocketApp | None = None
        self._connected = False
        self._should_run = True
        self._last_message = 0.0
        self._reconnect_delay = 5

    def start(self):
        while self._should_run:
            try:
                self._connect()
            except Exception as e:
                log.error(f"WebSocket error: {e}")

            if self._should_run:
                log.info(f"Reconnecting in {self._reconnect_delay}s...")
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    def stop(self):
        self._should_run = False
        if self.ws:
            self.ws.close()

    def _connect(self):
        self.ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws.run_forever(ping_interval=20, ping_timeout=10)

    def _on_open(self, ws):
        log.info("Connected to Polymarket RTDS")
        self._connected = True
        self._reconnect_delay = 5
        self._last_message = time.time()

        ws.send(json.dumps({
            "action": "subscribe",
            "subscriptions": [{"topic": "activity", "type": "trades"}]
        }))
        log.info("Subscribed to activity/trades")

        threading.Thread(target=self._health_monitor, daemon=True).start()

    def _on_message(self, ws, message: str):
        self._last_message = time.time()
        try:
            data = json.loads(message)
            if data.get("topic") == "activity" and data.get("type") == "trades":
                payload = data.get("payload", {})
                if payload:
                    self.processor.process(payload)
        except json.JSONDecodeError:
            pass

    def _on_error(self, ws, error):
        log.error(f"WebSocket error: {error}")

    def _on_close(self, ws, status_code, msg):
        log.info(f"WebSocket closed: {status_code}")
        self._connected = False

    def _health_monitor(self):
        while self._connected and self._should_run:
            time.sleep(10)
            if not self._connected:
                break
            stale = time.time() - self._last_message
            if stale > self.config.ws_stale_threshold:
                log.warning(f"No messages for {stale:.0f}s, reconnecting")
                self._connected = False
                if self.ws:
                    self.ws.close()
                break


# =============================================================================
# Main
# =============================================================================

def main():
    config = Config.from_env()

    log.info("=" * 50)
    log.info("🐋 Polymarket Whale Detector v3")
    log.info("=" * 50)
    log.info(f"Min Trade: ${config.min_trade_size:,}")
    log.info(f"Min Odds: {config.min_odds} ({100/config.min_odds:.0f}% implied)")
    log.info(f"Fresh Wallet: <{config.max_account_age_days} days")
    log.info(f"Alert Score: >={config.min_alert_score}")
    log.info(f"Excluded Tags: {config.excluded_tag_ids}")
    log.info("=" * 50)

    api = PolymarketAPI(config)
    analyzer = SignalAnalyzer(config)
    alerter = TelegramAlerter(config)
    processor = TradeProcessor(config, api, analyzer, alerter)
    client = WebSocketClient(processor, config)

    # Stats printer
    def print_stats():
        while True:
            time.sleep(config.stats_interval)
            processor.stats.log()

    threading.Thread(target=print_stats, daemon=True).start()

    alerter.send("🐋 Whale Detector v3 Started!")

    try:
        client.start()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        client.stop()
        alerter.send("🛑 Whale Detector stopped")


if __name__ == "__main__":
    main()
