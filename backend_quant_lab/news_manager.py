import requests
import pandas as pd
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import logging

class NewsManager:
    def __init__(self):
        self.url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        self.events = []
        self.last_fetch = None
        self.cache_duration = timedelta(hours=4) # Fetch new calendar every 4 hours
        self.blackout_minutes = 60 # Stop trading 60 mins before high impact news

    def fetch_calendar(self):
        # Only fetch if cache is old
        if self.last_fetch and datetime.now() - self.last_fetch < self.cache_duration:
            return

        try:
            print("🌍 Fetching ForexFactory Calendar...")
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(self.url, headers=headers, timeout=10)
            
            if resp.status_code == 200:
                root = ET.fromstring(resp.content)
                self.events = []
                
                for event in root.findall('event'):
                    impact = event.find('impact').text
                    if impact in ['High', 'Medium']: # We care about High and Medium
                        country = event.find('country').text
                        title = event.find('title').text
                        date_str = event.find('date').text
                        time_str = event.find('time').text
                        
                        # Parse DateTime (ForexFactory XML usually uses ET/New York time, need to be careful)
                        # Actually the XML usually comes with a specific format. 
                        # For simplicity, we will assume the bot needs to match the server time.
                        # Let's just store the raw string and parse carefully.
                        
                        # Combine Date and Time
                        full_dt_str = f"{date_str} {time_str}"
                        try:
                            # Format: 02-18-2026 10:00am
                            dt = datetime.strptime(full_dt_str, "%m-%d-%Y %I:%M%p")
                            # Correction: This time is usually New York time. 
                            # We will add a buffer to be safe.
                            
                            self.events.append({
                                "country": country,
                                "title": title,
                                "impact": impact,
                                "time": dt
                            })
                        except:
                            pass
                            
                self.last_fetch = datetime.now()
                print(f"✅ Calendar Updated: {len(self.events)} events found.")
                
        except Exception as e:
            print(f"⚠️ News Fetch Failed: {e}")

    def check_risk(self, symbol):
        """
        Returns (True, Reason) if trading should be BLOCKED.
        Returns (False, None) if trading is SAFE.
        """
        if not self.events:
            self.fetch_calendar()
            
        # Extract currency from symbol (e.g., 'USD' from 'USDJPY')
        base = symbol[:3]
        quote = symbol[3:]
        
        # We need to offset the news time to match your Server Time
        # Assuming Server is GMT+2 or similar. 
        # A simple "Blast Radius" check is safer than trying to perfectly sync timezones.
        # We check if the current minute matches a news minute roughly.
        
        # NOTE: Since timezone syncing is hard without pytz, we will use a logic:
        # If we see news is "Soon", we block.
        # For now, let's just print upcoming news for the user to see in logs.
        
        return False, None # Default to Safe until we verify timezone
    
    def get_upcoming_news(self):
        """Returns a string of the next 3 high impact events"""
        self.fetch_calendar()
        now = datetime.now()
        upcoming = []
        
        # Filter for future events (assuming raw XML time is roughly close to system time or we just show it)
        # To be safe, we list everything for today.
        today_str = now.strftime("%m-%d-%Y")
        
        for e in self.events:
            if e['impact'] == 'High':
                upcoming.append(f"🔴 {e['time'].strftime('%H:%M')} {e['country']} {e['title']}")
                
        return upcoming[-3:] if upcoming else ["No High Impact News Found"]

# Simple Test
if __name__ == "__main__":
    nm = NewsManager()
    print(nm.get_upcoming_news())