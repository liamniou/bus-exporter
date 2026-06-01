#!/usr/bin/env python3
"""
Prometheus exporter for Stockholm bus departures (SL API)
Exports metrics for buses 607 (Jaktskyttebanan) and 601 (Torsplan) 
"""

import time
import requests
from prometheus_client import start_http_server, Gauge, Info
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration
JAKTSKYTTEBANAN_SITE_ID = "2163"  # Jaktskyttebanan stop
SL_API_BASE = "https://transport.integration.sl.se/v1/sites"
SCRAPE_INTERVAL = 30  # seconds
WALK_TIME_MINUTES = 10
MIN_DEPARTURE_MINUTES = 8  # Hide buses departing in less than this many minutes
MAX_DEPARTURES = 4  # Show only the 4 closest departures

# Stockholm timezone - SL API returns times in this timezone
STOCKHOLM_TZ = ZoneInfo("Europe/Stockholm")

# Bus routes configuration
TORSPLAN_SITE_ID = "2151"  # Torsplan stop

BUS_ROUTES = [
    {
        "site_id": JAKTSKYTTEBANAN_SITE_ID,
        "stop_name": "Jaktskyttebanan",
        "lines": ["607"],
        "destination_contains": "Danderyds"
    },
    {
        "site_id": TORSPLAN_SITE_ID,
        "stop_name": "Torsplan",
        "lines": ["601"],
        "destination_contains": "Svalnäs"
    },
]

# Prometheus metrics
bus_departure_minutes = Gauge(
    'bus_departure_minutes',
    'Minutes until bus departure',
    ['line', 'stop', 'destination', 'departure_index']
)

bus_leave_in_minutes = Gauge(
    'bus_leave_in_minutes',
    'Minutes until you should leave (departure minus walk time)',
    ['line', 'stop', 'destination', 'departure_index']
)

bus_departure_info = Info(
    'bus_departure',
    'Bus departure information',
    ['line', 'stop', 'destination', 'departure_index']
)

exporter_scrape_duration = Gauge(
    'bus_exporter_scrape_duration_seconds',
    'Time spent scraping bus data'
)


def parse_display_to_minutes(display: str) -> int | None:
    """Parse SL display string to minutes. Returns None if not parseable."""
    if not display:
        return None
    
    # "Nu" means now (0 minutes)
    if display.lower() == "nu":
        return 0
    
    # Try to extract minutes from "X min" format
    match = re.match(r'^(\d+)\s*min', display, re.IGNORECASE)
    if match:
        return int(match.group(1))
    
    # Try HH:MM format - calculate minutes from now using Stockholm timezone
    match = re.match(r'^(\d{1,2}):(\d{2})$', display)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        now = datetime.now(STOCKHOLM_TZ)
        departure_minutes = hour * 60 + minute
        current_minutes = now.hour * 60 + now.minute
        diff = departure_minutes - current_minutes
        if diff < -60:
            diff += 24 * 60
        return max(0, diff)
    
    return None


def fetch_departures(site_id: str, lines: list[str], destination_contains: str) -> list[dict]:
    """Fetch and filter departures from SL API (no caching)."""
    url = f"{SL_API_BASE}/{site_id}/departures"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.error(f"Error fetching departures from site {site_id}: {e}")
        return []
    
    departures = []
    for dep in data.get('departures', []):
        line = dep.get('line', {}).get('designation', '')
        destination = dep.get('destination', '')
        display = dep.get('display', '')
        
        if line in lines and destination_contains.lower() in destination.lower():
            minutes = parse_display_to_minutes(display)
            
            # Filter out buses departing in less than MIN_DEPARTURE_MINUTES
            if minutes is not None and minutes < MIN_DEPARTURE_MINUTES:
                logger.debug(f"Skipping {line} to {destination} - only {minutes} min (< {MIN_DEPARTURE_MINUTES})")
                continue
            
            departures.append({
                'line': line,
                'destination': destination,
                'display': display,
                'minutes': minutes,
                'stop_area': dep.get('stop_area', {}).get('name', 'Unknown')
            })
    
    return departures


def update_metrics():
    """Fetch data and update Prometheus metrics."""
    global latest_departures
    start_time = time.time()
    all_departures = []
    
    for route in BUS_ROUTES:
        departures = fetch_departures(
            route["site_id"],
            lines=route["lines"],
            destination_contains=route["destination_contains"]
        )
        
        stop_name = route["stop_name"]
        
        if not departures:
            logger.warning(f"No departures found for {stop_name}")
        
        # Add all departures (no per-route limit)
        for dep in departures:
            leave_in = None
            if dep['minutes'] is not None:
                leave_in = dep['minutes'] - WALK_TIME_MINUTES
            
            all_departures.append({
                'line': dep['line'],
                'stop': stop_name,
                'display': dep['display'],
                'minutes': dep['minutes'],
                'leave_in': leave_in,
                'destination': dep['destination']
            })
    
    # Sort by minutes (chronological) and limit to MAX_DEPARTURES
    all_departures.sort(key=lambda x: (x['minutes'] is None, x['minutes'] or 999))
    all_departures = all_departures[:MAX_DEPARTURES]
    
    # Update Prometheus metrics with the limited set
    for idx, dep in enumerate(all_departures):
        labels = {
            'line': dep['line'],
            'stop': dep['stop'],
            'destination': dep['destination'],
            'departure_index': str(idx)
        }
        
        if dep['minutes'] is not None:
            bus_departure_minutes.labels(**labels).set(dep['minutes'])
            bus_leave_in_minutes.labels(**labels).set(max(0, dep['leave_in'] or 0))
        
        bus_departure_info.labels(**labels).info({
            'display': dep['display'],
            'stop_area': dep['stop']
        })
    
    latest_departures = all_departures
    
    duration = time.time() - start_time
    exporter_scrape_duration.set(duration)
    logger.info(f"Updated metrics: {len(all_departures)} departures ({duration:.2f}s)")


from http.server import HTTPServer, BaseHTTPRequestHandler
import json
import threading

latest_departures = []

class JSONHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/json':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(latest_departures).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        pass


def run_json_server():
    server = HTTPServer(('0.0.0.0', 9103), JSONHandler)
    server.serve_forever()


def main():
    port = 9101
    logger.info(f"Starting bus exporter on port {port} (metrics) and 9103 (JSON)")
    logger.info(f"Filtering out buses departing in < {MIN_DEPARTURE_MINUTES} minutes")
    logger.info(f"Showing max {MAX_DEPARTURES} closest departures")
    start_http_server(port)
    
    json_thread = threading.Thread(target=run_json_server, daemon=True)
    json_thread.start()
    
    while True:
        try:
            update_metrics()
        except Exception as e:
            logger.error(f"Error updating metrics: {e}")
        time.sleep(SCRAPE_INTERVAL)


if __name__ == '__main__':
    main()
