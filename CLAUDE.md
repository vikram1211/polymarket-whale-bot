# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Polymarket Whale Detector v3 - Real-time bot monitoring Polymarket for large trades with whale characteristics. Uses weighted signal scoring and sends alerts via Telegram.

## Commands

```bash
pip install -r requirements.txt
python whale_detector.py
```

Requires `.env` with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Architecture

Single-file (`whale_detector.py`) with clean component separation:

```
Config → PolymarketAPI → TradeProcessor → TelegramAlerter
                              ↓
                       SignalAnalyzer
                              ↓
                       WebSocketClient
```

**Key Classes:**
- `Config` - Dataclass loading all settings from env
- `PolymarketAPI` - API client with TTLCache for profiles (1hr), positions (5min), markets
- `TradeProcessor` - Pipeline: dedup → filters → enrich → analyze → alert
- `SignalAnalyzer` - Weighted scoring (0-100), alerts when score ≥ 40
- `TelegramAlerter` - Rate-limited (1/sec) message sending
- `WebSocketClient` - RTDS connection with health monitoring

**Filter Pipeline** (fast filters first, no API calls until passed):
1. Size filter (≥$2000)
2. Odds filter (≥1.8 decimal odds)
3. Market filter (exclude sports/crypto tags)
4. LP detection (balanced YES/NO = market maker)

**Signal Scoring:**
| Signal | Max Points | Logic |
|--------|------------|-------|
| Fresh wallet | 30 | Linear: newer = higher |
| Large trade | 25 | Log scale above $2k |
| Contrarian | 20 | <50% implied odds |
| Concentrated | 15 | >50% portfolio in market |
| First trade | 10 | No prior position |

## Configuration

Environment variables:
- `MIN_TRADE_SIZE` - USD threshold (default: 2000)
- `MAX_ACCOUNT_AGE_DAYS` - Fresh wallet cap (default: 45)
- `MIN_ODDS` - Minimum decimal odds (default: 1.8)
- `MIN_ALERT_SCORE` - Score threshold (default: 40)
- `EXCLUDED_TAG_IDS` - Tags to skip (default: "1,235" for sports/crypto)
