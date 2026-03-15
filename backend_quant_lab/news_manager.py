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

logger = logging.getLogger("Kom_News")

class NewsManager:
    def __init__(self):
        self.events = []
        self.last_fetch = None
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36'
        }
        # [Sprint 14] Tier 1 events require a massive 4-hour pre-event blackout.
        self.tier_1_keywords = ['CPI', 'FOMC', 'NFP', 'Non-Farm', 'Rate Decision', 'Interest Rate', 'GDP', 'Chair Powell Speaks']

    def fetch_calendar(self):
        """
        Scrapes ForexFactory for high/medium impact events.
        Implements Bug-68 Fix: Stateful inheritance of Time and Currency for grouped rows.
        """
        now = datetime.utcnow()
        # [S18c] Hardened cache: 1 hour to prevent 403 IP bans from ForexFactory
        if self.last_fetch and (now - self.last_fetch).total_seconds() < 3600:
            return self.events

        try:
            url = "https://www.forexfactory.com/calendar"
            response = requests.get(url, headers=self.headers, timeout=15)
            
            if response.status_code == 403:
                logger.error("🚫 News fetch failed: HTTP 403 (IP Blocked). Wait 10-15 minutes.")
                return self.events
                
            if response.status_code != 200:
                logger.error(f"News fetch failed: HTTP {response.status_code}")
                return self.events

            soup = BeautifulSoup(response.content, 'html.parser')
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

            self.events = parsed_events
            self.last_fetch = now
            logger.info(f"✅ News Pipeline: {len(self.events)} events synced (Stateful Parsing Active).")
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

        now = now or datetime.utcnow()

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
        [RESTORED S18a] Returns a list of high/medium impact events 
        occurring within the next X hours.
        """
        if not self.events:
            self.fetch_calendar()
            
        now = datetime.utcnow()
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