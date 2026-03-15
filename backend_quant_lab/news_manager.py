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
#   - [Sprint 11] Timezone normalization.
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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        # [Sprint 14] Tier 1 events require a massive 4-hour pre-event blackout.
        self.tier_1_keywords = ['CPI', 'FOMC', 'NFP', 'Non-Farm', 'Rate Decision', 'Interest Rate', 'GDP']

    def fetch_calendar(self):
        now = datetime.utcnow()
        # Cache limit: 1 hour to prevent IP banning from ForexFactory
        if self.last_fetch and (now - self.last_fetch).total_seconds() < 3600:
            return self.events

        try:
            url = "https://www.forexfactory.com/calendar"
            response = requests.get(url, headers=self.headers, timeout=10)
            if response.status_code != 200:
                logger.error(f"News fetch failed: HTTP {response.status_code}")
                return self.events

            soup = BeautifulSoup(response.content, 'html.parser')
            table = soup.find('table', class_='calendar__table')
            if not table:
                return self.events

            parsed_events = []
            
            # [SPRINT 18c / BUG-68 FIX] Initialize stateful trackers for grouped events
            current_time = "All Day"
            current_currency = "??"

            for row in table.find_all('tr', class_='calendar__row'):
                # Skip date headers and empty structural rows
                if 'calendar__row--new-day' in row.get('class', []):
                    continue

                # 1. Parse Time (Inherit if blank)
                time_td = row.find('td', class_='calendar__time')
                if time_td:
                    t_str = time_td.text.strip()
                    if t_str and t_str != "":
                        current_time = t_str

                # 2. Parse Currency/Country (Inherit if blank)
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
                    continue # We only care about High/Medium impact events

                # 4. Parse Title
                event_td = row.find('td', class_='calendar__event')
                event_title = event_td.text.strip() if event_td else "Unknown Event"

                # 5. Tier Classification
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
            logger.info(f"✅ Calendar Updated: {len(self.events)} events ({len([e for e in self.events if e['tier']==1])} Tier-1).")
            return self.events

        except Exception as e:
            logger.error(f"News parsing error: {e}")
            return self.events

    def _parse_event_time(self, time_str, utc_now):
        """
        [SPRINT 11] Parses ForexFactory string (e.g., '8:30am') into 
        a strict UTC datetime object for the current day.
        """
        try:
            if not time_str or time_str == "All Day" or time_str == "Tentative":
                return None
                
            # Clean up FF time strings (they use 'am' / 'pm' without spaces)
            t_clean = time_str.lower().replace('am', ' AM').replace('pm', ' PM')
            t_clean = re.sub(r'\s+', ' ', t_clean).strip().upper()
            dt_time = datetime.strptime(t_clean, '%I:%M %p').time()
            
            event_dt = datetime.combine(utc_now.date(), dt_time)
            return event_dt
        except Exception:
            return None

    def is_news_window(self, now=None):
        """
        [BUG-25 FIX] Directly uses the pre-parsed _event_dt objects.
        Tier 1: 4 hours before, 30 mins after.
        Tier 2: 15 mins before, 15 mins after.
        Returns (True, reason) if blocked, else (False, "").
        """
        if not self.events:
            return False, ""

        if now is None:
            now = datetime.utcnow()

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
        [RESTORED] Returns a list of high/medium impact events occurring 
        within the next X hours. Used by the Telegram /news command 
        and the Frontend Dashboard.
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
            
            # If the event is in the future and within the requested window
            if 0 <= time_diff <= (hours * 3600):
                upcoming.append(ev)
                
        return upcoming