import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

class NewsManager:
    def __init__(self):
        self.url = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
        self.events = []
        self.last_fetch = None
        self.cache_duration = timedelta(hours=1) # Refresh every hour

    def get_impact_analysis(self, title, currency):
        """Translates technical news titles into CEO-level insights."""
        t = title.lower()
        if "cpi" in t or "inflation" in t:
            return "Inflation Data. expect sharp spikes in currency value."
        if "payroll" in t or "employment" in t or "nfp" in t:
            return "Jobs Report. Extreme volatility. Market often fakes direction first."
        if "rate" in t or "statement" in t or "fomc" in t:
            return "Interest Rate Decision. Trend-defining event. Highest Risk."
        if "gdp" in t:
            return "Economic Growth Data. affects long-term trend strength."
        if "retail" in t:
            return "Consumer Spending. Moderate impact on currency strength."
        if "speech" in t:
            return "Central Bank Speech. Watch for surprise comments on policy."
        return "High Impact Event. Increased volatility expected."

    def fetch_calendar(self):
        # Auto-Refresh if cache is old
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
                    if impact in ['High', 'Medium']: 
                        country = event.find('country').text
                        title = event.find('title').text
                        date_str = event.find('date').text
                        time_str = event.find('time').text
                        
                        # Generate the "CEO Insight"
                        insight = self.get_impact_analysis(title, country)

                        # Simple Date Parsing
                        full_dt_str = f"{date_str} {time_str}"
                        
                        self.events.append({
                            "country": country,
                            "title": title,
                            "impact": impact,
                            "time": full_dt_str, # Keep string for simple display
                            "insight": insight   # <--- THE NEW FEATURE
                        })
                            
                self.last_fetch = datetime.now()
                print(f"✅ Calendar Updated: {len(self.events)} events found.")
                
        except Exception as e:
            print(f"⚠️ News Fetch Failed: {e}")

    def get_upcoming_news(self):
        """Returns structured data for the API"""
        self.fetch_calendar()
        return [e for e in self.events if e['impact'] in ['High', 'Medium']]