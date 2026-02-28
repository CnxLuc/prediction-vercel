"""
Bot Arena — Prediction Market Paper Trading Engine (Vercel Serverless)
"""

import json
import os
import sys
import time
import math
import random
import re
import hashlib
import traceback
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler

# ─── Config ───────────────────────────────────────────────────────────────────────────

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Supabase Storage for persistent state
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://kipjcmqlxkohtbghlicf.supabase.co").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
BUCKET = "bot-data"

# Local /tmp fallback paths (used as write-through cache)
BOTS_DB = Path("/tmp/bots_state.json")
TRADES_DB = Path("/tmp/bots_trades.json")
CACHE_FILE = Path("/tmp/bots_market_cache.json")
CACHE_TTL = 1500  # ~25 min (so 30-min cron always gets fresh data)

INITIAL_BANKROLL = 10000.0


# ─── Supabase Storage Helpers ─────────────────────────────────────────────────────

def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

def sb_read(filename):
    """Read JSON from Supabase Storage. Returns parsed dict/list or None."""
    if not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except:
        return None

def sb_write(filename, data):
    """Write JSON to Supabase Storage (upsert). Returns True on success."""
    if not SUPABASE_KEY:
        return False
    payload = json.dumps(data).encode()
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/octet-stream",
        "x-upsert": "true",
    }
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True
    except:
        return False

def sb_delete(filename):
    """Delete a file from Supabase Storage."""
    if not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{filename}"
    req = urllib.request.Request(url, headers=_sb_headers(), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return True
    except:
        return False

# ─── Bot Definitions ────────────────────────────────────────────────────────────────────

BOTS = [
    {
        "id": "tiago",
        "name": "Tiago the Armadillo",
        "animal": "armadillo",
        "emoji": "🦔",
        "title": "Defensive Value Analyst",
        "personality": "Conservative, methodical, obsessed with downside protection. Speaks in measured tones. Never chases hype. Favorite quote: 'The first rule is don't lose money. The second rule is don't forget the first rule.'",
        "strategy": "contrarian_value",
        "strategy_name": "Contrarian Value",
        "strategy_desc": "Buys markets that are significantly underpriced vs reference odds. Only enters when the edge exceeds 8pp AND liquidity is sufficient. Uses quarter-Kelly sizing to limit downside. Avoids markets expiring within 7 days (too much noise). Think of Tiago as the Warren Buffett of prediction markets — patient, disciplined, value-obsessed.",
        "params": {"min_edge": 8, "kelly_fraction": 0.25, "min_volume": 50000, "max_positions": 5, "min_days_to_expiry": 7},
        "color": "#00ff88",
        "accent": "#00cc6a",
    },
    {
        "id": "mako",
        "name": "Mako the Shark",
        "animal": "shark",
        "emoji": "🦈",
        "title": "Cross-Platform Arbitrageur",
        "personality": "Aggressive, fast-moving, speaks in short decisive sentences. Sees markets as a feeding ground. Always scanning for blood in the water. Never explains twice.",
        "strategy": "cross_platform_arb",
        "strategy_name": "Cross-Platform Arbitrage",
        "strategy_desc": "Exploits price discrepancies between Polymarket and Kalshi. When the same event trades at different prices, Mako buys cheap and sells expensive — locking in a spread regardless of outcome. Uses half-Kelly on the spread size. The purest form of alpha: structural market inefficiency.",
        "params": {"min_spread": 5, "kelly_fraction": 0.5, "min_volume": 20000, "max_positions": 8},
        "color": "#4488ff",
        "accent": "#3366cc",
    },
    {
        "id": "freya",
        "name": "Freya the Fox",
        "animal": "fox",
        "emoji": "🦊",
        "title": "Momentum & Narrative Trader",
        "personality": "Cunning, articulate, reads the room better than anyone. Trades narrative shifts before the crowd catches on. Loves a good story. Always has a contrarian take ready.",
        "strategy": "momentum_narrative",
        "strategy_name": "Momentum Narrative",
        "strategy_desc": "Identifies markets where 24h volume is surging relative to historical average, signaling a narrative shift. Buys into the momentum early, rides it, and exits when volume fades. Uses third-Kelly sizing. Freya reads the crowd — when everyone suddenly cares about a market, there's usually a reason.",
        "params": {"volume_surge_ratio": 2.0, "kelly_fraction": 0.33, "min_volume_24h": 30000, "max_positions": 6, "min_edge": 3},
        "color": "#ff8800",
        "accent": "#cc6600",
    },
    {
        "id": "ollie",
        "name": "Ollie the Owl",
        "animal": "owl",
        "emoji": "🦉",
        "title": "Statistical Macro Analyst",
        "personality": "Academic, precise, quotes base rates and historical precedents constantly. Slightly pedantic but almost always right. Wears metaphorical reading glasses. Loves Bayesian reasoning.",
        "strategy": "statistical_value",
        "strategy_name": "Base Rate Bayesian",
        "strategy_desc": "Uses historical base rates and Bayesian updating to estimate true probabilities. Only trades when market price diverges from the base-rate-adjusted estimate by 10pp+. Conservative Kelly at 20%. Ollie doesn't care what the crowd thinks — he cares what the data says.",
        "params": {"min_edge": 10, "kelly_fraction": 0.20, "min_volume": 30000, "max_positions": 4, "min_days_to_expiry": 14},
        "color": "#9966ff",
        "accent": "#7744cc",
    },
    {
        "id": "pepper",
        "name": "Pepper the Honeybadger",
        "animal": "honeybadger",
        "emoji": "🦡",
        "title": "High-Conviction YOLO Trader",
        "personality": "Absolutely fearless, borderline reckless. Talks in ALL CAPS when excited. Has zero respect for conventional wisdom. Will size up aggressively on high-conviction plays. Somehow it works more often than it should.",
        "strategy": "high_conviction",
        "strategy_name": "High Conviction",
        "strategy_desc": "Takes concentrated positions in markets with massive edges (15pp+). Uses full Kelly — maximum aggression. Fewer trades but much larger sizing. Pepper doesn't diversify. Pepper doesn't hedge. Pepper sees edge and attacks. High variance, high potential return.",
        "params": {"min_edge": 15, "kelly_fraction": 1.0, "min_volume": 100000, "max_positions": 3},
        "color": "#ff3355",
        "accent": "#cc2244",
    },
    {
        "id": "ming",
        "name": "Ming the Pangolin",
        "animal": "pangolin",
        "emoji": "🐲",
        "title": "Tail Risk Specialist",
        "personality": "Quiet, patient, philosophical. Sees the world through the lens of rare events. Speaks softly but carries devastating conviction when tail events approach. Favorite topic: black swans.",
        "strategy": "tail_risk",
        "strategy_name": "Tail Risk Hunter",
        "strategy_desc": "Specializes in markets priced at extreme probabilities (<15% or >85%) where the market may be underpricing tail risk. Buys cheap long shots and sells expensive near-certainties. Uses tiny Kelly (10%) because most of these won't hit — but when they do, the payoff is enormous.",
        "params": {"low_threshold": 15, "high_threshold": 85, "min_mispricing": 5, "kelly_fraction": 0.10, "max_positions": 6, "min_volume": 25000},
        "color": "#ffaa00",
        "accent": "#cc8800",
    },
]


# ─── Helpers ────────────────────────────────────────────────────────────────────────

def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "BotArena/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}

def safe_float(val, default=None):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

def deterministic_seed(bot_id, market_title, timestamp_hour):
    raw = f"{bot_id}:{market_title}:{timestamp_hour}"
    return int(hashlib.md5(raw.encode()).hexdigest()[:8], 16)


# ─── Market Fetchers ────────────────────────────────────────────────────────────────

def guess_category(title):
    t = title.lower()
    if any(w in t for w in ["election", "trump", "democrat", "republican", "house", "senate", "congress", "president", "governor", "vote", "impeach", "nominate", "fed chair"]):
        return "Politics"
    if any(w in t for w in ["fed", "rate", "inflation", "cpi", "gdp", "recession", "unemployment", "bitcoin", "crypto", "s&p", "stock", "gold", "tariff"]):
        return "Economics"
    if any(w in t for w in ["war", "ceasefire", "ukraine", "russia", "china", "taiwan", "iran", "strike", "nuclear", "nato", "invasion", "khamenei"]):
        return "Geopolitics"
    if any(w in t for w in ["nba", "nfl", "mlb", "nhl", "f1", "formula", "premier league", "champions league", "world cup", "march madness", "ncaa", "pga", "masters", "super bowl", "arsenal", "manchester"]):
        return "Sports"
    if any(w in t for w in ["ai", "gpt", "claude", "gemini", "openai", "anthropic", "google", "spacex", "tesla", "nvidia", "apple", "ipo", "tech"]):
        return "Tech"
    if any(w in t for w in ["oscar", "grammy", "emmy", "box office", "movie", "film", "actor", "actress", "director"]):
        return "Entertainment"
    return "Other"

def fetch_polymarket_markets():
    markets = []
    for offset in [0, 100]:
        url = f"{POLYMARKET_GAMMA}/events?active=true&closed=false&order=volume&ascending=false&limit=100&offset={offset}"
        data = fetch_json(url)
        if isinstance(data, list):
            event_list = data
        elif isinstance(data, dict) and "data" in data:
            event_list = data["data"]
        else:
            continue
        for event in event_list:
            for m in event.get("markets", []):
                try:
                    prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
                    yes_pct = safe_float(prices[0], 0) * 100 if prices else 0
                except:
                    yes_pct = 0
                if yes_pct <= 0:
                    continue
                markets.append({
                    "platform": "Polymarket",
                    "title": m.get("question", m.get("title", "")) or event.get("title", ""),
                    "event_title": event.get("title", ""),
                    "slug": event.get("slug", ""),
                    "yes_pct": round(yes_pct, 1),
                    "volume": safe_float(m.get("volume", 0), 0),
                    "volume_24hr": safe_float(event.get("volume24hr", 0), 0),
                    "liquidity": safe_float(m.get("liquidity", 0), 0),
                    "end_date": m.get("endDate", event.get("endDate", "")),
                    "category": event.get("tags", [{}])[0].get("label", "Other") if event.get("tags") else guess_category(event.get("title", "")),
                    "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                    "active": m.get("active", True),
                    "closed": m.get("closed", False),
                })
    return markets

def fetch_kalshi_markets():
    markets = []
    cursor = None
    for _ in range(3):
        url = f"{KALSHI_API}/markets?status=open&limit=100"
        if cursor:
            url += f"&cursor={cursor}"
        data = fetch_json(url)
        if "error" in data:
            break
        for m in data.get("markets", []):
            yes_price = safe_float(m.get("yes_price"), None)
            if yes_price is not None:
                yes_pct = yes_price
            else:
                yes_bid = safe_float(m.get("yes_bid"), 0)
                yes_ask = safe_float(m.get("yes_ask"), 0)
                yes_pct = (yes_bid + yes_ask) / 2 if yes_bid and yes_ask else 0
            if yes_pct <= 0:
                continue
            markets.append({
                "platform": "Kalshi",
                "title": m.get("title", ""),
                "event_title": m.get("event_ticker", ""),
                "ticker": m.get("ticker", ""),
                "yes_pct": round(yes_pct, 1),
                "volume": safe_float(m.get("volume", 0), 0),
                "volume_24hr": 0,
                "liquidity": safe_float(m.get("open_interest", 0), 0),
                "end_date": m.get("close_time", m.get("expiration_time", "")),
                "category": m.get("category", "Other"),
                "url": f"https://kalshi.com/markets/{m.get('ticker', '').lower()}",
            })
        cursor = data.get("cursor", "")
        if not cursor:
            break
    return markets


# ─── Reference Probabilities ──────────────────────────────────────────────────────────

REFERENCE_PROBS = [
    {"id": "fed_march_nochange", "prob": 96, "source": "CME FedWatch",
     "require_all": ["fed", "march"], "require_any": ["no change", "hold", "unchanged"],
     "exclude": ["chair", "nominate", "powell", "warsh", "decrease", "cut", "bps", "25", "50", "75", "100", "increase", "hike"]},
    {"id": "fed_april_nochange", "prob": 82, "source": "CME FedWatch",
     "require_all": ["fed", "april"], "require_any": ["no change", "hold", "unchanged"],
     "exclude": ["chair", "nominate", "decrease", "cut", "bps", "increase", "hike"]},
    {"id": "fed_rate_cuts_2026", "prob": 47, "source": "CME FedWatch",
     "require_all": ["fed", "rate", "cut"], "require_any": ["2026", "by end of", "total"],
     "exclude": ["chair", "nominate", "march", "april", "may", "january"]},
    {"id": "recession_2026", "prob": 30, "source": "RSM/JPMorgan consensus",
     "require_all": ["recession"], "require_any": ["2026", "us", "united states"],
     "exclude": ["global", "china", "europe", "germany", "uk", "japan"]},
    {"id": "dem_house_2026", "prob": 85, "source": "Polling consensus",
     "require_all": ["house"], "require_any": ["democrat", "democratic party", "2026 midterm"],
     "exclude": ["senate", "white house", "speaker", "majority leader"]},
    {"id": "china_taiwan_invade", "prob": 4, "source": "ASPI/CFR analysts",
     "require_all": ["china", "taiwan"], "require_any": ["invade", "invasion", "attack", "blockade"],
     "exclude": ["gta", "game", "esport"]},
    {"id": "ukraine_ceasefire", "prob": 36, "source": "Brookings consensus",
     "require_all": ["ceasefire"], "require_any": ["ukraine", "russia"],
     "exclude": ["israel", "iran", "gaza", "gta", "eurovision", "broken", "game"]},
    {"id": "arsenal_epl", "prob": 83, "source": "OddsChecker (-500)",
     "require_all": ["arsenal", "premier league"], "require_any": [],
     "exclude": ["champions league", "ucl", "fa cup", "game", "esport"]},
    {"id": "f1_russell", "prob": 33, "source": "Ladbrokes (2/1)",
     "require_all": ["russell"], "require_any": ["f1", "formula 1", "drivers championship"],
     "exclude": ["game", "esport", "lol", "valorant"]},
    {"id": "okc_nba", "prob": 30, "source": "Yahoo Sports (+230)",
     "require_all": ["thunder"], "require_any": ["nba", "nba champion"],
     "exclude": ["game", "esport", "round", "series"]},
    {"id": "us_iran_strike", "prob": 78, "source": "Polymarket consensus",
     "require_all": ["strike", "iran"], "require_any": ["us", "united states", "israel"],
     "exclude": ["next strike on", "february", "ceasefire broken", "game"]},
    {"id": "khamenei_out", "prob": 12, "source": "Expert consensus",
     "require_all": ["khamenei"], "require_any": ["out", "supreme leader", "leave", "no longer"],
     "exclude": ["game"]},
    {"id": "openai_ipo", "prob": 50, "source": "Analyst reports",
     "require_all": ["openai", "ipo"], "require_any": [],
     "exclude": ["market cap", "closing", "less than", "revenue"]},
    {"id": "spain_world_cup", "prob": 20, "source": "BetMGM (+400)",
     "require_all": ["spain", "world cup"], "require_any": [],
     "exclude": ["game", "esport", "group stage"]},
    {"id": "warsh_fed_chair", "prob": 93, "source": "Market consensus",
     "require_all": ["warsh"], "require_any": ["fed chair", "nominate", "federal reserve", "chairman"],
     "exclude": ["confirmed", "hassett", "rate", "cut", "bps"]},
]

def find_reference(title):
    t = title.lower()
    for ref in REFERENCE_PROBS:
        if not all(kw in t for kw in ref["require_all"]):
            continue
        if ref["require_any"] and not any(kw in t for kw in ref["require_any"]):
            continue
        if any(kw in t for kw in ref.get("exclude", [])):
            continue
        return ref
    return None


# ─── Cross-Platform Matching ──────────────────────────────────────────────────────────

def normalize_title(t):
    t = t.lower().strip()
    t = re.sub(r'[^a-z0-9\s]', '', t)
    t = re.sub(r'\s+', ' ', t)
    for noise in ["will ", "what ", "who ", "which ", "when ", "how ", "the ", "by end of ", "before ", "in 2026", "in 2025", "in 2027"]:
        t = t.replace(noise, "")
    return t.strip()

def keyword_overlap(t1, t2):
    words1 = set(t1.split())
    words2 = set(t2.split())
    if not words1 or not words2:
        return 0
    intersection = words1 & words2
    return len(intersection) / min(len(words1), len(words2))


# ─── Trading Constants ────────────────────────────────────────────────────────────────

MIN_TRADEABLE_PRICE = 5
MAX_TRADEABLE_PRICE = 95
MAX_BET_PER_TRADE = 800
SLIPPAGE_PCT = 2.0
FEES_PCT = 1.0


# ─── Kelly Criterion ──────────────────────────────────────────────────────────────────

def kelly_size(estimated_prob, market_price, bankroll, kelly_fraction=0.25):
    if estimated_prob <= 0.01 or estimated_prob >= 0.99 or market_price <= 0.01 or market_price >= 0.99:
        return 0, None
    b_yes = (1 / market_price) - 1
    f_yes = (b_yes * estimated_prob - (1 - estimated_prob)) / b_yes
    no_price = 1 - market_price
    no_prob = 1 - estimated_prob
    b_no = (1 / no_price) - 1
    f_no = (b_no * no_prob - (1 - no_prob)) / b_no
    if f_yes > 0 and f_yes >= f_no:
        bet = bankroll * f_yes * kelly_fraction
        bet = min(bet, bankroll * 0.15, MAX_BET_PER_TRADE)
        bet = bet * (1 - FEES_PCT / 100)
        return round(bet, 2), "BUY_YES"
    elif f_no > 0:
        bet = bankroll * f_no * kelly_fraction
        bet = min(bet, bankroll * 0.15, MAX_BET_PER_TRADE)
        bet = bet * (1 - FEES_PCT / 100)
        return round(bet, 2), "BUY_NO"
    return 0, None


# ─── Market Filters ────────────────────────────────────────────────────────────────

def is_tradeable(mkt):
    price = mkt.get("yes_pct", 0)
    if price < MIN_TRADEABLE_PRICE or price > MAX_TRADEABLE_PRICE:
        return False
    title_lower = mkt.get("title", "").lower()
    esport_keywords = ["lol:", "dota", "csgo", "cs2", "valorant", "overwatch", "league of legends",
                       "game winner", "map winner", "round winner", "esport", "fearx", "t1 vs",
                       "gen.g", "cloud9", "fnatic vs", "navi vs", "g2 vs", "100 thieves",
                       "total kills", "in game 1", "in game 2", "in game 3", "in game 4", "in game 5",
                       "first blood", "first tower", "first baron", "dragon", "rift herald"]
    if any(kw in title_lower for kw in esport_keywords):
        return False
    return True

def apply_slippage(market_price, direction):
    slip = SLIPPAGE_PCT / 100
    if direction == "BUY_YES":
        return min(market_price * (1 + slip), 99)
    elif direction == "BUY_NO":
        return max(market_price * (1 - slip), 1)
    return market_price


# ─── Bot Strategy Engines ─────────────────────────────────────────────────────────────

def strategy_contrarian_value(bot, markets, poly_markets, kalshi_markets, state):
    trades = []
    params = bot["params"]
    now = datetime.now(timezone.utc)
    for mkt in markets:
        if not is_tradeable(mkt):
            continue
        ref = find_reference(mkt["title"])
        if not ref:
            continue
        market_price = mkt["yes_pct"] / 100
        ref_prob = ref["prob"] / 100
        edge_pp = abs(mkt["yes_pct"] - ref["prob"])
        if edge_pp < params["min_edge"]:
            continue
        if mkt["volume"] < params["min_volume"]:
            continue
        if mkt.get("end_date"):
            try:
                end = datetime.fromisoformat(mkt["end_date"].replace("Z", "+00:00"))
                days_left = (end - now).total_seconds() / 86400
                if days_left < params.get("min_days_to_expiry", 7):
                    continue
            except:
                pass
        bet_amount, direction = kelly_size(ref_prob, market_price, state["bankroll"], params["kelly_fraction"])
        if bet_amount < 10:
            continue
        is_underpriced = mkt["yes_pct"] < ref["prob"]
        trades.append({
            "market": mkt["title"], "platform": mkt["platform"], "url": mkt["url"],
            "category": mkt["category"],
            "direction": "BUY_YES" if is_underpriced else "BUY_NO",
            "market_price": mkt["yes_pct"], "estimated_prob": ref["prob"],
            "edge_pp": round(edge_pp, 1), "bet_amount": round(bet_amount, 2),
            "kelly_fraction": params["kelly_fraction"], "source": ref["source"],
            "rationale": f"Market prices this at {mkt['yes_pct']:.0f}% but {ref['source']} suggests {ref['prob']}%. That's a {edge_pp:.0f}pp edge. {'Buying YES — the market is undervaluing this outcome.' if is_underpriced else 'Buying NO — the market is overvaluing this outcome.'} Volume (${mkt['volume']:,.0f}) is adequate for execution. Using {params['kelly_fraction']*100:.0f}% Kelly sizing for downside protection.",
            "confidence": "HIGH" if edge_pp >= 12 else "MEDIUM",
        })
    trades.sort(key=lambda t: -t["edge_pp"])
    return trades[:params["max_positions"]]

def strategy_cross_platform_arb(bot, markets, poly_markets, kalshi_markets, state):
    trades = []
    params = bot["params"]
    for pm in poly_markets:
        if pm.get("closed") or not pm.get("active", True):
            continue
        if not is_tradeable(pm):
            continue
        pm_norm = normalize_title(pm["title"])
        for km in kalshi_markets:
            if not is_tradeable(km):
                continue
            km_norm = normalize_title(km["title"])
            overlap = keyword_overlap(pm_norm, km_norm)
            if overlap < 0.5:
                continue
            gap = pm["yes_pct"] - km["yes_pct"]
            abs_gap = abs(gap)
            if abs_gap < params["min_spread"]:
                continue
            if gap > 0:
                buy_platform, buy_price = "Kalshi", km["yes_pct"]
                sell_platform, sell_price = "Polymarket", pm["yes_pct"]
                buy_url, sell_url = km["url"], pm["url"]
            else:
                buy_platform, buy_price = "Polymarket", pm["yes_pct"]
                sell_platform, sell_price = "Kalshi", km["yes_pct"]
                buy_url, sell_url = pm["url"], km["url"]
            spread_pct = abs_gap / 100
            bet_amount = state["bankroll"] * spread_pct * params["kelly_fraction"]
            bet_amount = min(bet_amount, state["bankroll"] * 0.20)
            if bet_amount < 10:
                continue
            trades.append({
                "market": pm["title"], "platform": f"{buy_platform} → {sell_platform}",
                "url": buy_url, "category": pm.get("category", guess_category(pm["title"])),
                "direction": "ARB", "market_price": pm["yes_pct"],
                "estimated_prob": km["yes_pct"], "edge_pp": round(abs_gap, 1),
                "bet_amount": round(bet_amount, 2), "kelly_fraction": params["kelly_fraction"],
                "source": f"{buy_platform} vs {sell_platform}",
                "rationale": f"SPREAD DETECTED: {buy_platform} has YES at {buy_price:.0f}¢, {sell_platform} at {sell_price:.0f}¢. That's a {abs_gap:.0f}¢ spread. Buy YES on {buy_platform}, buy NO on {sell_platform} — guaranteed {abs_gap:.0f}¢ profit per contract regardless of outcome. Execution risk: settlement timing and platform fees reduce effective spread.",
                "confidence": "HIGH" if abs_gap >= 10 else "MEDIUM",
                "arb_detail": {"buy_platform": buy_platform, "buy_price": buy_price, "sell_platform": sell_platform, "sell_price": sell_price, "spread": round(abs_gap, 1)},
            })
    trades.sort(key=lambda t: -t["edge_pp"])
    seen = set()
    unique = []
    for t in trades:
        key = normalize_title(t["market"])
        if key not in seen:
            seen.add(key)
            unique.append(t)
    return unique[:params["max_positions"]]

def strategy_momentum_narrative(bot, markets, poly_markets, kalshi_markets, state):
    trades = []
    params = bot["params"]
    for mkt in poly_markets:
        if mkt.get("closed") or not mkt.get("active", True):
            continue
        if not is_tradeable(mkt):
            continue
        if mkt["volume_24hr"] < params["min_volume_24h"]:
            continue
        if mkt["volume"] <= 0:
            continue
        if mkt["volume_24hr"] > mkt["volume"] * 1.5:
            continue
        if mkt["volume"] < 50000:
            continue
        surge_ratio = mkt["volume_24hr"] / mkt["volume"] if mkt["volume"] > 0 else 0
        if surge_ratio < 0.02:
            continue
        ref = find_reference(mkt["title"])
        if not ref:
            continue
        edge_pp = abs(mkt["yes_pct"] - ref["prob"])
        if edge_pp < params["min_edge"]:
            continue
        est_prob = ref["prob"] / 100
        bet_amount, direction = kelly_size(est_prob, mkt["yes_pct"] / 100, state["bankroll"], params["kelly_fraction"])
        if bet_amount < 10:
            continue
        trades.append({
            "market": mkt["title"], "platform": mkt["platform"], "url": mkt["url"],
            "category": mkt.get("category", "Other"),
            "direction": direction or "BUY_YES", "market_price": mkt["yes_pct"],
            "estimated_prob": ref["prob"], "edge_pp": round(edge_pp, 1),
            "bet_amount": round(bet_amount, 2), "kelly_fraction": params["kelly_fraction"],
            "source": f"Volume surge: ${mkt['volume_24hr']:,.0f} in 24h ({surge_ratio*100:.1f}% of total)",
            "rationale": f"Volume surge detected: ${mkt['volume_24hr']:,.0f} traded in the last 24h ({surge_ratio*100:.1f}% of all-time volume). Reference data from {ref['source']} suggests {ref['prob']}% vs market at {round(mkt['yes_pct'])}%. When volume spikes like this, the market is repricing in real-time. Getting ahead of the crowd.",
            "confidence": "HIGH" if (edge_pp >= 8 or surge_ratio >= 0.05) else "MEDIUM",
        })
    trades.sort(key=lambda t: -t["edge_pp"])
    return trades[:params["max_positions"]]

def strategy_statistical_value(bot, markets, poly_markets, kalshi_markets, state):
    trades = []
    params = bot["params"]
    now = datetime.now(timezone.utc)
    for mkt in markets:
        if not is_tradeable(mkt):
            continue
        ref = find_reference(mkt["title"])
        if not ref:
            continue
        edge_pp = abs(mkt["yes_pct"] - ref["prob"])
        if edge_pp < params["min_edge"]:
            continue
        if mkt["volume"] < params["min_volume"]:
            continue
        if mkt.get("end_date"):
            try:
                end = datetime.fromisoformat(mkt["end_date"].replace("Z", "+00:00"))
                days_left = (end - now).total_seconds() / 86400
                if days_left < params.get("min_days_to_expiry", 14):
                    continue
            except:
                pass
        ref_prob = ref["prob"] / 100
        market_price = mkt["yes_pct"] / 100
        bet_amount, direction = kelly_size(ref_prob, market_price, state["bankroll"], params["kelly_fraction"])
        if bet_amount < 10:
            continue
        is_underpriced = mkt["yes_pct"] < ref["prob"]
        trades.append({
            "market": mkt["title"], "platform": mkt["platform"], "url": mkt["url"],
            "category": mkt["category"],
            "direction": direction or ("BUY_YES" if is_underpriced else "BUY_NO"),
            "market_price": mkt["yes_pct"], "estimated_prob": ref["prob"],
            "edge_pp": round(edge_pp, 1), "bet_amount": round(bet_amount, 2),
            "kelly_fraction": params["kelly_fraction"], "source": ref["source"],
            "rationale": f"Bayesian analysis: Prior from {ref['source']} = {ref['prob']}%. Market likelihood = {mkt['yes_pct']:.0f}%. The {edge_pp:.0f}pp divergence exceeds my 10pp threshold. Historical base rates for similar events support the reference estimate. {'The market appears to be discounting information that base rates clearly support.' if is_underpriced else 'The market is pricing in too much certainty relative to historical precedent.'} Using conservative 20% Kelly — discipline over conviction.",
            "confidence": "HIGH" if edge_pp >= 15 else "MEDIUM",
        })
    trades.sort(key=lambda t: -t["edge_pp"])
    return trades[:params["max_positions"]]

def strategy_high_conviction(bot, markets, poly_markets, kalshi_markets, state):
    trades = []
    params = bot["params"]
    for mkt in markets:
        if not is_tradeable(mkt):
            continue
        ref = find_reference(mkt["title"])
        if not ref:
            continue
        edge_pp = abs(mkt["yes_pct"] - ref["prob"])
        if edge_pp < params["min_edge"]:
            continue
        if mkt["volume"] < params["min_volume"]:
            continue
        ref_prob = ref["prob"] / 100
        market_price = mkt["yes_pct"] / 100
        bet_amount, direction = kelly_size(ref_prob, market_price, state["bankroll"], params["kelly_fraction"])
        if bet_amount < 50:
            continue
        is_underpriced = mkt["yes_pct"] < ref["prob"]
        trades.append({
            "market": mkt["title"], "platform": mkt["platform"], "url": mkt["url"],
            "category": mkt["category"],
            "direction": direction or ("BUY_YES" if is_underpriced else "BUY_NO"),
            "market_price": mkt["yes_pct"], "estimated_prob": ref["prob"],
            "edge_pp": round(edge_pp, 1), "bet_amount": round(bet_amount, 2),
            "kelly_fraction": params["kelly_fraction"], "source": ref["source"],
            "rationale": f"MASSIVE EDGE: {edge_pp:.0f}pp between market ({mkt['yes_pct']:.0f}%) and reference ({ref['prob']}%). This is the kind of trade you SIZE UP on. Full Kelly. ${bet_amount:,.0f} on the line. {'The market is sleeping on this — buying YES hard.' if is_underpriced else 'The crowd is way too bullish — fading this with conviction.'} Volume ${mkt['volume']:,.0f} means we can get filled. LET'S GO.",
            "confidence": "MAXIMUM",
        })
    trades.sort(key=lambda t: -t["edge_pp"])
    return trades[:params["max_positions"]]

def strategy_tail_risk(bot, markets, poly_markets, kalshi_markets, state):
    trades = []
    params = bot["params"]
    for mkt in markets:
        if not is_tradeable(mkt):
            continue
        if mkt["volume"] < params["min_volume"]:
            continue
        ref = find_reference(mkt["title"])
        yes_pct = mkt["yes_pct"]
        if 2 <= yes_pct <= params["low_threshold"]:
            if ref:
                if ref["prob"] > yes_pct + params["min_mispricing"]:
                    edge_pp = ref["prob"] - yes_pct
                    est_prob = ref["prob"] / 100
                else:
                    continue
            else:
                edge_pp = 5
                est_prob = (yes_pct + 5) / 100
            bet_amount, direction = kelly_size(est_prob, yes_pct / 100, state["bankroll"], params["kelly_fraction"])
            if bet_amount < 5:
                continue
            trades.append({
                "market": mkt["title"], "platform": mkt["platform"], "url": mkt["url"],
                "category": mkt.get("category", "Other"), "direction": "BUY_YES",
                "market_price": yes_pct,
                "estimated_prob": ref["prob"] if ref else round(est_prob * 100, 1),
                "edge_pp": round(edge_pp, 1), "bet_amount": round(bet_amount, 2),
                "kelly_fraction": params["kelly_fraction"],
                "source": ref["source"] if ref else "Tail risk premium model",
                "rationale": f"Long shot at {yes_pct:.0f}%. {'Reference suggests ' + str(ref['prob']) + '% — significantly higher.' if ref else 'Markets systematically underprice tail events.'} Buying YES at {yes_pct:.0f}¢ means risking {yes_pct:.0f}¢ to win {100 - yes_pct:.0f}¢. That's a {(100 - yes_pct)/yes_pct:.1f}x payoff. Using small size (10% Kelly) because most long shots don't hit — but when they do, the asymmetry is powerful.",
                "confidence": "LOW" if not ref else "MEDIUM",
            })
        elif yes_pct >= params["high_threshold"]:
            if ref:
                if ref["prob"] < yes_pct - params["min_mispricing"]:
                    edge_pp = yes_pct - ref["prob"]
                    est_prob = ref["prob"] / 100
                else:
                    continue
            else:
                edge_pp = 5
                est_prob = (yes_pct - 5) / 100
            bet_amount, direction = kelly_size(1 - est_prob, (100 - yes_pct) / 100, state["bankroll"], params["kelly_fraction"])
            if bet_amount < 5:
                continue
            trades.append({
                "market": mkt["title"], "platform": mkt["platform"], "url": mkt["url"],
                "category": mkt.get("category", "Other"), "direction": "BUY_NO",
                "market_price": yes_pct,
                "estimated_prob": ref["prob"] if ref else round(est_prob * 100, 1),
                "edge_pp": round(edge_pp, 1), "bet_amount": round(bet_amount, 2),
                "kelly_fraction": params["kelly_fraction"],
                "source": ref["source"] if ref else "Tail risk premium model",
                "rationale": f"Market at {yes_pct:.0f}% — priced as near-certain. {'But reference suggests only ' + str(ref['prob']) + '%.' if ref else 'Markets systematically overprice near-certainties.'} Buying NO at {100 - yes_pct:.0f}¢ to capture the tail risk premium. If the upset happens, this pays {yes_pct:.0f}¢ on a {100 - yes_pct:.0f}¢ investment. The world is more uncertain than {yes_pct:.0f}% implies.",
                "confidence": "LOW" if not ref else "MEDIUM",
            })
    trades.sort(key=lambda t: -t["edge_pp"])
    return trades[:params["max_positions"]]


STRATEGY_MAP = {
    "contrarian_value": strategy_contrarian_value,
    "cross_platform_arb": strategy_cross_platform_arb,
    "momentum_narrative": strategy_momentum_narrative,
    "statistical_value": strategy_statistical_value,
    "high_conviction": strategy_high_conviction,
    "tail_risk": strategy_tail_risk,
}


# ─── State Management (Supabase + /tmp fallback) ───────────────────────────────────

def load_state():
    # Try Supabase first
    data = sb_read("bots_state.json")
    if data:
        # Also cache locally for fast access within same invocation
        try:
            BOTS_DB.write_text(json.dumps(data))
        except:
            pass
        return data
    # Fallback to /tmp
    if BOTS_DB.exists():
        try:
            return json.loads(BOTS_DB.read_text())
        except:
            pass
    # Initialize fresh
    state = {}
    for bot in BOTS:
        state[bot["id"]] = {
            "bankroll": INITIAL_BANKROLL,
            "total_trades": 0,
            "winning_trades": 0,
            "total_pnl": 0,
            "peak_bankroll": INITIAL_BANKROLL,
            "positions": [],
            "equity_curve": [{"time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "value": INITIAL_BANKROLL}],
        }
    return state

def save_state(state):
    # Write to both Supabase and /tmp
    try:
        BOTS_DB.write_text(json.dumps(state, indent=2))
    except:
        pass
    sb_write("bots_state.json", state)

def load_trades():
    # Try Supabase first
    data = sb_read("bots_trades.json")
    if data and isinstance(data, list):
        try:
            TRADES_DB.write_text(json.dumps(data))
        except:
            pass
        return data
    # Fallback to /tmp
    if TRADES_DB.exists():
        try:
            return json.loads(TRADES_DB.read_text())
        except:
            pass
    return []

def save_trades(trades):
    try:
        TRADES_DB.write_text(json.dumps(trades, indent=2))
    except:
        pass
    sb_write("bots_trades.json", trades)


# ─── Main Engine ───────────────────────────────────────────────────────────────────────

def run_bot_engine():
    now = datetime.now(timezone.utc)
    current_hour = now.strftime("%Y-%m-%dT%H")
    
    # Try loading cache from /tmp first, then Supabase
    cache = None
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
        except:
            pass
    if not cache:
        cache = sb_read("bots_market_cache.json")
    
    if cache and time.time() - cache.get("timestamp", 0) < CACHE_TTL:
        poly_markets = cache.get("poly_markets", [])
        kalshi_markets = cache.get("kalshi_markets", [])
    else:
        poly_markets = fetch_polymarket_markets()
        kalshi_markets = fetch_kalshi_markets()
        cache_data = {"timestamp": time.time(), "poly_markets": poly_markets, "kalshi_markets": kalshi_markets}
        try:
            CACHE_FILE.write_text(json.dumps(cache_data))
        except:
            pass
        sb_write("bots_market_cache.json", cache_data)
    
    all_markets = poly_markets + kalshi_markets
    state = load_state()
    all_trades = load_trades()
    bot_results = []
    new_trades = []
    
    for bot in BOTS:
        bot_state = state.get(bot["id"], {
            "bankroll": INITIAL_BANKROLL, "total_trades": 0, "winning_trades": 0,
            "total_pnl": 0, "peak_bankroll": INITIAL_BANKROLL, "positions": [],
            "equity_curve": [{"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": INITIAL_BANKROLL}],
        })
        
        strategy_fn = STRATEGY_MAP.get(bot["strategy"])
        if not strategy_fn:
            continue
        
        try:
            proposed_trades = strategy_fn(bot, all_markets, poly_markets, kalshi_markets, bot_state)
        except Exception as e:
            proposed_trades = []
        
        existing_market_keys = set()
        for pos in bot_state.get("positions", []):
            existing_market_keys.add(normalize_title(pos.get("market", "")))
        
        executed_trades = []
        for trade in proposed_trades:
            trade_key = normalize_title(trade["market"])
            if trade_key in existing_market_keys:
                continue
            current_positions = len(bot_state.get("positions", []))
            max_pos = bot["params"].get("max_positions", 5)
            if current_positions + len(executed_trades) >= max_pos:
                break
            slipped_entry = apply_slippage(trade["market_price"], trade["direction"])
            trade_record = {
                "id": hashlib.md5(f"{bot['id']}:{trade['market']}:{now.isoformat()}".encode()).hexdigest()[:12],
                "bot_id": bot["id"], "bot_name": bot["name"],
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "market": trade["market"], "platform": trade["platform"],
                "url": trade.get("url", ""), "category": trade.get("category", "Other"),
                "direction": trade["direction"],
                "entry_price": round(slipped_entry, 2),
                "estimated_prob": trade["estimated_prob"],
                "edge_pp": trade["edge_pp"], "bet_amount": trade["bet_amount"],
                "kelly_fraction": trade["kelly_fraction"], "source": trade["source"],
                "rationale": trade["rationale"], "confidence": trade["confidence"],
                "status": "OPEN", "pnl": 0,
                "arb_detail": trade.get("arb_detail"),
            }
            executed_trades.append(trade_record)
            bot_state["bankroll"] -= trade["bet_amount"]
            bot_state["total_trades"] += 1
            bot_state["positions"].append({
                "trade_id": trade_record["id"], "market": trade["market"],
                "direction": trade["direction"], "entry_price": round(slipped_entry, 2),
                "current_price": trade["market_price"], "bet_amount": trade["bet_amount"],
                "timestamp": trade_record["timestamp"], "platform": trade["platform"],
                "url": trade.get("url", ""),
            })
        
        # Update current prices — EXACT title match only
        for pos in bot_state.get("positions", []):
            pos_title_lower = pos["market"].strip().lower()
            exact_match = None
            for mkt in all_markets:
                if mkt["title"].strip().lower() == pos_title_lower:
                    exact_match = mkt
                    break
            if exact_match:
                new_price = exact_match["yes_pct"]
                old_price = pos["current_price"]
                if abs(new_price - old_price) <= 15:
                    pos["current_price"] = new_price
        
        # Calculate unrealized P&L
        unrealized_pnl = 0
        for pos in bot_state.get("positions", []):
            entry = pos["entry_price"]
            current = pos["current_price"]
            bet = pos["bet_amount"]
            if entry <= 0 or entry >= 100 or current <= 0:
                continue
            if pos["direction"] == "BUY_YES":
                exit_price = current * (1 - SLIPPAGE_PCT / 100)
                shares = bet / (entry / 100)
                current_value = shares * (exit_price / 100)
                pnl = current_value - bet
                unrealized_pnl += pnl
            elif pos["direction"] == "BUY_NO":
                no_entry = 100 - entry
                no_current = 100 - current
                if no_entry <= 0:
                    continue
                exit_price = no_current * (1 - SLIPPAGE_PCT / 100)
                shares = bet / (no_entry / 100)
                current_value = shares * (exit_price / 100)
                pnl = current_value - bet
                unrealized_pnl += pnl
            elif pos["direction"] == "ARB":
                if pos.get("arb_detail"):
                    raw_spread = pos["arb_detail"]["spread"] / 100
                    net_spread = raw_spread - (2 * FEES_PCT / 100)
                    unrealized_pnl += bet * max(net_spread, 0)
        
        current_equity = bot_state["bankroll"] + sum(p["bet_amount"] for p in bot_state.get("positions", [])) + unrealized_pnl
        bot_state["peak_bankroll"] = max(bot_state.get("peak_bankroll", INITIAL_BANKROLL), current_equity)
        
        equity_curve = bot_state.get("equity_curve", [])
        equity_curve.append({"time": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": round(current_equity, 2)})
        if len(equity_curve) > 168:
            equity_curve = equity_curve[-168:]
        bot_state["equity_curve"] = equity_curve
        
        win_rate = (bot_state["winning_trades"] / bot_state["total_trades"] * 100) if bot_state["total_trades"] > 0 else 0
        
        state[bot["id"]] = bot_state
        new_trades.extend(executed_trades)
        
        bot_results.append({
            **{k: v for k, v in bot.items() if k != "params"},
            "bankroll": round(bot_state["bankroll"], 2),
            "total_equity": round(current_equity, 2),
            "total_trades": bot_state["total_trades"],
            "winning_trades": bot_state["winning_trades"],
            "win_rate": round(win_rate, 1),
            "total_pnl": round(bot_state.get("total_pnl", 0), 2),
            "unrealized_pnl": round(unrealized_pnl, 2),
            "return_pct": round((current_equity - INITIAL_BANKROLL) / INITIAL_BANKROLL * 100, 2),
            "peak_bankroll": round(bot_state.get("peak_bankroll", INITIAL_BANKROLL), 2),
            "drawdown_pct": round((bot_state.get("peak_bankroll", INITIAL_BANKROLL) - current_equity) / bot_state.get("peak_bankroll", INITIAL_BANKROLL) * 100, 2) if bot_state.get("peak_bankroll", INITIAL_BANKROLL) > 0 else 0,
            "open_positions": len(bot_state.get("positions", [])),
            "positions": bot_state.get("positions", []),
            "equity_curve": bot_state.get("equity_curve", []),
            "new_trades": executed_trades,
            "strategy_params": bot["params"],
        })
    
    save_state(state)
    all_trades.extend(new_trades)
    if len(all_trades) > 500:
        all_trades = all_trades[-500:]
    save_trades(all_trades)
    
    return {
        "timestamp": time.time(),
        "lastUpdated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "lastUpdatedHuman": now.strftime("%d %b %Y %H:%M GMT"),
        "totalMarkets": len(all_markets),
        "polymarketCount": len(poly_markets),
        "kalshiCount": len(kalshi_markets),
        "bots": bot_results,
        "recentTrades": sorted(new_trades + all_trades[-50:], key=lambda t: t["timestamp"], reverse=True)[:50],
        "newTradesCount": len(new_trades),
    }


# ─── Vercel Handler ──────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            action = params.get("action", "run")
            
            result = None
            
            if action == "trades":
                trades = load_trades()
                bot_filter = params.get("bot")
                if bot_filter:
                    trades = [t for t in trades if t["bot_id"] == bot_filter]
                result = {"trades": trades[-100:], "total": len(trades)}
            
            elif action == "bot":
                bot_id = params.get("id")
                if not bot_id:
                    result = {"error": "Missing bot id"}
                else:
                    engine_result = run_bot_engine()
                    bot_data = next((b for b in engine_result["bots"] if b["id"] == bot_id), None)
                    if not bot_data:
                        result = {"error": f"Bot {bot_id} not found"}
                    else:
                        all_trades = load_trades()
                        bot_trades = [t for t in all_trades if t["bot_id"] == bot_id]
                        bot_data["trade_history"] = bot_trades[-50:]
                        result = bot_data
            
            elif action == "reset":
                if BOTS_DB.exists():
                    BOTS_DB.unlink()
                if TRADES_DB.exists():
                    TRADES_DB.unlink()
                sb_delete("bots_state.json")
                sb_delete("bots_trades.json")
                result = {"status": "reset", "message": "All bots reset to $10,000"}
            
            elif action == "debug":
                has_key = bool(SUPABASE_KEY)
                write_ok = sb_write("debug_test.json", {"t": time.time()})
                read_back = sb_read("debug_test.json")
                result = {
                    "supabase_url": SUPABASE_URL,
                    "has_key": has_key,
                    "key_len": len(SUPABASE_KEY) if SUPABASE_KEY else 0,
                    "write_ok": write_ok,
                    "read_back": read_back,
                }
            
            else:
                result = run_bot_engine()
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e), "traceback": traceback.format_exc()}).encode())
    
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
