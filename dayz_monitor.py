#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DayZ Server Monitor
====================

Watches a DayZ server over the Steam query protocol (A2S):

  - polls the server every 30 seconds by default
  - shows ping and current player count
  - writes every scan with a timestamp to a CSV file, so patterns can be
    spotted later (when are there many/few players online)
  - sends a webhook notification (e.g. to Discord) when
      a) monitoring starts (status message)
      b) new players join
      c) players leave (including the special case: the server goes empty)

Installation:
    pip install python-a2s requests

Examples:
    python dayz_monitor.py
    python dayz_monitor.py --webhook https://discord.com/api/webhooks/XXXX/YYYY
    python dayz_monitor.py --ip 203.0.113.10 --port 2302 --interval 60

A note on the port:
    The "query port" (used for things like player count/ping) is often NOT
    the same as the game port you connect to in-game. This script therefore
    automatically tries a few nearby ports on startup if the one given
    doesn't answer.
"""

# Haelt die Typannotation unten (dict[str, str | int | float]) auch auf
# aelteren Python-Versionen lauffaehig - sonst waere 3.10 Pflicht.
from __future__ import annotations

# Dokumentations-IP (RFC 5737), reiner Platzhalter. Die echte Adresse kommt
# aus dayz_monitor.config (siehe setup.py) oder von --ip.
PLACEHOLDER_IP = "203.0.113.10"

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"
YELLOW = "\033[93m"

def colored_ping(ping_ms):
    if ping_ms is None:
        return f"{RED}[N/A]{RESET}"

    if ping_ms < 80:
        color = GREEN
    elif ping_ms <= 150:
        color = YELLOW
    else:
        color = RED

    return f"{color}[{ping_ms:.0f}ms]{RESET}"

TITLE = r"""
.______  .______   ____   ____.______      ._____.___ ._______  .______  .___ _____._._______  .______  
:_ _   \ :      \  \   \_/   /\____  |     :         |: .___  \ :      \ : __|\__ _:|: .___  \ : __   \ 
|   |   ||   .   |  \___ ___/ /  ____|     |   \  /  || :   |  ||       || : |  |  :|| :   |  ||  \____|
| . |   ||   :   |    |   |   \      |     |   |\/   ||     :  ||   |   ||   |  |   ||     :  ||   :  \ 
|. ____/ |___|   |    |___|    \__:__|     |___| |   | \_. ___/ |___|   ||   |  |   | \_. ___/ |   |___\
 :/          |___|                :              |___|   :/         |___||___|  |___|   :/     |___|    
 :                                •                      :                              :               
                                  by bimonator https://github.com/bimonator

 """
def center_text(text):
    width = shutil.get_terminal_size().columns
    return text.center(width)

from pathlib import Path
import json
import argparse
import csv
import os
import sys
import time
from datetime import datetime
import shutil


# Windows-Konsolen laufen oft mit cp1252 und ohne ANSI-Unterstuetzung:
# ohne das hier wuerden Umlaute abstuerzen und Farbcodes als Muell erscheinen.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

if os.name == "nt":
    os.system("")  # schaltet die ANSI-Escape-Verarbeitung in cmd.exe frei


try:
    import a2s
except ImportError:
    print("The 'python-a2s' package is missing. Install it with:\n    pip install python-a2s")
    sys.exit(1)

try:
    import requests
except ImportError:
    requests = None  # Webhook-Versand wird dann übersprungen


# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="DayZ Server Monitor - ping, player count, activity logging, webhook notifications"
    )
    parser.add_argument("--ip", default=PLACEHOLDER_IP, help="IP address of the DayZ server")
    parser.add_argument("--port", type=int, default=2302,
                         help="Starting port for queries (adjusted automatically if needed)")
    parser.add_argument("--interval", type=int, default=30, help="Scan interval in seconds (default: 30)")
    parser.add_argument("--log", default="dayz_monitor_log.csv", help="Path to the CSV log file")
    parser.add_argument("--webhook", default=None, help="Webhook URL for notifications (e.g. Discord)")
    parser.add_argument("--config", type=str,
                        default=Path(__file__).parent / "dayz_monitor.config",
                        help="Path to a JSON config file with default settings")
    parser.add_argument("--timeout", type=float, default=5.0, help="Timeout per server request in seconds")

    # Zwei Durchgaenge: erst nur --config einlesen, dann die Werte aus der
    # Config als argparse-Defaults setzen und final parsen. Dadurch gilt
    # Config < CLI - ein explizit gesetztes --ip gewinnt also immer.
    pre_args, _ = parser.parse_known_args()
    cfg = read_config(Path(pre_args.config))
    if cfg:
        parser.set_defaults(**cfg)

    args = parser.parse_args()
    args.config_found = bool(cfg)
    return args

# ---------------------------------------------------------------------------
# Configdatei laden
# ---------------------------------------------------------------------------

def read_config(path: Path) -> dict[str, str | int | float]:

    if not path.exists():
        return {}  # kein Fehler – nur leeres Dict
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # nur zulässige Schlüssel übernehmen: webhook, ip, port, interval
        defaults = {k: v for k, v in cfg.items() if k in ("webhook", "ip", "port", "interval")}
    except (json.JSONDecodeError, IOError):
        print("Warning: dayz_monitor.config is not valid JSON - ignoring it.")
        print("         Recreate it with:  python setup.py")
        return {}

    # Port/Intervall koennen in der Config als String stehen - argparse wendet
    # seinen type= nicht auf gesetzte Defaults an, also hier konvertieren.
    for key in ("port", "interval"):
        if key in defaults:
            try:
                defaults[key] = int(defaults[key])
            except (TypeError, ValueError):
                print(f"Warning: '{key}' in the config is not a number - using the default instead.")
                del defaults[key]

    return defaults


# ---------------------------------------------------------------------------
# Query-Port automatisch finden
# ---------------------------------------------------------------------------

def find_query_port(ip, start_port, timeout):
    """
    Testet den angegebenen Port und, falls nötig, ein paar gängige Alternativen
    (der Query-Port weicht bei vielen Hostern vom Spiel-Port ab).
    Gibt den ersten funktionierenden Port zurück, sonst None.
    """
    candidates = []
    for p in (start_port, start_port + 1, start_port + 2, start_port - 1, 27016, 27015):
        if p > 0 and p not in candidates:
            candidates.append(p)

    for port in candidates:
        try:
            a2s.info((ip, port), timeout=timeout)
            return port
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def init_log_file(path):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["timestamp", "status", "ping_ms", "players", "max_players", "event", "details"]
            )


def log_entry(path, status, ping_ms, players, max_players, event="", details=""):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([timestamp, status, ping_ms, players, max_players, event, details])
    return timestamp


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def send_webhook(url, message):
    if not url:
        return
    if requests is None:
        print("The 'requests' package is missing, skipping webhook. Install it with: pip install requests")
        return
    try:
        resp = requests.post(url, json={"content": message}, timeout=5)
        if resp.status_code >= 300:
            print(f"Webhook error (status {resp.status_code})")
    except requests.RequestException as e:
        print(f"Could not send webhook: {e}")


# ---------------------------------------------------------------------------
# Serverabfrage
# ---------------------------------------------------------------------------

def query_server(ip, port, timeout):
    """Fragt Ping/Spieleranzahl ab. Gibt Dict zurück, im Fehlerfall mit 'error'-Key."""
    try:
        info = a2s.info((ip, port), timeout=timeout)
        return {
            "ping_ms": round(info.ping * 1000, 1) if info.ping is not None else None,
            "players": info.player_count,
            "max_players": info.max_players,
            "server_name": info.server_name,
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Hauptschleife
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    # Weder Config noch --ip: der Nutzer hat das Setup noch nicht gemacht.
    # Ohne diesen Hinweis liefe das Skript in den Platzhalter und meldete nur
    # "Server nicht erreichbar" - ein wenig hilfreicher Fehler.
    if not args.config_found and args.ip == PLACEHOLDER_IP:
        print("No configuration found yet (dayz_monitor.config is missing).")
        print()
        print("Start the setup wizard with:")
        print("    python setup.py")
        print()
        print("Or set things up directly on the command line:")
        print("    python dayz_monitor.py --ip YOUR.SERVER.IP --webhook YOUR_WEBHOOK_URL")
        sys.exit(1)

    if not args.webhook:
        print("Note: no webhook URL set - Discord notifications are off.")
        print("      Set one up with:  python setup.py")
        print()

    init_log_file(args.log)

    print(f"Checking connection to {args.ip} ...")
    port = find_query_port(args.ip, args.port, args.timeout)
    if port is None:
        print(f"Could not reach the server at {args.ip} on any of the tested ports.")
        print("Possible reasons: server offline, wrong IP, firewall, or the query port is")
        print("outside the range tested automatically. Your host's control panel usually lists it.")
        sys.exit(1)
    if port != args.port:
        print(f"Port {args.port} did not respond - port {port} works and will be used.\n")
    else:
        print(f"Connected successfully (port {port}).\n")

    print(f"Monitoring {args.ip}:{port} every {args.interval} seconds")
    print(f"Log file: {os.path.abspath(args.log)}")
    print(f"Webhook notifications: {'enabled' if args.webhook else 'disabled'}")
    print("Stop with Ctrl+C\n")

    last_count = None
    ping_history = []

    try:
        while True:
            cycle_start = time.time()
            result = query_server(args.ip, port, args.timeout)

            print("\033[2J\033[H", end="")

            for title_line in TITLE.strip("\n").splitlines():
                print(center_text(title_line))

            print()

            for ping in ping_history:
                print(center_text(ping))


            if "error" in result:
                ts = log_entry(args.log, "offline", "", "", "", "offline", result["error"])
                print(f"[{ts}] Server unreachable ({result['error']})")
                last_count = None
            else:
                ping_ms = result["ping_ms"]
                players = result["players"]
                max_players = result["max_players"]
                server_name = result["server_name"]

                event, details = "", ""

                if last_count is None:
                    # Erster erfolgreicher Scan seit Start des Skripts: Status einmalig
                    # melden, statt zu schweigen. Kein "Join", da unbekannt ist, seit
                    # wann diese Spieler schon online sind.
                    send_webhook(
                        args.webhook,
                        f"ℹ️ Monitoring started on **{server_name}** - currently {players}/{max_players} players online."
                    )
                elif players > last_count:
                    diff = players - last_count
                    event = "join"
                    details = f"+{diff} players (total {players})"
                    send_webhook(
                        args.webhook,
                        f"🟢 {diff} player(s) joined **{server_name}**! Currently {players}/{max_players} online."
                    )
                elif players < last_count:
                    diff = last_count - players
                    event = "server_empty" if players == 0 else "leave"
                    details = f"-{diff} players (total {players})"
                    if players == 0:
                        send_webhook(args.webhook, f"🔴 **{server_name}** is now empty (0 players).")
                    else:
                        send_webhook(
                            args.webhook,
                            f"🟠 {diff} player(s) left **{server_name}**. Currently {players}/{max_players} online."
                        )

                ts = log_entry(args.log, "online", ping_ms, players, max_players, event, details)
                ping_line = f"[{ts}] {colored_ping(ping_ms)} | Players: {players}/{max_players}"

                ping_history.append(ping_line)

                terminal_height = shutil.get_terminal_size().lines
                max_pings = max(1, terminal_height - 12)
                if len(ping_history) > max_pings:
                    ping_history.pop(0)

                ping_display = colored_ping(ping_ms)
                line = f"[{ts}] Ping: {ping_display} | Players: {players}/{max_players}"
                if details:
                    line += f" | {details}"
                if event == "join":
                    print(f"{GREEN}{line}{RESET}")
                elif event in ("leave", "server_empty"):
                    print(f"{RED}{line}{RESET}")
                else:
                    print(line)

                last_count = players

            elapsed = time.time() - cycle_start
            time.sleep(max(0, args.interval - elapsed))

    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


if __name__ == "__main__":
    main()
