"""One-time-per-day login: turns a Kite request_token into an access_token.

Kite access tokens expire every morning (~06:00 IST), so run this once a day:

    export KITE_API_KEY=...  KITE_API_SECRET=...
    python login.py
"""
import json
import os
import sys
import webbrowser
from datetime import datetime
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect

SESSION_FILE = os.path.join(os.path.dirname(__file__), ".kite_session.json")


def main():
    api_key = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]
    kite = KiteConnect(api_key=api_key)

    url = kite.login_url()
    print(f"Log in here, then copy the request_token=... value from the redirect URL:\n  {url}")
    webbrowser.open(url)
    request_token = (sys.argv[1] if len(sys.argv) > 1 else input("request_token: ")).strip()

    session = kite.generate_session(request_token, api_secret=api_secret)
    with open(SESSION_FILE, "w") as f:
        json.dump({"access_token": session["access_token"], "date": datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()}, f)
    os.chmod(SESSION_FILE, 0o600)
    print(f"Logged in as {session['user_id']}; token saved to {SESSION_FILE}")


if __name__ == "__main__":
    main()
