#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DayZ Activity Report
======================

Builds an HTML report from the DayZ monitor CSV.

Includes:
    - a minimalist, Mullvad-style map (self-contained inline SVG,
      no mapping library, no external map services/tiles at runtime);
      the region follows the server: Europe, North America, Asia, Africa
    - a pulsing green marker for the server's location
    - automatic IP geolocation
    - server location / hosting provider
    - daily player-count heatmap
    - daily ping heatmap
    - weekday x hour activity pattern
    - key stats
    - the server location is determined automatically via geo-IP, no manual entry needed

Installation:
    pip install requests

Usage:
    python dayz_report.py
    python dayz_report.py --log dayz_monitor_log.csv --out report.html
    python dayz_report.py --days 30
    python dayz_report.py --no-geo
    python dayz_report.py --map-region north_america
    python dayz_report.py --no-browser
"""

import argparse
import csv
import html
import json
import math
import os
import webbrowser
from collections import defaultdict
from datetime import datetime, date, timedelta

try:
    import requests
except ImportError:
    requests = None


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

DEFAULT_IP = "203.0.113.10"


def load_configured_ip():
    """Liest die Server-IP aus dayz_monitor.config, falls vorhanden
    (vom setup.py-Assistenten oder manuell angelegt). Fällt sonst auf
    DEFAULT_IP zurück. Ein explizit übergebenes --ip hat immer Vorrang,
    da dies hier nur den argparse-Default liefert."""
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "dayz_monitor.config",
    )
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        ip = cfg.get("ip")
        if ip:
            return ip
    except (OSError, json.JSONDecodeError):
        pass
    return DEFAULT_IP


# Generischer Fallback, falls Geo-IP-Lookup komplett fehlschlägt (z.B. kein
# Internetzugriff). Bewusst ein neutraler Punkt in der Kartenmitte statt
# einer erfundenen Stadt - "unbekannt" ist ehrlicher als ein falscher Standort.
FALLBACK_LAT = 50.0
FALLBACK_LON = 10.0

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Builds a DayZ activity/ping report"
    )

    p.add_argument(
        "--log",
        default="dayz_monitor_log.csv",
        help="Path to the CSV log file"
    )

    p.add_argument(
        "--out",
        default="dayz_report.html",
        help="Path of the generated HTML file"
    )

    p.add_argument(
        "--ip",
        default=load_configured_ip(),
        help="Server IP for the geo-IP lookup (default: the value from dayz_monitor.config)"
    )

    p.add_argument(
        "--days",
        type=int,
        default=None,
        help="How many days back to show"
    )

    p.add_argument(
        "--map-region",
        default=None,
        choices=sorted(MAP_REGIONS),
        help=(
            "force a map region instead of deriving it from the server "
            "location (handy for testing/screenshots)"
        )
    )

    p.add_argument(
        "--no-geo",
        action="store_true",
        help="Skip the geo-IP lookup"
    )

    p.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't automatically open the HTML file after creating it"
    )

    return p.parse_args()


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def load_rows(log_path):
    rows = []

    if not os.path.exists(log_path):
        return rows

    with open(log_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.strptime(
                    row["timestamp"],
                    "%Y-%m-%d %H:%M:%S"
                )
            except (ValueError, KeyError):
                continue

            entry = {
                "ts": ts,
                "status": row.get("status", "")
            }

            if entry["status"] == "online":
                try:
                    entry["players"] = int(row["players"])
                except (ValueError, KeyError, TypeError):
                    entry["players"] = None

                try:
                    entry["ping"] = (
                        float(row["ping_ms"])
                        if row.get("ping_ms")
                        else None
                    )
                except ValueError:
                    entry["ping"] = None

            rows.append(entry)

    return rows


# ---------------------------------------------------------------------------
# Statistiken
# ---------------------------------------------------------------------------

def compute_daily_stats(rows):
    daily = defaultdict(lambda: {
        "players": [],
        "ping": []
    })

    for r in rows:
        if r["status"] != "online":
            continue

        d = r["ts"].date()

        if r.get("players") is not None:
            daily[d]["players"].append(r["players"])

        if r.get("ping") is not None:
            daily[d]["ping"].append(r["ping"])

    stats = {}

    for d, v in daily.items():
        stats[d] = {
            "avg_players": (
                sum(v["players"]) / len(v["players"])
                if v["players"]
                else None
            ),
            "peak_players": (
                max(v["players"])
                if v["players"]
                else None
            ),
            "avg_ping": (
                sum(v["ping"]) / len(v["ping"])
                if v["ping"]
                else None
            ),
            "scans": len(v["players"])
        }

    return stats


def compute_hourly_pattern(rows):
    buckets = defaultdict(list)

    for r in rows:
        if r["status"] != "online":
            continue

        if r.get("players") is None:
            continue

        buckets[
            (r["ts"].weekday(), r["ts"].hour)
        ].append(r["players"])

    return {
        k: sum(v) / len(v)
        for k, v in buckets.items()
    }


def compute_summary(rows):
    online = [
        r for r in rows
        if r["status"] == "online"
    ]

    pings = [
        r["ping"]
        for r in online
        if r.get("ping") is not None
    ]

    players = [
        r["players"]
        for r in online
        if r.get("players") is not None
    ]

    return {
        "avg_ping": (
            sum(pings) / len(pings)
            if pings
            else None
        ),

        "avg_players": (
            sum(players) / len(players)
            if players
            else None
        ),

        "peak_players": (
            max(players)
            if players
            else None
        ),

        "days_logged": len({
            r["ts"].date()
            for r in rows
        }),

        "uptime_pct": (
            len(online) / len(rows) * 100
            if rows
            else None
        ),

        "first_date": min(
            (r["ts"] for r in rows),
            default=None
        ),

        "last_date": max(
            (r["ts"] for r in rows),
            default=None
        ),
    }


# ---------------------------------------------------------------------------
# Geo-IP
# ---------------------------------------------------------------------------

def lookup_location(ip, cache_path):
    if requests is not None:
        try:
            resp = requests.get(
                f"http://ip-api.com/json/{ip}",
                params={
                    "fields": (
                        "status,country,regionName,city,"
                        "lat,lon,isp,org"
                    )
                },
                timeout=5,
            )

            resp.raise_for_status()
            data = resp.json()

            if data.get("status") == "success":
                loc = {
                    "city": data.get("city", ""),
                    "country": data.get("country", ""),
                    "region": data.get("regionName", ""),
                    "isp": (
                        data.get("org")
                        or data.get("isp", "")
                    ),
                    "lat": data.get("lat"),
                    "lon": data.get("lon"),
                }

                try:
                    with open(
                        cache_path,
                        "w",
                        encoding="utf-8"
                    ) as f:
                        json.dump(
                            loc,
                            f,
                            ensure_ascii=False,
                            indent=2
                        )
                except OSError:
                    pass

                return loc

        except Exception:
            pass

    try:
        with open(
            cache_path,
            encoding="utf-8"
        ) as f:
            return json.load(f)

    except (
        OSError,
        json.JSONDecodeError
    ):
        return None


def ensure_coordinates(loc):
    if not loc:
        return {
            "city": "",
            "country": "",
            "region": "",
            "isp": "",
            "lat": FALLBACK_LAT,
            "lon": FALLBACK_LON,
        }

    try:
        lat = float(loc.get("lat"))
        lon = float(loc.get("lon"))

        if -90 <= lat <= 90 and -180 <= lon <= 180:
            loc["lat"] = lat
            loc["lon"] = lon
            return loc

    except (TypeError, ValueError):
        pass

    loc["lat"] = FALLBACK_LAT
    loc["lon"] = FALLBACK_LON

    loc.setdefault("city", "")
    loc.setdefault("country", "")
    loc.setdefault("region", "")
    loc.setdefault("isp", "")

    return loc


def format_location(loc):
    if not loc:
        return "unknown"

    place = ", ".join(
        p for p in (
            loc.get("city"),
            loc.get("country")
        )
        if p
    ) or "unknown"

    if loc.get("isp"):
        return f"{place} ({loc['isp']})"

    return place


# ---------------------------------------------------------------------------
# Europa-Umriss (statisch, Mullvad-Stil)
# ---------------------------------------------------------------------------
#
# Bewusst kein echtes GeoJSON/TopoJSON mehr: die Karte war früher auf einen
# Laufzeit-Download von countries.geojson + Leaflet angewiesen. Stattdessen
# steckt hier eine vereinfachte, handkuratierte "low poly"-Küstenlinie als
# (Länge, Breite)-Punkte drin. Kein Vermessungsanspruch, nur so genau, dass
# Europa erkennbar bleibt - komplett offline, ohne externe Bibliothek.
#
# Zwei getrennte Datensätze:
#   - EUROPE_SHAPES:  geschlossene Flächen (Küstenlinien) -> gefüllt,
#                      OHNE eigenen Rand.
#   - EUROPE_BORDERS: offene Linienzüge (Landesgrenzen) -> nur diese
#                      bekommen einen sichtbaren Rand/Stroke.

EUROPE_REF_LAT = 45.0   # Referenzbreite für die Projektion
EUROPE_PADDING = 2.5    # Rand um die Landmasse in Projektions-Einheiten

# Festland: Iberische Halbinsel -> Mittelmeer -> Balkan -> Schwarzes Meer ->
# (willkürlicher) Schnitt Richtung Ural -> Nordmeerküste -> Skandinavien ->
# Ostsee -> Nordsee -> Atlantikküste zurück zum Start.
EUROPE_MAINLAND = [
    # Iberische Halbinsel
    (-9.5, 43.0), (-8.9, 42.6), (-8.8, 41.5), (-8.9, 40.6),
    (-9.4, 39.4), (-9.5, 38.7), (-9.0, 38.0), (-8.8, 37.3),
    (-8.95, 37.0), (-7.9, 36.95), (-7.4, 37.15), (-6.3, 36.9),
    (-5.6, 36.0), (-4.4, 36.4), (-2.6, 36.7), (-1.3, 37.2),
    (-0.5, 37.6), (0.2, 39.5), (0.8, 40.3), (1.2, 41.0),
    (2.2, 41.4), (3.1, 42.4),
    # Frankreich Mittelmeerküste
    (3.9, 43.0), (4.4, 43.4), (4.8, 43.3), (5.4, 43.3),
    (6.2, 43.1), (6.9, 43.5), (7.3, 43.7),
    # Italien Ligurien/Tyrrhenisches Meer
    (8.4, 44.4), (8.9, 44.1), (9.8, 44.1), (10.3, 42.9),
    (11.1, 42.4), (11.8, 42.4), (12.2, 41.8), (13.9, 40.8),
    (14.3, 40.85), (14.4, 40.55), (15.3, 40.0), (15.9, 39.5),
    # Kalabrien, Golf von Tarent, Apulien
    (15.7, 38.1), (16.3, 38.2), (16.6, 39.0), (18.0, 39.8),
    (18.5, 40.1), (18.1, 40.6), (17.2, 41.1), (16.2, 41.9),
    (14.7, 42.0),
    # Mittel-/Norditalien Adria
    (13.7, 43.0), (13.5, 43.6), (12.6, 44.5), (12.3, 45.4),
    (13.6, 45.6),
    # Balkan Adriaküste
    (13.9, 45.2), (14.4, 45.3), (15.2, 44.8), (14.9, 44.3),
    (15.9, 43.9), (16.4, 43.5), (17.1, 43.0), (17.7, 42.9),
    (18.4, 42.6), (18.5, 42.3), (19.1, 41.3),
    # Albanien, Griechenland, Ägäis
    (19.4, 40.5), (19.9, 39.6), (21.0, 39.0), (21.3, 38.2),
    (21.7, 37.3), (21.7, 36.8), (22.4, 36.6), (22.9, 36.9),
    (23.0, 37.0), (23.4, 37.6), (23.7, 37.9), (24.0, 38.9),
    (23.3, 39.4), (24.9, 40.0), (25.6, 40.9), (26.1, 40.8),
    # Schwarzes Meer
    (27.9, 43.2), (28.6, 44.2), (29.7, 45.4), (30.7, 46.5),
    (32.0, 45.4), (33.5, 46.3), (34.4, 44.9), (35.3, 45.2),
    # Asowsches Meer, willkürlicher Ost-Schnitt Richtung Ural
    (37.5, 47.0), (39.0, 47.2), (46.0, 49.0), (55.0, 53.0),
    (60.0, 60.0),
    # Arktisküste
    (54.0, 67.0), (44.0, 66.5), (40.0, 68.5), (33.0, 68.9),
    (28.5, 69.9),
    # Norwegen (mit mehr Fjord-Zacken)
    (25.8, 71.1), (23.0, 70.7), (21.0, 70.3), (19.5, 70.0),
    (18.9, 69.6), (17.4, 68.9), (16.4, 68.6), (15.0, 67.9),
    (14.5, 67.3), (13.0, 66.6), (12.5, 66.2), (10.4, 63.4),
    (8.0, 62.7), (6.5, 62.1), (5.3, 60.4), (5.0, 59.7),
    (5.7, 58.9), (7.0, 58.0),
    # Skagerrak, Schweden Westküste
    (8.0, 58.2), (10.5, 59.3), (11.4, 57.7), (12.8, 55.4),
    # Schweden Ostküste, Bottnischer Meerbusen
    (14.3, 55.4), (16.4, 56.2), (17.6, 58.7), (18.1, 59.3),
    (18.7, 60.7), (21.0, 61.5), (22.0, 63.8), (24.1, 65.5),
    # Finnland
    (21.6, 63.1), (21.2, 61.5), (22.3, 60.5), (23.5, 60.0),
    (24.9, 60.2), (27.5, 60.4), (30.3, 60.0),
    # Baltikum
    (28.0, 59.5), (26.7, 59.4), (24.7, 59.4), (23.5, 58.4),
    (23.5, 57.6), (24.1, 56.9), (21.1, 55.7), (20.5, 55.0),
    (19.9, 54.4),
    # Polen / Deutschland Ostseeküste
    (18.6, 54.6), (16.9, 54.5), (14.7, 54.1), (14.3, 53.9),
    (12.6, 54.4),
    # Jütland, Deutschland Nordsee
    (10.5, 57.7), (8.5, 56.5), (8.6, 55.5), (8.1, 53.9),
    (7.2, 53.4),
    # Niederlande, Belgien, Nordfrankreich
    (4.8, 53.0), (4.3, 51.9), (3.2, 51.3), (2.2, 51.0),
    (1.6, 51.0),
    # Normandie, Bretagne, Biskaya
    (-1.1, 49.7), (-1.6, 49.3), (-2.8, 48.7), (-4.4, 48.4),
    (-4.3, 47.7), (-2.5, 47.3), (-1.2, 46.2), (-1.3, 44.7),
]

# Großbritannien (stark vereinfacht, aber mit mehr Buchten)
EUROPE_BRITAIN = [
    (-5.5, 50.0), (-4.2, 50.4), (-3.5, 50.6), (-1.9, 50.6),
    (-0.9, 50.8), (0.4, 51.1), (1.4, 51.4), (0.7, 52.0),
    (0.0, 52.9), (-0.1, 53.5), (-0.2, 54.1), (-1.8, 54.6),
    (-2.0, 55.9), (-3.2, 56.0), (-2.9, 57.5), (-1.8, 57.5),
    (-3.1, 58.6), (-5.1, 58.3), (-5.8, 56.8), (-5.6, 56.0),
    (-4.9, 55.9), (-3.6, 54.9), (-4.5, 54.1), (-4.3, 52.8),
    (-4.7, 52.3), (-5.3, 51.9), (-3.5, 51.2),
]

# Irland (stark vereinfacht)
EUROPE_IRELAND = [
    (-10.5, 51.5), (-9.7, 51.8), (-8.0, 51.7), (-6.5, 52.0),
    (-6.0, 52.2), (-6.2, 53.4), (-5.7, 54.0), (-5.5, 54.6),
    (-7.3, 55.2), (-9.9, 54.3),
]

# Island (nur zur groben Orientierung, sehr grob)
EUROPE_ICELAND = [
    (-24.0, 65.5), (-22.5, 66.4), (-21.0, 66.5), (-18.5, 66.5),
    (-14.5, 66.4), (-13.5, 65.0), (-15.5, 63.7), (-18.0, 63.4),
    (-22.5, 63.8),
]

# Ein paar Inseln für die Wiedererkennbarkeit (sehr grob)
EUROPE_SICILY = [
    (12.4, 38.2), (14.3, 38.2), (15.6, 38.2),
    (15.3, 37.0), (13.0, 37.5), (12.4, 38.0),
]

EUROPE_SARDINIA = [
    (8.2, 41.2), (9.7, 41.1), (9.6, 39.2), (8.4, 39.1), (8.1, 40.5),
]

EUROPE_CORSICA = [
    (9.4, 43.0), (9.5, 42.3), (9.2, 41.4), (8.6, 41.9), (8.7, 42.7),
]

EUROPE_CRETE = [
    (23.5, 35.5), (24.8, 35.3), (26.0, 35.2), (25.7, 35.4), (24.0, 35.5),
]

# Seeland (Dänemark, Kopenhagen)
EUROPE_ZEALAND = [
    (12.0, 55.9), (12.7, 56.0), (12.6, 55.2), (11.9, 55.2), (11.4, 55.6),
]

# Balearen (grob, im Wesentlichen Mallorca)
EUROPE_BALEARICS = [
    (2.3, 39.9), (3.3, 39.8), (3.1, 39.3), (2.4, 39.4),
]

# Malta (sehr klein, nur als Orientierungspunkt)
EUROPE_MALTA = [
    (14.3, 35.9), (14.6, 35.95), (14.5, 35.8), (14.3, 35.85),
]

# Zypern (grob)
EUROPE_CYPRUS = [
    (32.3, 35.7), (33.7, 35.4), (34.0, 35.0), (33.0, 34.6), (32.4, 35.0),
]

# Gotland (Ostsee, Schweden)
EUROPE_GOTLAND = [
    (18.2, 57.9), (19.0, 57.7), (18.8, 57.0), (18.1, 57.3),
]

# Geschlossene Küstenlinien-Flächen - werden gefüllt, ohne eigenen Rand.
EUROPE_SHAPES = [
    EUROPE_MAINLAND,
    EUROPE_BRITAIN,
    EUROPE_IRELAND,
    EUROPE_ICELAND,
    EUROPE_SICILY,
    EUROPE_SARDINIA,
    EUROPE_CORSICA,
    EUROPE_CRETE,
    EUROPE_ZEALAND,
    EUROPE_BALEARICS,
    EUROPE_MALTA,
    EUROPE_CYPRUS,
    EUROPE_GOTLAND,
]

# Offene Linienzüge für Landesgrenzen (stark vereinfacht, Auswahl der
# größeren/bekannteren Grenzen - kein Anspruch auf Vollständigkeit).
EUROPE_BORDERS = [
    # Portugal / Spanien
    [(-8.2, 42.0), (-6.9, 41.9), (-6.8, 41.0),
     (-6.3, 40.0), (-7.0, 39.0), (-7.5, 38.3), (-7.4, 37.3)],

    # Frankreich / Spanien (Pyrenäen)
    [(-1.8, 43.4), (0.0, 42.7), (1.8, 42.5), (3.1, 42.4)],

    # Frankreich: Italien -> Schweiz -> Deutschland -> Luxemburg -> Belgien
    [(7.3, 43.7), (7.0, 44.2), (6.9, 45.0), (6.8, 45.9),
     (6.1, 46.2), (7.0, 47.4), (7.6, 47.6), (8.2, 48.9),
     (6.4, 49.5), (6.2, 49.9), (5.9, 50.8), (4.2, 50.4), (3.2, 51.3)],

    # Schweiz / Italien
    [(6.8, 45.9), (7.7, 45.9), (8.2, 46.0), (9.0, 46.1), (10.4, 46.5)],

    # Schweiz / Deutschland
    [(7.6, 47.6), (8.5, 47.6), (9.2, 47.7), (9.5, 47.5)],

    # Tschechien / Slowakei
    [(16.9, 48.6), (17.4, 49.3), (17.7, 49.9)],

    # Deutschland / Niederlande
    [(6.0, 50.8), (6.2, 51.9), (7.0, 53.2)],

    # Niederlande / Belgien
    [(4.3, 51.5), (3.4, 51.3)],

    # Deutschland / Dänemark
    [(8.7, 54.8), (9.4, 54.8)],

    # Deutschland / Schweiz / Österreich (Bodensee)
    [(9.5, 47.5), (10.2, 47.3), (11.0, 47.4)],

    # Deutschland / Österreich / Tschechien
    [(11.0, 47.4), (12.2, 47.7), (13.0, 48.8),
     (12.5, 50.0), (12.2, 50.3), (12.9, 50.2), (14.3, 50.9)],

    # Deutschland / Polen (Oder-Neiße)
    [(14.3, 50.9), (14.7, 51.9), (14.6, 52.9), (14.3, 53.9)],

    # Polen / Tschechien / Slowakei
    [(14.3, 50.9), (16.0, 50.2), (17.7, 49.9), (18.9, 49.5)],

    # Slowakei / Ukraine
    [(18.9, 49.5), (22.6, 49.0)],

    # Polen / Ukraine / Belarus / Litauen / Kaliningrad
    [(18.9, 49.5), (23.6, 50.4), (23.6, 52.1),
     (23.9, 53.9), (22.7, 54.4), (19.9, 54.4)],

    # Belarus / Ukraine
    [(23.6, 52.1), (28.2, 51.6), (30.7, 51.8), (32.4, 52.1)],

    # Belarus / Russland
    [(32.4, 52.1), (31.8, 53.9), (31.0, 55.3), (28.2, 56.2), (27.4, 57.5)],

    # Österreich / Italien / Slowenien (Alpen)
    [(11.0, 47.4), (13.0, 47.5), (13.8, 46.9), (13.7, 46.5)],

    # Österreich / Ungarn / Slowakei
    [(13.8, 48.8), (16.9, 48.6), (17.1, 47.7),
     (18.8, 48.0), (20.2, 48.5), (22.0, 48.4)],

    # Ungarn / Slowenien / Kroatien / Serbien
    [(17.1, 47.7), (16.5, 46.9), (16.0, 46.5), (19.0, 45.2), (21.5, 45.2)],

    # Rumänien / Ukraine / Moldawien
    [(22.0, 48.4), (22.7, 47.9), (24.9, 47.9), (26.6, 48.3), (28.2, 46.5)],

    # Serbien / Bosnien / Kroatien
    [(19.0, 45.2), (18.4, 44.0), (17.2, 44.5)],

    # Serbien / Montenegro / Albanien
    [(18.4, 44.0), (19.3, 42.9), (19.7, 42.5)],

    # Serbien / Bulgarien / Nordmazedonien
    [(21.9, 43.0), (22.5, 42.3), (22.0, 41.1)],

    # Albanien / Nordmazedonien / Griechenland
    [(19.7, 42.5), (20.6, 41.1), (21.0, 41.1)],
    [(20.0, 39.8), (20.5, 40.0), (20.6, 41.1)],

    # Nordmazedonien / Griechenland / Bulgarien
    [(22.0, 41.1), (22.9, 41.3)],
    [(22.9, 41.3), (24.5, 41.6), (26.1, 41.7)],

    # Bulgarien / Rumänien (Donau)
    [(22.7, 44.2), (25.0, 43.7), (27.2, 44.0), (28.0, 43.7)],

    # Norwegen / Schweden
    [(12.5, 66.2), (12.3, 64.0), (12.2, 61.0), (11.9, 59.1)],

    # Norwegen / Finnland
    [(25.0, 69.0), (29.0, 69.8)],

    # Schweden / Finnland
    [(23.5, 66.8), (21.0, 68.4)],

    # Finnland / Russland
    [(30.0, 64.9), (30.0, 61.8)],

    # Baltikum: Estland/Lettland, Lettland/Litauen, Litauen/Belarus/Russland
    [(27.4, 57.5), (26.0, 56.3), (21.3, 54.9), (23.5, 52.9)],
]


# ---------------------------------------------------------------------------
# Map regions
# ---------------------------------------------------------------------------
#
# Europe is the hand-drawn outline above. Every other region (North
# America, Asia, Africa) comes from dayz_map_data.py, which is generated
# by tools/build_map_data.py from a Natural Earth GeoJSON. If that file
# is missing, only Europe is available - the report still works.
#
# Which region gets drawn depends on the geo-located server: a server in
# Frankfurt shows Europe, one in Dallas shows North America. See
# select_region().

def shapes_bbox(shapes):
    """(lon_min, lat_min, lon_max, lat_max) of a region's landmass."""
    lons = [lon for shape in shapes for lon, _lat in shape]
    lats = [lat for shape in shapes for _lon, lat in shape]

    return min(lons), min(lats), max(lons), max(lats)


EUROPE_REGION = {
    "name": "Europe",
    "ref_lat": EUROPE_REF_LAT,
    "padding": EUROPE_PADDING,
    "bbox": shapes_bbox(EUROPE_SHAPES),
    "shapes": EUROPE_SHAPES,
    "lakes": [],
    "borders": EUROPE_BORDERS,
}

MAP_REGIONS = {"europe": EUROPE_REGION}

try:
    from dayz_map_data import REGIONS as GENERATED_REGIONS
except ImportError:
    GENERATED_REGIONS = {}

for _key, _region in GENERATED_REGIONS.items():
    _region.setdefault("lakes", [])
    MAP_REGIONS[_key] = _region

FALLBACK_REGION = "europe"


def project_point(lon, lat, ref_lat=EUROPE_REF_LAT):
    """Simplified equirectangular projection with a latitude correction
    (one reference parallel) so the region is not stretched sideways.
    Plenty good enough for a decorative mini map.
    """
    x = lon * math.cos(math.radians(ref_lat))
    y = -lat
    return x, y


def point_in_ring(lon, lat, ring):
    """Ray casting: is the point inside this closed ring?"""
    inside = False

    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat):
            crossing = x1 + (lat - y1) / (y2 - y1) * (x2 - x1)

            if lon < crossing:
                inside = not inside

    return inside


def region_covers(region, lon, lat):
    """Does the location sit on this region's landmass?"""
    return any(
        point_in_ring(lon, lat, shape)
        for shape in region["shapes"]
    )


def region_edge_margin(region, lon, lat):
    """How far is the location from the edge of the map (in degrees)?

    This settles overlaps: Cairo is on both the Africa and the Asia
    map, but on the Asia map it clings to the left edge. The region
    where the marker sits deepest inside the frame shows it best.
    """
    lon_min, lat_min, lon_max, lat_max = region["bbox"]
    scale = math.cos(math.radians(lat))

    return min(
        (lon - lon_min) * scale,
        (lon_max - lon) * scale,
        lat - lat_min,
        lat_max - lat,
    )


def region_distance(region, lon, lat):
    """Distance (in degrees) to the nearest vertex of the region."""
    scale = math.cos(math.radians(lat))
    best = float("inf")

    for shape in region["shapes"]:
        for plon, plat in shape:
            dx = (plon - lon) * scale
            dy = plat - lat
            best = min(best, dx * dx + dy * dy)

    return math.sqrt(best)


def select_region(lon, lat, forced=None):
    """Pick the map region for the server location.

    1. If the location sits on a region's landmass, that region wins.
       When regions overlap (Cairo is on both the Africa and the Asia
       map), the one where the marker sits furthest from the edge of
       the frame wins.
    2. Otherwise the region with the closest land. The marker still
       stays visible because region_bounds() folds it into the viewport.
    """
    if forced:
        if forced not in MAP_REGIONS:
            raise ValueError(
                f"Unknown map region '{forced}'. "
                f"Available: {', '.join(sorted(MAP_REGIONS))}"
            )

        return forced, MAP_REGIONS[forced]

    covering = [
        key for key, region in MAP_REGIONS.items()
        if region_covers(region, lon, lat)
    ]

    if covering:
        key = max(
            covering,
            key=lambda k: region_edge_margin(MAP_REGIONS[k], lon, lat)
        )
        return key, MAP_REGIONS[key]

    key = min(
        MAP_REGIONS,
        key=lambda k: region_distance(MAP_REGIONS[k], lon, lat)
    )

    return key, MAP_REGIONS[key]


def region_bounds(region, marker=None):
    """Viewport (viewBox) of the region.

    The marker is part of the calculation: if the server happens to sit
    outside the region, the map zooms out far enough to keep it visible
    instead of hiding it behind the edge.
    """
    ref_lat = region["ref_lat"]
    padding = region["padding"]

    xs = []
    ys = []

    for shape in region["shapes"]:
        for lon, lat in shape:
            x, y = project_point(lon, lat, ref_lat)
            xs.append(x)
            ys.append(y)

    if marker is not None:
        x, y = project_point(marker[0], marker[1], ref_lat)
        xs.append(x)
        ys.append(y)

    min_x = min(xs) - padding
    min_y = min(ys) - padding

    width = (max(xs) - min(xs)) + padding * 2
    height = (max(ys) - min(ys)) + padding * 2

    return min_x, min_y, width, height


def shape_to_polygon_points(shape, ref_lat=EUROPE_REF_LAT):
    return " ".join(
        f"{x:.2f},{y:.2f}"
        for x, y in (
            project_point(lon, lat, ref_lat) for lon, lat in shape
        )
    )


def render_landmass_svg(region):
    return "\n            ".join(
        f'<polygon class="landmass" '
        f'points="{shape_to_polygon_points(shape, region["ref_lat"])}">'
        f'</polygon>'
        for shape in region["shapes"]
    )


def render_water_svg(region):
    """Inland water (the Caspian Sea, for example) painted over the
    landmass in the water colour - in the country data set those are
    holes in the land."""
    return "\n            ".join(
        f'<polygon class="water-body" '
        f'points="{shape_to_polygon_points(lake, region["ref_lat"])}">'
        f'</polygon>'
        for lake in region.get("lakes", ())
    )


def render_borders_svg(region):
    return "\n            ".join(
        f'<polyline class="country-border" '
        f'points="{shape_to_polygon_points(border, region["ref_lat"])}">'
        f'</polyline>'
        for border in region["borders"]
    )


# Width of the Europe viewport - the reference for scaling the marker,
# because all marker sizes were originally tuned for Europe.
EUROPE_MAP_WIDTH = region_bounds(EUROPE_REGION)[2]



# ---------------------------------------------------------------------------
# Farben
# ---------------------------------------------------------------------------

GREEN_LEVELS = {
    0: "#111713",
    1: "#183321",
    2: "#245d35",
    3: "#3b8d4e",
    4: "#75c442",
}

NO_DATA_COLOR = "#171b19"

PING_SCALE = [
    (50, "#75c442"),
    (100, "#a5c94c"),
    (150, "#d7bf43"),
    (250, "#d98a3a"),
    (float("inf"), "#c5483e"),
]

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr",
    "May", "Jun", "Jul", "Aug",
    "Sep", "Oct", "Nov", "Dec"
]

WEEKDAY_NAMES = [
    "Mon", "Tue", "Wed", "Thu",
    "Fri", "Sat", "Sun"
]


def green_level(value, max_value):
    if not max_value or value is None or value <= 0:
        return 0

    ratio = value / max_value

    if ratio <= 0.25:
        return 1

    if ratio <= 0.50:
        return 2

    if ratio <= 0.75:
        return 3

    return 4


def ping_color(avg_ping):
    if avg_ping is None:
        return NO_DATA_COLOR

    for threshold, color in PING_SCALE:
        if avg_ping < threshold:
            return color

    return PING_SCALE[-1][1]


# ---------------------------------------------------------------------------
# Kalender-Heatmap
# ---------------------------------------------------------------------------

def render_calendar_heatmap(
    stats,
    start,
    end,
    color_fn,
    tooltip_fn
):
    cal_start = (
        start -
        timedelta(days=start.weekday())
    )

    all_days = []
    d = cal_start

    while d <= end:
        all_days.append(d)
        d += timedelta(days=1)

    cells = []
    month_labels = {}
    week_index = 0

    for i, day in enumerate(all_days):

        if day.weekday() == 0 and i != 0:
            week_index += 1

        if (
            day.day <= 7
            and day.weekday() == 0
        ):
            month_labels[
                week_index
            ] = MONTH_NAMES[day.month - 1]

        s = stats.get(day)

        color = color_fn(s)

        tooltip = html.escape(
            tooltip_fn(day, s),
            quote=True
        )

        cells.append(
            f'<div class="cell" '
            f'style="background:{color};'
            f'grid-column:{week_index + 1};'
            f'grid-row:{day.weekday() + 1};" '
            f'title="{tooltip}"></div>'
        )

    months_html = "".join(
        f'<span style="grid-column:{c + 1};">'
        f'{n}</span>'
        for c, n in sorted(
            month_labels.items()
        )
    )

    weekdays_html = "".join(
        f"<span>{w}</span>"
        for w in WEEKDAY_NAMES
    )

    return (
        months_html,
        weekdays_html,
        "".join(cells)
    )


def player_tooltip(day, s):
    if (
        s is None
        or s["avg_players"] is None
    ):
        return (
            f"{day.strftime('%Y-%m-%d')}: "
            f"no data"
        )

    return (
        f"{day.strftime('%Y-%m-%d')}: "
        f"avg {s['avg_players']:.1f} players, "
        f"peak {s['peak_players']} "
        f"({s['scans']} scans)"
    )


def ping_tooltip(day, s):
    if (
        s is None
        or s["avg_ping"] is None
    ):
        return (
            f"{day.strftime('%Y-%m-%d')}: "
            f"no data"
        )

    return (
        f"{day.strftime('%Y-%m-%d')}: "
        f"avg {s['avg_ping']:.0f} ms "
        f"({s['scans']} scans)"
    )


# ---------------------------------------------------------------------------
# Muster-Heatmap
# ---------------------------------------------------------------------------

def render_pattern_heatmap(pattern):
    max_value = (
        max(pattern.values())
        if pattern
        else 0
    )

    cells = []

    for weekday in range(7):
        for hour in range(24):

            value = pattern.get(
                (weekday, hour)
            )

            if value is not None:
                color = GREEN_LEVELS[
                    green_level(
                        value,
                        max_value
                    )
                ]

                tooltip = (
                    f"{WEEKDAY_NAMES[weekday]} "
                    f"{hour:02d}:00: "
                    f"avg {value:.1f} players"
                )

            else:
                color = NO_DATA_COLOR

                tooltip = (
                    f"{WEEKDAY_NAMES[weekday]} "
                    f"{hour:02d}:00: "
                    f"no data"
                )

            tooltip = html.escape(
                tooltip,
                quote=True
            )

            cells.append(
                f'<div class="cell" '
                f'style="background:{color};'
                f'grid-column:{hour + 1};'
                f'grid-row:{weekday + 1};" '
                f'title="{tooltip}"></div>'
            )

    hour_labels = "".join(
        f'<span style="grid-column:{h + 1};">'
        f'{h if h % 3 == 0 else ""}'
        f'</span>'
        for h in range(24)
    )

    weekday_labels = "".join(
        f"<span>{w}</span>"
        for w in WEEKDAY_NAMES
    )

    return (
        hour_labels,
        weekday_labels,
        "".join(cells)
    )


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def build_html(
    rows,
    stats,
    pattern,
    summary,
    location,
    start,
    end,
    out_path,
    map_region=None
):

    max_players_all = max(
        (
            s["avg_players"]
            for s in stats.values()
            if s["avg_players"] is not None
        ),
        default=0
    )

    p_months, p_weekdays, p_cells = (
        render_calendar_heatmap(
            stats,
            start,
            end,
            color_fn=lambda s:
                GREEN_LEVELS[
                    green_level(
                        s["avg_players"]
                        if s else None,
                        max_players_all
                    )
                ]
                if s
                else NO_DATA_COLOR,
            tooltip_fn=player_tooltip,
        )
    )

    g_months, g_weekdays, g_cells = (
        render_calendar_heatmap(
            stats,
            start,
            end,
            color_fn=lambda s:
                ping_color(
                    s["avg_ping"]
                    if s
                    else None
                ),
            tooltip_fn=ping_tooltip,
        )
    )

    hour_labels, pat_weekdays, pat_cells = (
        render_pattern_heatmap(pattern)
    )

    green_legend = "".join(
        f'<span class="cell" '
        f'style="background:{GREEN_LEVELS[i]};">'
        f'</span>'
        for i in range(5)
    )

    ping_legend = "".join(
        f'<span class="cell" '
        f'style="background:{c};">'
        f'</span>'
        for _, c in PING_SCALE
    )

    def fmt(v, suffix="", digits=1):
        if v is None:
            return "n/a"

        return f"{v:.{digits}f}{suffix}"

    city = location.get("city") or ""

    country = location.get("country") or ""

    isp = location.get("isp") or ""

    lat = float(
        location.get("lat")
        if location.get("lat") is not None
        else FALLBACK_LAT
    )

    lon = float(
        location.get("lon")
        if location.get("lon") is not None
        else FALLBACK_LON
    )

    # -----------------------------------------------------------------
    # Karte: Umriss-Polygone + Marker-Position projizieren
    # -----------------------------------------------------------------

    region_key, region = select_region(lon, lat, forced=map_region)

    map_min_x, map_min_y, map_w, map_h = region_bounds(
        region,
        marker=(lon, lat)
    )

    map_view_box = (
        f"{map_min_x:.2f} {map_min_y:.2f} "
        f"{map_w:.2f} {map_h:.2f}"
    )

    map_aspect_ratio = map_w / map_h

    region_name = html.escape(region["name"])

    landmass_svg = render_landmass_svg(region)
    water_svg = render_water_svg(region)
    borders_svg = render_borders_svg(region)

    marker_x, marker_y = project_point(lon, lat, region["ref_lat"])

    # Marker sizes are given in projection units and were tuned for
    # Europe. On a larger region (North America/Asia) the dot has to
    # grow with it, otherwise it is a single pixel.
    marker_scale = max(1.0, map_w / EUROPE_MAP_WIDTH)

    # Die Karte zeigt nur Europa. Liegt der Server woanders (oder schlaegt der
    # Geo-Lookup fehl), landet der Marker ausserhalb der viewBox und waere
    # unsichtbar - dann klemmen wir ihn an den Rand und sagen das im Tooltip,
    # statt ihn kommentarlos verschwinden zu lassen.
    marker_off_map = not (
        map_min_x <= marker_x <= map_min_x + map_w
        and map_min_y <= marker_y <= map_min_y + map_h
    )

    if marker_off_map:
        edge = 0.6  # etwas Abstand, damit der Punkt nicht angeschnitten wird
        marker_x = min(max(marker_x, map_min_x + edge), map_min_x + map_w - edge)
        marker_y = min(max(marker_y, map_min_y + edge), map_min_y + map_h - edge)

    marker_title = html.escape(
        f"{city}, {country} \u2014 {isp} \u2014 "
        f"{lat:.4f}, {lon:.4f}"
        + (
            " \u2014 outside the map area, "
            "marker pinned to the nearest edge"
            if marker_off_map else ""
        )
    )

    html_document = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport"
      content="width=device-width, initial-scale=1">

<title>DayZ Server Report</title>

<style>

:root {{
    --bg: #262624;
    --panel: #2d2d2a;
    --panel2: #333330;
    --border: #45443f;

    --text: #eeece6;
    --muted: #a3a299;

    --green: #75c442;
    --green-bright: #8bd84f;

    --water: #262624;
    --country: #35342f;
    --country-hover: #3d3c36;
    --country-border: #55534b;

    --map-ratio: {map_aspect_ratio:.4f};
}}

* {{
    box-sizing: border-box;
}}

html,
body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);

    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Roboto,
        Helvetica,
        Arial,
        sans-serif;
}}

body {{
    padding: 34px 24px 60px;
}}

.container {{
    width: min(1120px, 100%);
    margin: auto;
}}

.header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 30px;
    margin-bottom: 24px;
}}

.brand {{
    display: flex;
    align-items: center;
    gap: 13px;
}}

.brand-mark {{
    width: 13px;
    height: 13px;
    border-radius: 50%;
    background: var(--green);

    box-shadow:
        0 0 0 5px rgba(117,196,66,.10),
        0 0 18px rgba(117,196,66,.32);
}}

h1 {{
    margin: 0;
    font-size: 24px;
    line-height: 1.2;
    font-weight: 600;
    letter-spacing: -0.3px;
}}

.sub {{
    margin-top: 7px;
    color: var(--muted);
    font-size: 13px;
}}

.server-badge {{
    padding: 8px 12px;

    border: 1px solid var(--border);
    border-radius: 5px;

    background: #0d100e;

    color: var(--muted);

    font-size: 12px;
    white-space: nowrap;
}}

.server-badge strong {{
    color: var(--green-bright);
    font-weight: 600;
}}

.stats {{
    display: grid;
    grid-template-columns:
        repeat(5, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 18px;
}}

.stat {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 5px;
    padding: 14px 15px;
}}

.stat .label {{
    color: var(--muted);
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: .7px;
}}

.stat .value {{
    margin-top: 5px;
    color: var(--text);
    font-size: 19px;
    font-weight: 600;
}}

.map-card {{
    position: relative;
    overflow: hidden;

    width: 100%;
    max-width: 620px;
    aspect-ratio: var(--map-ratio);

    margin: 0 auto;

    border: 1px solid var(--border);
    border-radius: 6px;

    background: var(--water);
}}

.europe-map {{
    display: block;
    width: 100%;
    height: 100%;
    cursor: default;
    touch-action: none;
}}

.europe-map.is-zoomed {{
    cursor: grab;
}}

.europe-map.is-dragging {{
    cursor: grabbing;
}}

.map-zoom-controls {{
    position: absolute;
    right: 12px;
    bottom: 12px;
    z-index: 2;

    display: flex;
    flex-direction: column;
    gap: 6px;
}}

.map-zoom-btn {{
    width: 26px;
    height: 26px;

    display: flex;
    align-items: center;
    justify-content: center;

    background: rgba(20, 20, 18, .72);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 5px;

    font-size: 15px;
    line-height: 1;
    cursor: pointer;

    transition: background .15s ease;
}}

.map-zoom-btn:hover {{
    background: rgba(35, 35, 32, .9);
}}

.map-zoom-reset {{
    font-size: 12px;
}}

.map-hint {{
    position: absolute;
    left: 16px;
    bottom: 10px;
    z-index: 2;

    color: #8a8a88;
    font-size: 10px;
    letter-spacing: .3px;

    pointer-events: none;
}}

.landmass {{
    fill: var(--country);
    stroke: none;

    transition: fill .15s ease;
}}

.map-card:hover .landmass {{
    fill: var(--country-hover);
}}

.water-body {{
    fill: var(--water);
    stroke: none;
    pointer-events: none;
}}

.country-border {{
    fill: none;
    stroke: var(--country-border);
    stroke-width: 1px;
    stroke-linejoin: round;
    stroke-linecap: round;
    vector-effect: non-scaling-stroke;
    pointer-events: none;
}}

.map-title {{
    position: absolute;
    top: 14px;
    left: 16px;
    z-index: 2;
    pointer-events: none;
}}

.map-title .title {{
    color: #dcdcda;
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
}}

.map-title .description {{
    margin-top: 4px;
    color: #8a8a88;
    font-size: 11px;
}}

.marker-pulse {{
    fill: none;
    stroke: var(--green);
    stroke-width: 1.2px;
    vector-effect: non-scaling-stroke;
    animation: marker-pulse 2.6s ease-out infinite;
}}

.marker-halo {{
    fill: none;
    stroke: var(--green);
    stroke-opacity: .35;
    stroke-width: 1px;
    vector-effect: non-scaling-stroke;
}}

.marker-dot {{
    fill: var(--green-bright);
    stroke: var(--bg);
    stroke-width: 1.4px;
    vector-effect: non-scaling-stroke;
}}

.marker-label {{
    fill: #e6e9e5;
    font-family: inherit;
    font-weight: 600;
    paint-order: stroke fill;
    stroke: var(--bg);
    stroke-width: .28;
    stroke-linejoin: round;
    pointer-events: none;
}}

@keyframes marker-pulse {{
    0%   {{ r: .5; stroke-opacity: .6; }}
    100% {{ r: 3;  stroke-opacity: 0; }}
}}

h2 {{
    margin: 35px 0 11px;

    padding-bottom: 8px;

    border-bottom: 1px solid var(--border);

    color: #d5dad5;

    font-size: 14px;
    font-weight: 600;

    letter-spacing: .1px;
}}

.heatmap-wrap {{
    overflow-x: auto;
    padding-bottom: 2px;
}}

.months,
.hourlabels {{
    display: grid;
    grid-auto-flow: column;

    margin-left: 32px;

    height: 17px;

    color: #747d74;

    font-size: 10px;
}}

.months {{
    grid-auto-columns: 15px;
}}

.hourlabels {{
    grid-auto-columns: 15.3px;
}}

.weekdays {{
    display: grid;

    grid-template-rows:
        repeat(7, 12px);

    gap: 3px;

    float: left;

    margin-right: 6px;

    color: #747d74;

    font-size: 9px;
}}

.grid {{
    display: grid;

    grid-auto-flow: column;

    grid-template-rows:
        repeat(7, 12px);

    grid-auto-columns: 12px;

    gap: 3px;

    margin-left: 32px;
}}

.pattern-grid {{
    display: grid;

    grid-template-columns:
        repeat(24, 12px);

    grid-template-rows:
        repeat(7, 12px);

    gap: 3px;

    margin-left: 32px;
}}

.cell {{
    width: 12px;
    height: 12px;

    border-radius: 2px;

    display: inline-block;

    transition:
        transform .08s ease,
        filter .08s ease;
}}

.cell:hover {{
    transform: scale(1.25);
    filter: brightness(1.25);
    z-index: 2;
}}

.legend {{
    display: flex;

    align-items: center;

    gap: 4px;

    margin-top: 12px;

    color: #727a72;

    font-size: 10px;
}}

.legend .cell {{
    width: 11px;
    height: 11px;
}}

.clearfix::after {{
    content: "";
    display: table;
    clear: both;
}}

@media (max-width: 800px) {{

    body {{
        padding: 20px 12px 40px;
    }}

    .header {{
        display: block;
    }}

    .server-badge {{
        display: inline-block;
        margin-top: 12px;
    }}

    .stats {{
        grid-template-columns:
            repeat(2, minmax(0, 1fr));
    }}
}}

@media (max-width: 480px) {{

    .stats {{
        grid-template-columns:
            1fr 1fr;
    }}

    .stat {{
        padding: 11px;
    }}

    .stat .value {{
        font-size: 16px;
    }}
}}

</style>
</head>

<body>

<div class="container">

    <div class="header">

        <div>

            <div class="brand">

                <div class="brand-mark"></div>

                <div>
                    <h1>DayZ Server Report</h1>

                    <div class="sub">
                        {html.escape(summary.get("ip", ""))}
                        &middot;
                        {html.escape(format_location(location))}
                        &middot;
                        {start.strftime("%Y-%m-%d")}
                        &ndash;
                        {end.strftime("%Y-%m-%d")}
                    </div>
                </div>

            </div>

        </div>

        <div class="server-badge">
            <strong>ONLINE SERVER</strong>
            &nbsp;&middot;&nbsp;
            {html.escape(city)}
        </div>

    </div>


    <div class="stats">

        <div class="stat">
            <div class="label">Avg Ping</div>
            <div class="value">
                {fmt(summary["avg_ping"], " ms", 0)}
            </div>
        </div>

        <div class="stat">
            <div class="label">Avg Players</div>
            <div class="value">
                {fmt(summary["avg_players"])}
            </div>
        </div>

        <div class="stat">
            <div class="label">Peak Players</div>
            <div class="value">
                {
                    summary["peak_players"]
                    if summary["peak_players"] is not None
                    else "n/a"
                }
            </div>
        </div>

        <div class="stat">
            <div class="label">Days Logged</div>
            <div class="value">
                {summary["days_logged"]}
            </div>
        </div>

        <div class="stat">
            <div class="label">Uptime</div>
            <div class="value">
                {fmt(summary["uptime_pct"], "%", 1)}
            </div>
        </div>

    </div>


    <h2>Server Location</h2>

    <div class="map-card">

        <div class="map-title">
            <div class="title">{region_name}</div>
            <div class="description">
                Server location
            </div>
        </div>

        <svg class="europe-map"
             id="europeMap"
             viewBox="{map_view_box}"
             preserveAspectRatio="xMidYMid meet"
             role="img"
             aria-label="Map {region_name}: server location {html.escape(city)}, {html.escape(country)}">

            {landmass_svg}

            {water_svg}

            {borders_svg}

            <g transform="translate({marker_x:.2f},{marker_y:.2f}) scale({marker_scale:.3f})">
                <circle class="marker-pulse" r=".5"></circle>
                <circle class="marker-halo" r=".85"></circle>
                <circle class="marker-dot" r=".42"></circle>
                <circle r=".14" fill="#ffffff" fill-opacity=".85"></circle>
                <text class="marker-label"
                      x="1.0" y=".45"
                      font-size="1.15">{html.escape(city)}</text>
                <title>{marker_title}</title>
            </g>

        </svg>

        <div class="map-zoom-controls">
            <button type="button" class="map-zoom-btn"
                    data-zoom="in" aria-label="Zoom in">+</button>
            <button type="button" class="map-zoom-btn"
                    data-zoom="out" aria-label="Zoom out">&minus;</button>
            <button type="button" class="map-zoom-btn map-zoom-reset"
                    data-zoom="reset" aria-label="Reset zoom">&#8634;</button>
        </div>

        <div class="map-hint">Scroll to zoom</div>

    </div>


    <h2>
        Player Activity per Day
        <span style="color:#727a72;font-weight:400;">
            &nbsp;&middot;&nbsp;Avg Player Count
        </span>
    </h2>

    <div class="heatmap-wrap">

        <div class="months">
            {p_months}
        </div>

        <div class="clearfix">

            <div class="weekdays">
                {p_weekdays}
            </div>

            <div class="grid">
                {p_cells}
            </div>

        </div>

        <div class="legend">
            Less
            {green_legend}
            More
        </div>

    </div>


    <h2>
        Ping Quality per Day
        <span style="color:#727a72;font-weight:400;">
            &nbsp;&middot;&nbsp;Avg Ping
        </span>
    </h2>

    <div class="heatmap-wrap">

        <div class="months">
            {g_months}
        </div>

        <div class="clearfix">

            <div class="weekdays">
                {g_weekdays}
            </div>

            <div class="grid">
                {g_cells}
            </div>

        </div>

        <div class="legend">
            Faster
            {ping_legend}
            Slower
        </div>

    </div>


    <h2>
        Activity Pattern:
        Weekday &times; Hour
        <span style="color:#727a72;font-weight:400;">
            &nbsp;&middot;&nbsp;Avg Players per Hour
        </span>
    </h2>

    <div class="heatmap-wrap">

        <div class="hourlabels">
            {hour_labels}
        </div>

        <div class="clearfix">

            <div class="weekdays">
                {pat_weekdays}
            </div>

            <div class="pattern-grid">
                {pat_cells}
            </div>

        </div>

        <div class="legend">
            Less
            {green_legend}
            More
        </div>

    </div>

</div>

<script>
(function () {{
    var svg = document.getElementById('europeMap');
    if (!svg) return;

    var base = {{
        x: {map_min_x:.4f},
        y: {map_min_y:.4f},
        w: {map_w:.4f},
        h: {map_h:.4f}
    }};

    var scale = 1;
    var offsetX = 0;
    var offsetY = 0;
    var minScale = 1;
    var maxScale = 6;

    function clamp(value, min, max) {{
        return Math.min(max, Math.max(min, value));
    }}

    function apply() {{
        var w = base.w / scale;
        var h = base.h / scale;

        offsetX = clamp(offsetX, 0, Math.max(0, base.w - w));
        offsetY = clamp(offsetY, 0, Math.max(0, base.h - h));

        svg.setAttribute(
            'viewBox',
            (base.x + offsetX) + ' ' + (base.y + offsetY) + ' ' + w + ' ' + h
        );

        svg.classList.toggle('is-zoomed', scale > 1.001);
    }}

    function zoomAt(factor, px, py) {{
        var newScale = clamp(scale * factor, minScale, maxScale);
        if (newScale === scale) return;

        var w = base.w / scale;
        var h = base.h / scale;
        var focusX = base.x + offsetX + px * w;
        var focusY = base.y + offsetY + py * h;

        scale = newScale;

        var newW = base.w / scale;
        var newH = base.h / scale;

        offsetX = focusX - px * newW - base.x;
        offsetY = focusY - py * newH - base.y;

        apply();
    }}

    svg.addEventListener('wheel', function (event) {{
        event.preventDefault();

        var rect = svg.getBoundingClientRect();
        var px = (event.clientX - rect.left) / rect.width;
        var py = (event.clientY - rect.top) / rect.height;

        zoomAt(event.deltaY < 0 ? 1.2 : 1 / 1.2, px, py);
    }}, {{ passive: false }});

    var dragging = false;
    var lastX = 0;
    var lastY = 0;

    svg.addEventListener('mousedown', function (event) {{
        if (scale <= 1.001) return;

        dragging = true;
        lastX = event.clientX;
        lastY = event.clientY;
        svg.classList.add('is-dragging');
    }});

    window.addEventListener('mousemove', function (event) {{
        if (!dragging) return;

        var rect = svg.getBoundingClientRect();
        var w = base.w / scale;
        var h = base.h / scale;

        offsetX -= (event.clientX - lastX) / rect.width * w;
        offsetY -= (event.clientY - lastY) / rect.height * h;

        lastX = event.clientX;
        lastY = event.clientY;

        apply();
    }});

    window.addEventListener('mouseup', function () {{
        dragging = false;
        svg.classList.remove('is-dragging');
    }});

    var buttons = document.querySelectorAll('.map-zoom-btn');

    for (var i = 0; i < buttons.length; i++) {{
        buttons[i].addEventListener('click', function (event) {{
            var action = event.currentTarget.getAttribute('data-zoom');

            if (action === 'reset') {{
                scale = 1;
                offsetX = 0;
                offsetY = 0;
                apply();
                return;
            }}

            zoomAt(action === 'in' ? 1.4 : 1 / 1.4, 0.5, 0.5);
        }});
    }}
}})();
</script>

</body>
</html>
"""

    with open(
        out_path,
        "w",
        encoding="utf-8"
    ) as f:
        f.write(html_document)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():

    args = parse_args()

    rows = load_rows(args.log)

    if not rows:
        print(
            f"No data found in {args.log}. "
            f"Let the monitor run for a while first."
        )
        return


    stats = compute_daily_stats(rows)

    pattern = compute_hourly_pattern(rows)

    summary = compute_summary(rows)

    summary["ip"] = args.ip


    end = date.today()

    if args.days:
        start = (
            end -
            timedelta(days=args.days - 1)
        )
    else:
        start = (
            summary["first_date"].date()
            if summary["first_date"]
            else end
        )


    base_dir = (
        os.path.dirname(
            os.path.abspath(args.log)
        )
        or "."
    )


    # ---------------------------------------------------------------
    # Geo-IP
    # ---------------------------------------------------------------

    location_cache = os.path.join(
        base_dir,
        ".dayz_location_cache.json"
    )

    location = None

    if not args.no_geo:
        location = lookup_location(
            args.ip,
            location_cache
        )

    location = ensure_coordinates(
        location
    )


    # ---------------------------------------------------------------
    # HTML
    # ---------------------------------------------------------------

    build_html(
        rows=rows,
        stats=stats,
        pattern=pattern,
        summary=summary,
        location=location,
        start=start,
        end=end,
        out_path=args.out,
        map_region=args.map_region
    )


    out_abspath = os.path.abspath(
        args.out
    )

    print()
    print(
        f"Report created: {out_abspath}"
    )

    print(
        f"Server: {args.ip}"
    )

    print(
        f"Location: {format_location(location)}"
    )

    print(
        f"Coordinates: "
        f"{location['lat']:.4f}, "
        f"{location['lon']:.4f}"
    )

    if not args.no_browser:

        # Auf einem headless Server (z.B. dem Raspberry Pi) gibt es keinen
        # Browser. webbrowser.open wirft dort je nach System einen Fehler -
        # der Report ist da aber laengst geschrieben, das darf nicht abbrechen.
        try:
            opened = webbrowser.open("file://" + out_abspath)
        except Exception:
            opened = False

        if not opened:
            print()
            print(
                "No browser found - just open the file above manually "
                "(or start with --no-browser to skip this message)."
            )


if __name__ == "__main__":
    main()
