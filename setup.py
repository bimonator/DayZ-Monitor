#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DayZ Server Monitor — Setup Wizard
====================================

Interactively creates dayz_monitor.config with YOUR server details and
YOUR Discord webhook, so you never have to hand-edit a JSON file.

Usage:
    python setup.py
"""

import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Windows-Konsolen nutzen oft cp1252 und kommen mit den Symbolen unten nicht
# klar - ohne das hier bricht der Assistent mit einem UnicodeEncodeError ab.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

try:
    import a2s
except ImportError:
    a2s = None  # Der Verbindungstest wird dann uebersprungen.

CONFIG_PATH = Path(__file__).parent / "dayz_monitor.config"

WEBHOOK_PATTERN = re.compile(
    r"^https://discord(app)?\.com/api/webhooks/\d+/[\w-]+/?$"
)


def ask(prompt, default=None, validator=None, error_msg="That doesn't look right, try again."):
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return str(default)
        if not raw:
            print("This value is required.")
            continue
        if validator and not validator(raw):
            print(f"  -> {error_msg}")
            continue
        return raw


def is_valid_ip(value):
    parts = value.split(".")
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def is_valid_port(value):
    try:
        return 1 <= int(value) <= 65535
    except ValueError:
        return False


def is_valid_interval(value):
    try:
        return int(value) > 0
    except ValueError:
        return False


def is_valid_webhook(value):
    return bool(WEBHOOK_PATTERN.match(value))


def probe_port(ip, port, timeout=3.0):
    """Fragt einen einzelnen Port per A2S ab. Gibt den Servernamen zurueck
    oder None, wenn dort nichts antwortet."""
    try:
        info = a2s.info((ip, int(port)), timeout=timeout)
        return getattr(info, "server_name", "") or "(unnamed server)"
    except Exception:
        return None


def find_query_port(ip, start_port):
    """Testet den angegebenen Port und die ueblichen Alternativen. Der
    Query-Port weicht bei vielen Hostern vom Spiel-Port ab, deshalb pruefen
    wir das hier schon im Setup statt den Nutzer spaeter raten zu lassen.
    Gibt (port, server_name) zurueck oder (None, None)."""
    start_port = int(start_port)
    candidates = []
    for p in (start_port, start_port + 1, start_port + 2, start_port - 1, 27016, 27015):
        if p > 0 and p not in candidates:
            candidates.append(p)

    for port in candidates:
        name = probe_port(ip, port)
        if name:
            return port, name
    return None, None


def test_webhook(url):
    payload = json.dumps(
        {"content": "✅ DayZ Monitor setup complete — this webhook works!"}
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status < 300
    except urllib.error.HTTPError as e:
        print(f"  -> Discord responded with an error: {e.code} {e.reason}")
        return False
    except urllib.error.URLError as e:
        print(f"  -> Could not reach Discord: {e.reason}")
        return False


def confirm_server(ip, port):
    """Prueft, ob der Server antwortet, und korrigiert den Port falls noetig.
    Gibt den zu speichernden Port zurueck."""
    if a2s is None:
        print()
        print("  -> Skipping the connection test: the 'python-a2s' package is missing.")
        print("     Install the dependencies first:  pip install -r requirements.txt")
        return port

    print()
    print(f"Testing the connection to {ip} ...")
    found, name = find_query_port(ip, port)

    if found is None:
        print("  -> No answer on any of the ports tried.")
        print("     Common causes: the server is offline, the IP is wrong, or the")
        print("     query port is a different one. You can look the correct query")
        print("     port up on https://www.battlemetrics.com/ .")
        print("     The config is saved anyway - re-run setup.py once you know it.")
        return port

    print(f"  -> Server reached: {name}")
    if int(found) != int(port):
        print(f"  -> Port {port} stayed quiet, but port {found} answered. Using {found}.")
    return found


def main():
    print("=" * 60)
    print(" DayZ Server Monitor — Setup")
    print("=" * 60)
    print(
        "\nThis writes dayz_monitor.config with your own server and webhook\n"
        "details. Re-run this anytime to change them.\n"
    )

    if CONFIG_PATH.exists():
        overwrite = input(
            f"{CONFIG_PATH.name} already exists. Overwrite it? [y/N]: "
        ).strip().lower()
        if overwrite != "y":
            print("Aborted — existing config left untouched.")
            sys.exit(0)
        print()

    print("--- Your DayZ server ---")
    print("The IP address is the one you use to connect to the server in DayZ.")
    print()
    ip = ask(
        "Server IP address",
        validator=is_valid_ip,
        error_msg="That doesn't look like a valid IPv4 address, e.g. 203.0.113.10",
    )
    port = ask(
        "Query port (just press Enter if you don't know it)",
        default=2302,
        validator=is_valid_port,
        error_msg="Port must be a number between 1 and 65535.",
    )

    # Direkt hier testen statt den Nutzer spaeter im Monitor scheitern zu
    # lassen: der Query-Port weicht bei vielen Hostern vom Spiel-Port ab.
    port = confirm_server(ip, port)

    interval = ask(
        "Poll interval in seconds",
        default=30,
        validator=is_valid_interval,
        error_msg="Interval must be a positive whole number.",
    )

    print()
    print("--- Discord notifications (optional) ---")
    print("In Discord: Server Settings -> Integrations -> Webhooks -> New Webhook,")
    print("pick a channel, then click 'Copy Webhook URL'.")
    print("Press Enter to skip and run without notifications.")
    print()
    webhook = ask(
        "Discord webhook URL",
        default="",
        validator=lambda v: not v or is_valid_webhook(v),
        error_msg=(
            "That doesn't look like a Discord webhook URL. It should look like:\n"
            "     https://discord.com/api/webhooks/123456789012345678/AbCdEf..."
        ),
    )

    if webhook:
        print()
        print("Sending a test message to your webhook ...")
        if test_webhook(webhook):
            print("  -> Success. Check your Discord channel for the test message.")
        else:
            print("  -> Could not confirm delivery, but saving the config anyway.")
            print("     Double-check the webhook URL in Discord if no message shows up.")
    else:
        print()
        print("No webhook set - Discord notifications stay off.")
        print("Re-run 'python setup.py' later to add one.")

    config = {
        "webhook": webhook,
        "ip": ip,
        "port": int(port),
        "interval": int(interval),
    }

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print()
    print(f"Saved to {CONFIG_PATH.name}.")
    print()
    print("You're all set. Start monitoring with:")
    print("    python dayz_monitor.py")
    print()
    print("Generate an HTML report anytime with:")
    print("    python dayz_report.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
