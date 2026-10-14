"""Stop the messenger running on this computer, preserving its stored history."""
import json
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


def main():
    opener = build_opener(ProxyHandler({}))
    base = "http://127.0.0.1:8766/"
    try:
        with opener.open(base + "api/admin", timeout=3) as response:
            token = json.load(response)["token"]
        request = Request(base + "api/admin/stop", data=b"{}", headers={"X-CSRF-Token": token})
        with opener.open(request, timeout=3) as response:
            json.load(response)
        print("LAN Messenger stopped. History is saved.")
    except URLError:
        raise SystemExit("No local messenger responded on port 8766.")


if __name__ == "__main__":
    main()
