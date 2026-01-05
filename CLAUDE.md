# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Polymarket Whale Detector v4 - Real-time bot monitoring Polymarket for large trades with whale signals. Sends alerts via Telegram.

## Commands

```bash
python whale_detector.py
```

Dependencies auto-install on first run (`requests`, `websocket-client`). Requires `.env` with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Deploys to Railway via `railway.toml` and `Procfile`.

## Architecture

Single-file (`whale_detector.py`) with optimized filter pipeline:

```
RTDS WebSocket (wss://ws-live-data.polymarket.com)
        │
        ▼
   Trade Received
        │
        ▼
┌─────────────────┐
│ Market Filter   │ ── Excluded tag? → Discard (cached set, no API)
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ Size Filter     │ ── < $2,000? → Discard
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ LP Detection    │ ── Balanced YES/NO? → Discard (cached positions)
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ Signal Detection│ ── Score < 40? → Discard
│ • Fresh wallet  │    (API calls happen here)
│ • Size anomaly  │
│ • Timing        │
│ • Longshot      │
└─────────────────┘
        │
        ▼
   Telegram Alert
```

**Key Classes:**
- `Config` - Dataclass loading settings from env
- `TTLCache` - Simple dict-based cache with TTL (replaces cachetools)
- `PolymarketAPI` - API client with caching for profiles, positions, markets, trades
- `SignalDetector` - Detects whale signals and calculates score
- `TradeProcessor` - Pipeline: dedup → market filter → size filter → LP → signals → alert
- `TelegramAlerter` - Rate-limited (1/sec) message sending
- `WebSocketClient` - RTDS connection with health monitoring

**API Endpoints:**
- Data API: `https://data-api.polymarket.com` (positions, trades)
- Gamma API: `https://gamma-api.polymarket.com` (profiles, markets)

**Signal Scoring:**
| Signal | Max Points | Logic |
|--------|------------|-------|
| Fresh wallet | 30 | Account < 30 days old |
| Longshot | 25 | Betting on < 35% odds |
| Size anomaly | 25 | Trade > 3x wallet's average |
| Timing | 20 | Market ends within 24h |

## Configuration

Environment variables:
- `TELEGRAM_BOT_TOKEN` - Required
- `TELEGRAM_CHAT_ID` - Required
- `MIN_TRADE_SIZE` - USD threshold (default: 2000)
- `MIN_ALERT_SCORE` - Score threshold (default: 40)
- `MAX_FRESH_WALLET_AGE` - Days for fresh wallet signal (default: 30)
- `SIZE_ANOMALY_MULTIPLIER` - Multiple of avg trade size (default: 3.0)
- `TIMING_HOURS_BEFORE_END` - Hours before market end (default: 24)
- `LONGSHOT_THRESHOLD` - Max implied probability (default: 0.35)
- `EXCLUDED_TAG_IDS` - Tags to skip (default: "1,235" for sports/crypto)
- `STATS_INTERVAL` - Seconds between stats logs (default: 15)
