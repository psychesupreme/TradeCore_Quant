# ============================================================
# TradeCore v51.0 — news_manager.py
# SPRINT 1 FIXES APPLIED:
#   [BUG-04] Parse datetime objects during fetch (not strings)
#            Stores event_dt for reliable bot_engine comparison
#   [NEW]    Event tier classification (Tier 1 / Tier 2)
#            Tier 1 gets 4-hour pre-event guard in bot_engine
#   [QUALITY] Graceful fallback when time field is empty/None
# ============================================================

import requests
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# dateutil handles all ForexFactory time format variations robustly.
# Install: pip install python-dateutil
try:
    from dateutil import parser as _dateparser
    _DATEUTIL_AVAILABLE = True
except ImportError:
    _DATEUTIL_AVAILABLE = False
    print("⚠️  WARNING: python-dateutil not installed.")
    print("   News guard time comparison will be disabled.")
    print("   Run: pip install python-dateutil")


# ============================================================
# TIER CLASSIFICATION
# Tier 1 = market-moving events. bot_engine will apply a
#           4-hour pre-event block for these.
# Tier 2 = notable but less extreme. 15-minute block applied.
# ============================================================
_TIER1_KEYWORDS = [
    "nfp", "non-farm", "payroll",
    "fomc", "federal funds", "interest rate decision",
    "cpi", "core cpi", "inflation",
    "rate decision", "rate statement",
    "gdp"
]

_TIER2_KEYWORDS = [
    "retail sales", "pmi", "ism",
    "unemployment", "jobless",
    "speech", "testimony",
    "trade balance", "housing"
]


def _classify_tier(title: str) -> int:
    """Returns 1 for Tier 1 events, 2 for Tier 2, 0 if unclassified."""
    t = title.lower()
    if any(kw in t for kw in _TIER1_KEYWORDS):
        return 1
    if any(kw in t for kw in _TIER2_KEYWORDS):
        return 2
    return 2  # default everything to Tier 2 if impact is High/Medium


def _parse_event_dt(date_str: str, time_str: str) -> datetime | None:
    """
    Robustly parses ForexFactory date and time strings into a datetime.

    ForexFactory XML formats observed in the wild:
      date_str : "03-03-2026"  /  "Mar 3, 2026"
      time_str : "2:30pm"  /  "2:30 pm"  /  "All Day"  /  ""  /  None

    Returns None if parsing fails (e.g. "All Day" events).
    """
    if not date_str:
        return None

    # Handle "All Day" or empty time — return noon of that day
    if not time_str or time_str.strip().lower() in ("all day", "tentative", ""):
        try:
            if _DATEUTIL_AVAILABLE:
                return _dateparser.parse(date_str).replace(hour=12, minute=0, second=0)
        except Exception:
            return None
        return None

    full_str = f"{date_str} {time_str}".strip()

    if _DATEUTIL_AVAILABLE:
        try:
            return _dateparser.parse(full_str)
        except Exception:
            pass

    # Manual fallback: try common formats
    for fmt in [
        "%m-%d-%Y %I:%M%p",
        "%m-%d-%Y %I:%M %p",
        "%b %d, %Y %I:%M%p",
        "%b %d, %Y %I:%M %p",
    ]:
        try:
            return datetime.strptime(full_str.lower(), fmt.lower())
        except ValueError:
            continue

    return None  # Parsing failed — guard will be skipped for this event


class NewsManager:
    def __init__(self):
        self.url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        self.events = []
        self.last_fetch = None
        self.cache_duration = timedelta(hours=1)
        # [HF-E] Prevents multiple scheduler/API threads from fetching
        # simultaneously. Without this lock, 4 threads can all pass the
        # `last_fetch is None` check before any one sets it — causing the
        # consecutive duplicate fetches observed in the session logs.
        self._fetch_lock = threading.Lock()

    # ----------------------------------------------------------
    # CEO-LEVEL INSIGHT GENERATOR
    # ----------------------------------------------------------
    def get_impact_analysis(self, title: str, currency: str) -> str:
        """Translates technical news titles into plain-language trading insights."""
        t = title.lower()
        if "cpi" in t or "inflation" in t:
            return "Inflation Data. Expect sharp spikes — direction often reverses after initial move."
        if "payroll" in t or "nfp" in t or "non-farm" in t:
            return "Jobs Report (NFP). Extreme volatility. Market frequently fakes the initial direction."
        if "fomc" in t or ("rate" in t and "decision" in t):
            return "Interest Rate Decision. Trend-defining. Highest single risk event of the month."
        if "federal funds" in t:
            return "Federal Funds Rate. Immediate USD repricing. Stay flat until dust settles."
        if "gdp" in t:
            return "Economic Growth Data. Affects long-term trend strength and risk appetite."
        if "retail" in t:
            return "Consumer Spending. Moderate FX impact. Watch for sustained trend confirmation."
        if "pmi" in t or "ism" in t:
            return "Business Activity Index. Early leading indicator. Can signal trend shifts."
        if "speech" in t or "testimony" in t:
            return "Central Bank Speech. Watch for surprise forward guidance on policy."
        if "unemployment" in t or "jobless" in t:
            return "Jobs Data. Moderate impact. Confirms or contradicts NFP narrative."
        if "trade balance" in t:
            return "Trade Data. Low direct impact but shapes medium-term currency flows."
        return "High Impact Event. Expect elevated volatility. Tighten risk controls."

    # ----------------------------------------------------------
    # CALENDAR FETCH
    # ----------------------------------------------------------
    def fetch_calendar(self, force: bool = False):
        """
        Fetches and parses the ForexFactory XML calendar. Cached for 1 hour.
        [HF-E] Lock prevents concurrent threads from all passing the
        'last_fetch is None' check simultaneously and triggering duplicate
        network fetches (the consecutive cluster fetches seen in session logs).
        """
        # Fast path — no lock overhead when cache is fresh
        if not force and self.last_fetch and (datetime.now() - self.last_fetch) < self.cache_duration:
            return

        # Only one thread may fetch at a time
        if not self._fetch_lock.acquire(blocking=False):
            return  # Another thread is already fetching; skip silently

        try:
            # Double-check inside lock — another thread may have just completed
            if not force and self.last_fetch and (datetime.now() - self.last_fetch) < self.cache_duration:
                return

            print("🌍 Fetching ForexFactory Calendar...")
            headers = {'User-Agent': 'Mozilla/5.0 (compatible; TradeCore/51.0)'}
            resp = requests.get(self.url, headers=headers, timeout=10)

            if resp.status_code != 200:
                print(f"⚠️  Calendar fetch HTTP {resp.status_code}. Using stale cache.")
                return

            root = ET.fromstring(resp.content)
            new_events = []

            for event in root.findall('event'):
                impact_node = event.find('impact')
                if impact_node is None:
                    continue
                impact = impact_node.text

                if impact not in ('High', 'Medium'):
                    continue

                country  = (event.find('country') or ET.Element('x')).text or "??"
                title    = (event.find('title')   or ET.Element('x')).text or "Unknown Event"
                date_str = (event.find('date')    or ET.Element('x')).text or ""
                time_str = (event.find('time')    or ET.Element('x')).text or ""

                event_dt = _parse_event_dt(date_str, time_str)
                tier     = _classify_tier(title)
                insight  = self.get_impact_analysis(title, country)

                new_events.append({
                    "country":   country,
                    "title":     title,
                    "impact":    impact,
                    "tier":      tier,
                    "time":      f"{date_str} {time_str}".strip(),
                    "event_dt":  event_dt,
                    "insight":   insight,
                })

            self.events     = new_events
            self.last_fetch = datetime.now()
            t1 = sum(1 for e in new_events if e['tier'] == 1)
            t2 = sum(1 for e in new_events if e['tier'] == 2)
            print(f"✅ Calendar Updated: {len(new_events)} events ({t1} Tier-1 / {t2} Tier-2).")

        except requests.exceptions.Timeout:
            print("⚠️  News Fetch Timeout — using stale cache.")
        except Exception as e:
            print(f"⚠️  News Fetch Failed: {e}")
        finally:
            self._fetch_lock.release()

    # ----------------------------------------------------------
    # PUBLIC ACCESSORS
    # ----------------------------------------------------------
    def get_upcoming_news(self) -> list:
        """Returns all High/Medium events. Triggers a fetch if cache is stale."""
        self.fetch_calendar()
        return [e for e in self.events if e['impact'] in ('High', 'Medium')]

    def is_news_window(self, now: datetime = None) -> tuple[bool, str]:
        """
        Convenience method: returns (True, reason) if we are currently
        inside any news guard window. Used by bot_engine before processing
        any symbol during broad market-wide events (e.g. FOMC day).
        """
        if now is None:
            now = datetime.now()
        for event in self.events:
            if event['impact'] != 'High':
                continue
            event_dt = event.get('event_dt')
            if event_dt is None:
                continue
            tier = event.get('tier', 2)
            # Tier 1: 4h pre-event + 30min post-event
            # Tier 2: 15min pre-event + 15min post-event
            pre_guard  = timedelta(hours=4)  if tier == 1 else timedelta(minutes=15)
            post_guard = timedelta(minutes=30) if tier == 1 else timedelta(minutes=15)
            if (event_dt - pre_guard) <= now <= (event_dt + post_guard):
                return True, f"{event['country']} {event['title']} (T{tier})"
        return False, ""
