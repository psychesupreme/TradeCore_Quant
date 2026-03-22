# ============================================================
# Kom v1.0 (formerly TradeCore) — news_manager.py
# [SPRINT 18a: REBRAND] | [SPRINT 18c: FOREXFACTORY PARSER FIX]
#
# SPRINT 18c BUG FIX:
#   - [BUG-68] Fixed the "🔴 ?? - High" UI display error. 
#     ForexFactory leaves the 'currency' and 'time' HTML table 
#     cells blank for grouped events occurring at the exact same 
#     time. The scraper now remembers the 'last_seen_currency' 
#     and 'last_seen_time' to ensure all grouped events inherit 
#     the correct asset tags.
#
# HISTORICAL PRESERVATION (Sprints 10-17):
#   - [Sprint 10] Master News Guard implementation. Fetches 
#     high/medium impact events to pause trading algorithms.
#   - [Sprint 11] Timezone normalization to strict UTC.
#   - [Sprint 14] Tier-1 vs Tier-2 classification. Tier-1 
#     (NFP, CPI, FOMC) creates a 4-hour pre-event blackout.
#     Tier-2 (Standard High Impact) creates a 15-min blackout.
#   - [BUG-25] Fixed datetime parsing in is_news_window() 
#     allowing bot_engine to correctly read the event times.
# ============================================================

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import time
import logging
import re
import json
import os

logger = logging.getLogger("Kom_News")

# [S24-NEWS] Disk cache path — survives restarts so StatReload / server
# restarts never hit ForexFactory again within the same calendar day.
_CACHE_PATH = "logs/news_cache.json"
# On 403, back off for 2 hours rather than retrying on the next restart.
_CACHE_TTL_NORMAL  = 3600     # 1 hour on success
_CACHE_TTL_BLOCKED = 7200     # 2 hours after 403


class NewsManager:
    def __init__(self):
        self.events      = []
        self.last_fetch  = None
        self._blocked_until = None   # set when ForexFactory returns 403
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/120.0.0.0 Safari/537.36'
        }
        self.tier_1_keywords = [
            'CPI', 'FOMC', 'NFP', 'Non-Farm', 'Rate Decision',
            'Interest Rate', 'GDP', 'Chair Powell Speaks'
        ]
        # [S24-NEWS] Warm the in-memory cache from disk on startup so the
        # bot never makes a live HTTP request immediately after a restart.
        self._load_disk_cache()

    # ── DISK CACHE ────────────────────────────────────────────────────────

    def _load_disk_cache(self):
        """Load the last successful calendar from disk."""
        try:
            if not os.path.exists(_CACHE_PATH):
                return
            with open(_CACHE_PATH, 'r') as f:
                data = json.load(f)
            cached_at = datetime.fromisoformat(data.get('cached_at', '2000-01-01'))
            age = (datetime.now(timezone.utc).replace(tzinfo=None) - cached_at).total_seconds()
            # Only use cache if it's less than 12 hours old
            if age < 43200:
                raw_events = data.get('events', [])
                # Re-parse _event_dt strings back to datetime objects
                for ev in raw_events:
                    edt_str = ev.pop('_event_dt_iso', None)
                    ev['_event_dt'] = datetime.fromisoformat(edt_str) if edt_str else None
                self.events     = raw_events
                self.last_fetch = cached_at
                logger.info(
                    f"📰 News: Loaded {len(self.events)} events from disk cache "
                    f"(age: {age/3600:.1f}h)."
                )
        except Exception as e:
            logger.debug(f"News disk cache load error: {e}")

    def _save_disk_cache(self):
        """Persist the current calendar to disk."""
        try:
            os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
            # Serialize datetime objects to ISO strings for JSON
            serializable = []
            for ev in self.events:
                ev_copy = dict(ev)
                edt = ev_copy.pop('_event_dt', None)
                ev_copy['_event_dt_iso'] = edt.isoformat() if edt else None
                serializable.append(ev_copy)
            with open(_CACHE_PATH, 'w') as f:
                json.dump({
                    'cached_at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                    'events':    serializable,
                }, f)
        except Exception as e:
            logger.debug(f"News disk cache save error: {e}")

    def fetch_calendar(self):
        """
        Scrapes ForexFactory for high/medium impact events.
        [S24-NEWS] Disk-cached: restarts never trigger a live request if
        the on-disk cache is still fresh. 403 responses set a 2-hour
        backoff so repeated restarts don't burn through the IP quota.
        [BUG-68] Stateful inheritance of Time and Currency for grouped rows.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # [S24-NEWS] Honour the 403 backoff window
        if self._blocked_until and now < self._blocked_until:
            mins_left = int((self._blocked_until - now).total_seconds() / 60)
            logger.debug(f"📰 News: 403 backoff active — {mins_left}m remaining.")
            return self.events

        # In-memory TTL (falls back to disk cache age on startup)
        if self.last_fetch:
            age = (now - self.last_fetch).total_seconds()
            if age < _CACHE_TTL_NORMAL:
                return self.events

        try:
            url = "https://www.forexfactory.com/calendar"
            response = requests.get(url, headers=self.headers, timeout=15)

            if response.status_code == 403:
                self._blocked_until = now + timedelta(seconds=_CACHE_TTL_BLOCKED)
                # Update last_fetch so in-memory TTL also backs off
                self.last_fetch = now
                logger.error(
                    f"🚫 News: HTTP 403 (IP blocked). "
                    f"Next attempt at {self._blocked_until.strftime('%H:%M')} UTC."
                )
                return self.events

            if response.status_code != 200:
                logger.error(f"News fetch failed: HTTP {response.status_code}")
                return self.events

            soup  = BeautifulSoup(response.content, 'html.parser')
            table = soup.find('table', class_='calendar__table')
            if not table:
                logger.warning("ForexFactory table structure not found.")
                return self.events

            parsed_events = []
            
            # [BUG-68 FIX] Initialize stateful trackers for grouped rows
            current_time = "All Day"
            current_currency = "??"

            for row in table.find_all('tr', class_='calendar__row'):
                # Skip date headers and structural padding
                if 'calendar__row--new-day' in row.get('class', []):
                    continue

                # 1. Parse Time (Inherit if cell is blank)
                time_td = row.find('td', class_='calendar__time')
                if time_td:
                    t_str = time_td.text.strip()
                    if t_str and t_str != "":
                        current_time = t_str

                # 2. Parse Currency (Inherit if cell is blank)
                curr_td = row.find('td', class_='calendar__currency')
                if curr_td:
                    c_str = curr_td.text.strip()
                    if c_str and c_str != "":
                        current_currency = c_str

                # 3. Parse Impact
                impact_td = row.find('td', class_='calendar__impact')
                if not impact_td:
                    continue

                impact_span = impact_td.find('span')
                impact_class = impact_span.get('class', [''])[0] if impact_span else ""
                
                impact_level = "Low"
                if 'high' in impact_class:
                    impact_level = "High"
                elif 'medium' in impact_class:
                    impact_level = "Medium"
                else:
                    continue # Skip Low impact and Non-economic events

                # 4. Parse Title
                event_td = row.find('td', class_='calendar__event')
                event_title = event_td.text.strip() if event_td else "Unknown Event"

                # 5. Tier Classification (Sprint 14)
                tier = 2
                if impact_level == 'High':
                    for kw in self.tier_1_keywords:
                        if kw.lower() in event_title.lower():
                            tier = 1
                            break

                # 6. Time Parsing to Python Datetime (UTC)
                event_dt = self._parse_event_time(current_time, now)

                parsed_events.append({
                    'country': current_currency,
                    'impact': impact_level,
                    'title': event_title,
                    'time': current_time,
                    'tier': tier,
                    '_event_dt': event_dt
                })

            self.events     = parsed_events
            self.last_fetch = now
            self._blocked_until = None   # clear any prior backoff on success
            # [S24-NEWS] Persist to disk so next restart uses cached data
            self._save_disk_cache()
            logger.info(
                f"✅ News Pipeline: {len(self.events)} events synced "
                f"(Stateful Parsing · Disk Cached)."
            )
            return self.events

        except Exception as e:
            logger.error(f"News parsing error: {e}")
            return self.events

    def _parse_event_time(self, time_str, utc_now):
        """
        [SPRINT 11] Parses ForexFactory string (e.g., '8:30am') into 
        a strict UTC datetime object.
        """
        try:
            if not time_str or time_str in ["All Day", "Tentative"]:
                return None
                
            # Normalize format for strptime
            t_clean = time_str.lower().replace('am', ' AM').replace('pm', ' PM')
            t_clean = re.sub(r'\s+', ' ', t_clean).strip().upper()
            
            dt_time = datetime.strptime(t_clean, '%I:%M %p').time()
            return datetime.combine(utc_now.date(), dt_time)
        except Exception:
            return None

    def is_news_window(self, now=None):
        """
        [BUG-25 FIX] Prevents limit placement during volatile windows.
        Tier 1: 4h pre-event / 30m post-event blackout.
        Tier 2: 15m pre-event / 15m post-event blackout.
        """
        if not self.events:
            return False, ""

        now = now or datetime.now(timezone.utc).replace(tzinfo=None)

        for ev in self.events:
            if ev['impact'] != 'High':
                continue

            edt = ev.get('_event_dt')
            if not edt:
                continue

            # Calculate mathematical blackout windows
            if ev['tier'] == 1:
                window_start = edt - timedelta(hours=4)
                window_end   = edt + timedelta(minutes=30)
            else:
                window_start = edt - timedelta(minutes=15)
                window_end   = edt + timedelta(minutes=15)

            if window_start <= now <= window_end:
                return True, f"{ev['country']} {ev['title']} (Tier {ev['tier']})"

        return False, ""

    def get_upcoming_news(self, hours=12):
        """
        [RESTORED S18a] Returns high/medium impact events in next X hours.
        [S28] Weekend awareness: returns sentinel during market closure.
        [S28] Tier field added to all events for dashboard display.
        """
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        dow = now.weekday()  # 0=Mon, 5=Sat, 6=Sun
        t   = now.hour * 60 + now.minute

        # Weekend: FX markets closed Fri 22:00 → Sun 22:00 UTC
        _is_weekend = (dow == 5) or (dow == 6) or                       (dow == 4 and t >= 22*60) or                       (dow == 6 and t < 22*60)

        if _is_weekend:
            # On weekends, don't burn a fetch — return market status info
            # Only BTC/ETH are active; next event will be Sunday 22:00 open
            dow_names = {4:'Friday', 5:'Saturday', 6:'Sunday', 0:'Monday'}
            _day = dow_names.get(dow, 'Weekend')
            return [{
                'title':   'FX Markets Closed — Crypto Only',
                'country': '🌐',
                'impact':  'Info',
                'tier':    0,
                'time':    now.strftime('%Y-%m-%d %H:%M'),
                '_weekend': True,
            }]

        if not self.events:
            self.fetch_calendar()
            
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        upcoming = []
        
        for ev in self.events:
            edt = ev.get('_event_dt')
            if not edt:
                continue
                
            time_diff = (edt - now).total_seconds()
            
            # Filter for future events within the window
            if 0 <= time_diff <= (hours * 3600):
                upcoming.append(ev)
                
        return upcoming