"""
Prediction Market Arbitrage Scanner — Vercel Serverless Function
"""

import json
import os
import sys
import time
import re
import traceback
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler

# ─── Config ───────────────────────────────────────────────────────────────────────────

POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

CACHE_FILE = Path("/tmp/cache_data.json")
CACHE_TTL = 3500  # ~58 minutes

# Supabase Storage for persistent cache
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://kipjcmqlxkohtbghlicf.supabase.co").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
SCANNER_BUCKET = "scanner-cache"

def _sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

def sb_read(filename):
    if not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/storage/v1/object/{SCANNER_BUCKET}/{filename}"
    req = urllib.request.Request(url, headers=_sb_headers())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except:
        return None

def sb_write(filename, data):
    if not SUPABASE_KEY:
        return False
    payload = json.dumps(data).encode()
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/octet-stream",
        "x-upsert": "true",
    }
    url = f"{SUPABASE_URL}/storage/v1/object/{SCANNER_BUCKET}/{filename}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True
    except:
        return False

# ─── Helpers ────────────────────────────────────────────────────────────────────────

def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "PredictionDashboard/1.0", "Accept": "application/json"})
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

# ─── Polymarket Fetcher ────────────────────────────────────────────────────────────

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
                markets.append({
                    "platform": "Polymarket",
                    "event_title": event.get("title", ""),
                    "event_slug": event.get("slug", ""),
                    "market_title": m.get("question", m.get("title", "")),
                    "market_slug": m.get("slug", ""),
                    "outcomes": m.get("outcomes", "[]"),
                    "outcome_prices": m.get("outcomePrices", "[]"),
                    "volume": safe_float(m.get("volume", 0), 0),
                    "volume_24hr": safe_float(event.get("volume24hr", 0), 0),
                    "liquidity": safe_float(m.get("liquidity", 0), 0),
                    "end_date": m.get("endDate", event.get("endDate", "")),
                    "category": event.get("tags", [{}])[0].get("label", "Other") if event.get("tags") else guess_category(event.get("title", "")),
                    "condition_id": m.get("conditionId", ""),
                    "active": m.get("active", True),
                    "closed": m.get("closed", False),
                    "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                })
    return markets

# ─── Kalshi Price Extraction ────────────────────────────────────────────────────────────

def _normalize_to_cents(val):
    """Normalize a price value to cents [0-100]. Values <= 1.0 are treated as dollars."""
    if val is None:
        return None
    v = safe_float(val, None)
    if v is None:
        return None
    if 0 < v <= 1.0:
        v = v * 100
    return v

def extract_kalshi_yes_pct(market):
    """Extract yes probability (in cents, 0-100) from a Kalshi market dict.

    Fallback chain:
      1. yes_price (if non-zero)
      2. midpoint of yes_bid + yes_ask (if both non-zero)
      3. single-sided: yes_ask alone (if yes_bid is zero/missing)
      4. single-sided: yes_bid alone (if yes_ask is zero/missing)
      5. last_price
      6. dollar-denominated variants (yes_price_dollar, last_price_dollar)

    Returns a value in (0, 100] or None if no valid price found.
    """
    # 1. yes_price
    yes_price = _normalize_to_cents(market.get("yes_price"))
    if yes_price is not None and yes_price > 0:
        if yes_price > 100:
            return None
        return yes_price

    # 2-4. bid/ask (with one-sided fallback)
    yes_bid = _normalize_to_cents(market.get("yes_bid"))
    yes_ask = _normalize_to_cents(market.get("yes_ask"))
    bid_ok = yes_bid is not None and yes_bid > 0
    ask_ok = yes_ask is not None and yes_ask > 0

    if bid_ok and ask_ok:
        mid = (yes_bid + yes_ask) / 2
        if 0 < mid <= 100:
            return mid
    elif ask_ok:
        if 0 < yes_ask <= 100:
            return yes_ask
    elif bid_ok:
        if 0 < yes_bid <= 100:
            return yes_bid

    # 5. last_price
    last = _normalize_to_cents(market.get("last_price"))
    if last is not None and 0 < last <= 100:
        return last

    # 6. dollar-denominated explicit fields
    for field in ("yes_price_dollar", "last_price_dollar"):
        raw = safe_float(market.get(field), None)
        if raw is not None and raw > 0:
            cents = raw * 100
            if 0 < cents <= 100:
                return cents

    return None


# ─── Kalshi Fetcher ─────────────────────────────────────────────────────────────────────

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
            yes_pct = extract_kalshi_yes_pct(m)
            if yes_pct is None or yes_pct <= 0:
                continue
            markets.append({
                "platform": "Kalshi",
                "event_title": m.get("event_ticker", ""),
                "market_title": m.get("title", ""),
                "ticker": m.get("ticker", ""),
                "event_ticker": m.get("event_ticker", ""),
                "yes_price": round(yes_pct, 1),
                "volume": safe_float(m.get("volume", 0), 0),
                "open_interest": safe_float(m.get("open_interest", 0), 0),
                "category": m.get("category", "Other"),
                "end_date": m.get("close_time", m.get("expiration_time", "")),
                "subtitle": m.get("subtitle", ""),
                "url": f"https://kalshi.com/markets/{m.get('ticker', '').lower()}",
            })
        cursor = data.get("cursor", "")
        if not cursor:
            break
    return markets

# ─── Category Guesser ─────────────────────────────────────────────────────────────────────

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

# ─── Cross-Platform Matching ─────────────────────────────────────────────────────────

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

# ─── Reference Probabilities ──────────────────────────────────────────────────────────

REFERENCE_PROBS = [
    {"id": "fed_march_nochange", "prob": 96, "source": "CME FedWatch Tool", "url": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
     "require_all": ["fed", "march"], "require_any": ["no change", "hold", "maintain", "interest rate"], "exclude": ["chair", "nominate", "powell", "warsh"]},
    {"id": "fed_april_nochange", "prob": 82, "source": "CME FedWatch Tool", "url": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
     "require_all": ["fed", "april"], "require_any": ["no change", "hold", "maintain", "interest rate"], "exclude": ["chair", "nominate"]},
    {"id": "fed_rate_cuts_2026", "prob": 47, "source": "CME FedWatch Tool", "url": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
     "require_all": ["fed", "rate", "cut"], "require_any": ["2026", "june"], "exclude": ["chair", "nominate"]},
    {"id": "recession_2026", "prob": 30, "source": "RSM/JPMorgan consensus", "url": "https://rsmus.com/insights/economics/economic-outlook-for-2026.html",
     "require_all": ["recession"], "require_any": ["2026", "us", "united states"], "exclude": ["global", "china", "europe"]},
    {"id": "dem_house_2026", "prob": 85, "source": "Polymarket/polling consensus", "url": "https://polymarket.com/event/which-party-will-win-the-house-in-2026",
     "require_all": ["house"], "require_any": ["democrat", "democratic party", "2026 midterm"], "exclude": ["senate", "balance of power", "white house"]},
    {"id": "china_taiwan_invade", "prob": 4, "source": "ASPI/Stimson/CFR analysts", "url": "https://polymarket.com/event/will-china-invade-taiwan-by-june-30-2026",
     "require_all": ["china", "taiwan"], "require_any": ["invade", "invasion", "attack", "blockade"], "exclude": ["gta", "before gta"]},
    {"id": "ukraine_ceasefire", "prob": 36, "source": "Polymarket/Brookings consensus", "url": "https://polymarket.com/event/russia-x-ukraine-ceasefire-before-2027",
     "require_all": ["ceasefire"], "require_any": ["ukraine", "russia"], "exclude": ["israel", "iran", "gaza", "gta", "eurovision", "broken"]},
    {"id": "arsenal_epl", "prob": 83, "source": "OddsChecker (-500 implied)", "url": "https://www.oddschecker.com",
     "require_all": ["arsenal", "premier league"], "require_any": [], "exclude": ["champions league", "ucl", "fa cup"]},
    {"id": "f1_russell", "prob": 33, "source": "Ladbrokes/BoyleSports (2/1)", "url": "https://destinationformula1.com",
     "require_all": ["russell"], "require_any": ["f1", "formula", "drivers"], "exclude": []},
    {"id": "okc_nba", "prob": 30, "source": "Yahoo Sports/Covers (+230)", "url": "https://sports.yahoo.com",
     "require_all": ["thunder"], "require_any": ["nba", "champion"], "exclude": []},
    {"id": "us_iran_strike", "prob": 78, "source": "Polymarket consensus (live)", "url": "https://polymarket.com",
     "require_all": ["strike", "iran"], "require_any": ["us", "united states", "israel"], "exclude": ["next strike on", "february 2", "february 1", "february 3", "ceasefire broken"]},
    {"id": "khamenei_out", "prob": 12, "source": "Expert consensus (age-adjusted)", "url": "https://kalshi.com",
     "require_all": ["khamenei"], "require_any": ["out", "supreme leader", "leave"], "exclude": []},
    {"id": "openai_ipo", "prob": 50, "source": "Analyst reports", "url": "https://polymarket.com/event/openai-ipo-by",
     "require_all": ["openai", "ipo"], "require_any": [], "exclude": ["market cap", "closing", "less than"]},
    {"id": "spain_world_cup", "prob": 20, "source": "BetMGM (+400)", "url": "https://www.vegasinsider.com/soccer/odds/world-cup/",
     "require_all": ["spain", "world cup"], "require_any": [], "exclude": []},
    {"id": "warsh_fed_chair", "prob": 93, "source": "Polymarket/Kalshi consensus", "url": "https://polymarket.com",
     "require_all": ["warsh"], "require_any": ["fed chair", "nominate", "federal reserve"], "exclude": ["confirmed", "hassett"]},
    {"id": "oscar_best_picture", "prob": 77, "source": "Awards trackers", "url": "https://polymarket.com",
     "require_all": ["oscar", "best picture"], "require_any": [], "exclude": []},
    {"id": "oscar_best_actor", "prob": 70, "source": "Awards trackers", "url": "https://polymarket.com",
     "require_all": ["oscar"], "require_any": ["best actor", "chalamet"], "exclude": ["supporting", "actress"]},
]

def find_reference(market_title):
    t = market_title.lower()
    for ref in REFERENCE_PROBS:
        if not all(kw in t for kw in ref["require_all"]):
            continue
        if ref["require_any"] and not any(kw in t for kw in ref["require_any"]):
            continue
        if any(kw in t for kw in ref.get("exclude", [])):
            continue
        return ref
    return None

# ─── Discrepancy Analysis ────────────────────────────────────────────────────────────

def analyze_discrepancies(poly_markets, kalshi_markets):
    discrepancies = []
    
    # 1. Cross-platform matching
    matched_pairs = []
    for pm in poly_markets:
        if pm.get("closed") or not pm.get("active"):
            continue
        pm_norm = normalize_title(pm.get("market_title", "") or pm.get("event_title", ""))
        try:
            prices = json.loads(pm.get("outcome_prices", "[]"))
            pm_yes_pct = safe_float(prices[0], 0) * 100 if prices else 0
        except:
            pm_yes_pct = 0
        if pm_yes_pct <= 0:
            continue
        for km in kalshi_markets:
            km_norm = normalize_title(km.get("market_title", ""))
            overlap = keyword_overlap(pm_norm, km_norm)
            if overlap >= 0.5:
                km_yes_pct = km.get("yes_price", 0)
                if km_yes_pct <= 0:
                    continue
                gap = abs(pm_yes_pct - km_yes_pct)
                if gap >= 3:
                    matched_pairs.append({
                        "type": "cross_platform",
                        "market": pm.get("market_title", "") or pm.get("event_title", ""),
                        "category": pm.get("category", guess_category(pm.get("market_title", ""))),
                        "platforms": {
                            "Polymarket": {"price": round(pm_yes_pct, 1), "url": pm.get("url", "")},
                            "Kalshi": {"price": round(km_yes_pct, 1), "url": km.get("url", "")}
                        },
                        "discrepancy_pp": round(gap, 1),
                        "signal": "CROSS-PLATFORM DIVERGENCE" if gap < 10 else "MAJOR CROSS-PLATFORM DIVERGENCE",
                        "higher_platform": "Polymarket" if pm_yes_pct > km_yes_pct else "Kalshi",
                        "liquidity": f"PM: ${pm.get('volume', 0):,.0f} vol, ${pm.get('liquidity', 0):,.0f} liq | Kalshi: {km.get('volume', 0):,.0f} contracts",
                        "volume_24hr": pm.get("volume_24hr", 0) + km.get("volume", 0),
                        "potentialProfit": f"Buy on {'Kalshi' if pm_yes_pct > km_yes_pct else 'Polymarket'} at {min(pm_yes_pct, km_yes_pct):.0f}\u00a2, sell on {'Polymarket' if pm_yes_pct > km_yes_pct else 'Kalshi'} at {max(pm_yes_pct, km_yes_pct):.0f}\u00a2 \u2192 {gap:.0f}\u00a2 spread",
                        "riskFactors": [
                            "Resolution criteria may differ between platforms \u2014 verify before trading",
                            "Cross-platform settlement timing creates capital lockup risk",
                            "Transaction fees and withdrawal costs reduce effective spread",
                            "Polymarket uses USDC (crypto), Kalshi uses USD \u2014 currency friction"
                        ],
                        "severity": "critical" if gap >= 20 else ("high" if gap >= 10 else "medium"),
                        "end_date": pm.get("end_date", ""),
                    })
    
    seen = set()
    unique_pairs = []
    for pair in sorted(matched_pairs, key=lambda x: -x["discrepancy_pp"]):
        key = normalize_title(pair["market"])
        if key not in seen:
            seen.add(key)
            unique_pairs.append(pair)
    discrepancies.extend(unique_pairs[:20])
    
    # 2. Reference probability discrepancies
    all_markets = []
    for pm in poly_markets:
        if pm.get("closed") or not pm.get("active"):
            continue
        try:
            prices = json.loads(pm.get("outcome_prices", "[]"))
            yes_pct = safe_float(prices[0], 0) * 100 if prices else 0
        except:
            yes_pct = 0
        if yes_pct > 0:
            all_markets.append({
                "platform": "Polymarket",
                "title": pm.get("market_title", "") or pm.get("event_title", ""),
                "category": pm.get("category", "Other"),
                "yes_pct": yes_pct,
                "volume": pm.get("volume", 0),
                "volume_24hr": pm.get("volume_24hr", 0),
                "liquidity": pm.get("liquidity", 0),
                "url": pm.get("url", ""),
                "end_date": pm.get("end_date", ""),
            })
    
    for km in kalshi_markets:
        if km.get("yes_price", 0) > 0:
            all_markets.append({
                "platform": "Kalshi",
                "title": km.get("market_title", ""),
                "category": km.get("category", "Other"),
                "yes_pct": km.get("yes_price", 0),
                "volume": km.get("volume", 0),
                "volume_24hr": 0,
                "liquidity": km.get("open_interest", 0),
                "url": km.get("url", ""),
                "end_date": km.get("end_date", ""),
            })
    
    for mkt in all_markets:
        ref = find_reference(mkt["title"])
        if ref:
            gap = mkt["yes_pct"] - ref["prob"]
            abs_gap = abs(gap)
            if abs_gap >= 4:
                if mkt.get("end_date"):
                    try:
                        end = datetime.fromisoformat(mkt["end_date"].replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        if (end - now).total_seconds() < 48 * 3600:
                            continue
                    except:
                        pass
                norm = normalize_title(mkt["title"])
                if any(keyword_overlap(norm, normalize_title(d["market"])) > 0.6 for d in discrepancies):
                    continue
                direction = "OVERPRICED" if gap > 0 else "UNDERPRICED"
                discrepancies.append({
                    "type": "vs_reference",
                    "market": mkt["title"],
                    "category": mkt.get("category", guess_category(mkt["title"])),
                    "platforms": {
                        mkt["platform"]: {"price": round(mkt["yes_pct"], 1), "url": mkt["url"]}
                    },
                    "referenceOdds": ref["prob"],
                    "referenceSource": ref["source"],
                    "referenceUrl": ref["url"],
                    "discrepancy_pp": round(abs_gap, 1),
                    "direction": direction,
                    "signal": f"{direction}" if abs_gap < 10 else f"SIGNIFICANTLY {direction}",
                    "liquidity": f"${mkt.get('volume', 0):,.0f} vol" + (f", ${mkt.get('liquidity', 0):,.0f} liq" if mkt.get("liquidity") else ""),
                    "volume_24hr": mkt.get("volume_24hr", 0),
                    "potentialProfit": f"{'Sell' if gap > 0 else 'Buy'} YES at {mkt['yes_pct']:.0f}\u00a2 vs {ref['prob']}% reference \u2192 ~{abs_gap:.0f}% edge" if abs_gap > 0 else "Near parity",
                    "riskFactors": generate_risk_factors(mkt, ref, gap),
                    "severity": "high" if abs_gap >= 12 else ("medium" if abs_gap >= 6 else "low"),
                    "end_date": mkt.get("end_date", ""),
                })
    
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    discrepancies.sort(key=lambda d: (severity_order.get(d.get("severity", "low"), 4), -d.get("discrepancy_pp", 0)))
    return discrepancies[:30]


def generate_risk_factors(mkt, ref, gap):
    factors = []
    cat = guess_category(mkt["title"])
    if cat == "Geopolitics":
        factors.extend(["Geopolitical events are inherently unpredictable \u2014 tail risks are real", "Information asymmetry: intelligence agencies know more than public markets", "Market may be pricing in scenarios that expert analysis ignores"])
    elif cat == "Politics":
        factors.extend(["Polls can shift significantly before election day", "Turnout models vary widely and drive different probability estimates", "Late-breaking events (scandals, endorsements) can move odds rapidly"])
    elif cat == "Economics":
        factors.extend(["Economic data releases can cause sudden repricing", "Tariff policy changes remain a wild card for all forecasts", "NBER recession dating is backward-looking \u2014 resolution may take months"])
    elif cat == "Sports":
        factors.extend(["Injuries, suspensions, and lineup changes can shift odds overnight", "Single-elimination formats have high variance regardless of skill", "Sharp money on sportsbooks may be more accurate than prediction markets"])
    elif cat == "Tech":
        factors.extend(["AI model releases are unpredictable \u2014 one surprise launch changes everything", "Resolution criteria may differ between platforms", "Fast-moving space \u2014 odds can shift dramatically day-to-day"])
    elif cat == "Entertainment":
        factors.extend(["Awards voting is ongoing \u2014 late momentum can shift results", "Guild award results are strong but imperfect predictors", "Academy voters are famously unpredictable"])
    factors.append(f"Reference probability ({ref['prob']}%) from {ref['source']} \u2014 methodology may differ from market consensus")
    if mkt.get("volume", 0) < 100000:
        factors.append("LOW LIQUIDITY \u2014 thin market means prices may be noisy and hard to execute at displayed price")
    return factors


# ─── Vercel Handler ──────────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            force_refresh = params.get("refresh", "false").lower() == "true"
            
            # Check cache (/tmp first, then Supabase)
            if not force_refresh:
                cache = None
                if CACHE_FILE.exists():
                    try:
                        cache = json.loads(CACHE_FILE.read_text())
                    except:
                        pass
                if not cache:
                    cache = sb_read("cache_data.json")
                if cache:
                    cache_age = time.time() - cache.get("timestamp", 0)
                    if cache_age < CACHE_TTL:
                        cache["fromCache"] = True
                        cache["cacheAge"] = int(cache_age)
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(json.dumps(cache).encode())
                        return
            
            # Fetch fresh data
            errors = []
            try:
                poly_markets = fetch_polymarket_markets()
            except Exception as e:
                poly_markets = []
                errors.append(f"Polymarket fetch error: {str(e)}")
            try:
                kalshi_markets = fetch_kalshi_markets()
            except Exception as e:
                kalshi_markets = []
                errors.append(f"Kalshi fetch error: {str(e)}")
            try:
                discrepancies = analyze_discrepancies(poly_markets, kalshi_markets)
            except Exception as e:
                discrepancies = []
                errors.append(f"Analysis error: {str(e)}\n{traceback.format_exc()}")
            
            total_markets = len(poly_markets) + len(kalshi_markets)
            severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for d in discrepancies:
                sev = d.get("severity", "low")
                severity_counts[sev] = severity_counts.get(sev, 0) + 1
            
            result = {
                "timestamp": time.time(),
                "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lastUpdatedHuman": datetime.now(timezone.utc).strftime("%d %b %Y %H:%M GMT"),
                "totalMarketsAnalyzed": total_markets,
                "polymarketCount": len(poly_markets),
                "kalshiCount": len(kalshi_markets),
                "discrepancies": discrepancies,
                "discrepancyCount": len(discrepancies),
                "severityCounts": severity_counts,
                "errors": errors,
                "fromCache": False,
                "cacheAge": 0,
            }
            
            try:
                CACHE_FILE.write_text(json.dumps(result))
            except:
                pass
            sb_write("cache_data.json", result)
            
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
