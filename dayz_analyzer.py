"""
DayZ Server Analyzer
Analyzes dayz_monitor_log.csv and creates:
    - a GitHub-style activity calendar
    - a weekday/hour heatmap
    - daily statistics
    - a text report

Required packages:
    pip install pandas matplotlib

Usage:
    python dayz_analyzer.py

Optional:
    python dayz_analyzer.py --log dayz_monitor_log.csv
    python dayz_analyzer.py --output reports
"""

from pathlib import Path
import argparse
import sys

# Windows-Konsolen benutzen standardmaessig cp1252 und koennen die Haken-/
# Umlautzeichen unten nicht darstellen - ohne das hier bricht das Skript mit
# einem UnicodeEncodeError ab, sobald es die erste Statuszeile ausgibt.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap


# ============================================================================
# KONFIGURATION
# ============================================================================

DEFAULT_LOG = "dayz_monitor_log.csv"
DEFAULT_OUTPUT = "reports"

# Gewichtung für den Aktivitäts-Score
# 70 % Serverauslastung
# 30 % Join/Leave-Aktivität
PLAYER_WEIGHT = 0.70
EVENT_WEIGHT = 0.30


# ============================================================================
# ARGUMENTE
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="DayZ Server Log Analyzer"
    )

    parser.add_argument(
        "--log",
        default=DEFAULT_LOG,
        help=f"CSV file (default: {DEFAULT_LOG})"
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output folder (default: {DEFAULT_OUTPUT})"
    )

    return parser.parse_args()


# ============================================================================
# CSV LADEN
# ============================================================================

def load_log(path):
    path = Path(path)

    if not path.exists():
        print("ERROR: log file not found:")
        print(f"       {path.resolve()}")
        print()
        print("The file is only created while the monitor runs. So start")
        print("python dayz_monitor.py first and let it run for a while.")
        sys.exit(1)

    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"ERROR reading the CSV: {e}")
        sys.exit(1)

    required_columns = [
        "timestamp",
        "status",
        "ping_ms",
        "players",
        "max_players",
        "event",
        "details",
    ]

    missing = [
        column for column in required_columns
        if column not in df.columns
    ]

    if missing:
        print("ERROR: the following columns are missing:")
        for column in missing:
            print(f"  - {column}")
        sys.exit(1)

    # Zeitstempel konvertieren
    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        errors="coerce"
    )

    # Ungültige Zeitstempel entfernen
    df = df.dropna(subset=["timestamp"]).copy()

    # Numerische Werte konvertieren
    df["players"] = pd.to_numeric(
        df["players"],
        errors="coerce"
    ).fillna(0)

    df["max_players"] = pd.to_numeric(
        df["max_players"],
        errors="coerce"
    ).fillna(0)

    df["ping_ms"] = pd.to_numeric(
        df["ping_ms"],
        errors="coerce"
    )

    # Offline-Einträge erkennen
    df["online"] = df["status"].astype(str).str.lower() == "online"

    # Ereignisse vereinheitlichen
    df["event"] = (
        df["event"]
        .fillna("")
        .astype(str)
        .str.lower()
        .str.strip()
    )

    # Chronologisch sortieren
    df = df.sort_values("timestamp").reset_index(drop=True)

    return df


# ============================================================================
# AKTIVITÄT BERECHNEN
# ============================================================================

def calculate_activity(df):
    """
    Erstellt einen Aktivitätswert pro Tag.

    Der Score basiert auf:
        70 % durchschnittliche Serverauslastung
        30 % Join/Leave-Aktivität

    Beide Werte werden innerhalb des gesamten Zeitraums normalisiert.
    """

    online = df[df["online"]].copy()

    if online.empty:
        return pd.DataFrame()

    online["date"] = online["timestamp"].dt.date

    # ------------------------------------------------------------------------
    # Spielerzahl pro Tag
    # ------------------------------------------------------------------------

    daily_players = (
        online
        .groupby("date")
        .agg(
            avg_players=("players", "mean"),
            max_players=("players", "max"),
            avg_ping=("ping_ms", "mean"),
            samples=("players", "count"),
        )
    )

    # ------------------------------------------------------------------------
    # Join / Leave Events pro Tag
    # ------------------------------------------------------------------------

    events = df[
        df["event"].isin(["join", "leave", "server_empty"])
    ].copy()

    if not events.empty:
        events["date"] = events["timestamp"].dt.date

        daily_events = (
            events
            .groupby("date")
            .size()
            .rename("events")
        )
    else:
        daily_events = pd.Series(dtype=float, name="events")

    # Zusammenführen
    daily = daily_players.join(
        daily_events,
        how="left"
    )

    daily["events"] = daily["events"].fillna(0)

    # ------------------------------------------------------------------------
    # Normalisierung 0-1
    # ------------------------------------------------------------------------

    max_avg_players = daily["avg_players"].max()

    if max_avg_players > 0:
        daily["player_score"] = (
            daily["avg_players"] / max_avg_players
        )
    else:
        daily["player_score"] = 0

    max_events = daily["events"].max()

    if max_events > 0:
        daily["event_score"] = (
            daily["events"] / max_events
        )
    else:
        daily["event_score"] = 0

    # ------------------------------------------------------------------------
    # Gesamtscore
    # ------------------------------------------------------------------------

    daily["activity_score"] = (
        daily["player_score"] * PLAYER_WEIGHT
        +
        daily["event_score"] * EVENT_WEIGHT
    )

    daily["activity_score"] *= 100

    return daily


# ============================================================================
# GITHUB-STYLE KALENDER
# ============================================================================

def create_calendar_heatmap(daily, output_path):
    if daily.empty:
        print("No data available for the calendar heatmap.")
        return

    # Datum als Datetime
    data = daily.copy()
    data.index = pd.to_datetime(data.index)

    first_date = data.index.min()
    last_date = data.index.max()

    # Kalender auf Wochen ausrichten
    calendar_start = (
        first_date - pd.Timedelta(days=(first_date.weekday() + 1) % 7)
    )

    calendar_end = (
        last_date + pd.Timedelta(days=(6 - last_date.weekday()))
    )

    all_days = pd.date_range(
        calendar_start,
        calendar_end,
        freq="D"
    )

    calendar = pd.DataFrame(
        index=all_days,
        columns=["activity"]
    )

    calendar["activity"] = data["activity_score"]

    # Sonntag = 0 ... Samstag = 6
    calendar["weekday"] = (
        calendar.index.weekday + 1
    ) % 7

    # Woche seit Start
    calendar["week"] = (
        (calendar.index - calendar.index.min()).days // 7
    )

    weeks = calendar["week"].max() + 1

    matrix = pd.DataFrame(
        0.0,
        index=range(7),
        columns=range(weeks)
    )

    for date, row in calendar.iterrows():
        matrix.loc[
            row["weekday"],
            row["week"]
        ] = row["activity"] if pd.notna(row["activity"]) else 0

    # ------------------------------------------------------------------------
    # GitHub-artige Farbskala
    # ------------------------------------------------------------------------

    colors = [
        "#161b22",
        "#0e4429",
        "#006d32",
        "#26a641",
        "#39d353",
    ]

    cmap = LinearSegmentedColormap.from_list(
        "github_green",
        colors
    )

    fig, ax = plt.subplots(
        figsize=(max(12, weeks * 0.25), 3.8)
    )

    image = ax.imshow(
        matrix.values,
        cmap=cmap,
        vmin=0,
        vmax=100,
        aspect="equal"
    )

    # ------------------------------------------------------------------------
    # Achsen
    # ------------------------------------------------------------------------

    ax.set_yticks(range(7))
    ax.set_yticklabels([
        "Sun",
        "Mon",
        "Tue",
        "Wed",
        "Thu",
        "Fri",
        "Sat"
    ])

    # Monatsnamen oben anzeigen
    month_positions = []
    month_labels = []

    for month in pd.date_range(
        first_date.replace(day=1),
        last_date,
        freq="MS"
    ):
        days_from_start = (
            month - calendar_start
        ).days

        week = days_from_start // 7

        month_positions.append(week)
        month_labels.append(
            month.strftime("%b %Y")
        )

    ax.set_xticks(month_positions)
    ax.set_xticklabels(
        month_labels,
        rotation=0,
        ha="left"
    )

    # Keine Tick-Striche
    ax.tick_params(
        axis="both",
        which="both",
        length=0
    )

    # ------------------------------------------------------------------------
    # Titel
    # ------------------------------------------------------------------------

    ax.set_title(
        "DayZ Server Activity",
        fontsize=16,
        fontweight="bold",
        loc="left",
        pad=15
    )

    ax.set_xlabel(
        "Days - activity combines server load and join/leave events",
        labelpad=10
    )

    # GitHub-artige Kästchen
    for spine in ax.spines.values():
        spine.set_visible(False)

    # ------------------------------------------------------------------------
    # Colorbar
    # ------------------------------------------------------------------------

    cbar = fig.colorbar(
        image,
        ax=ax,
        orientation="horizontal",
        pad=0.18,
        fraction=0.05
    )

    cbar.set_label("Activity")

    cbar.outline.set_visible(False)

    plt.tight_layout()

    fig.savefig(
        output_path,
        dpi=200,
        bbox_inches="tight",
        facecolor="#0d1117"
    )

    plt.close(fig)

    print(f"✓ Calendar saved: {output_path}")


# ============================================================================
# WOCHENTAG × UHRZEIT HEATMAP
# ============================================================================

def create_hour_heatmap(df, output_path):
    online = df[df["online"]].copy()

    if online.empty:
        return

    online["weekday"] = online["timestamp"].dt.weekday
    online["hour"] = online["timestamp"].dt.hour

    # Durchschnittliche Spielerzahl
    player_matrix = (
        online
        .groupby(["weekday", "hour"])["players"]
        .mean()
        .unstack(fill_value=0)
    )

    # Join / Leave Events
    events = df[
        df["event"].isin(["join", "leave", "server_empty"])
    ].copy()

    if not events.empty:
        events["weekday"] = events["timestamp"].dt.weekday
        events["hour"] = events["timestamp"].dt.hour

        event_matrix = (
            events
            .groupby(["weekday", "hour"])
            .size()
            .unstack(fill_value=0)
        )
    else:
        event_matrix = pd.DataFrame()

    # Alle Stunden ergänzen
    player_matrix = player_matrix.reindex(
        index=range(7),
        columns=range(24),
        fill_value=0
    )

    if not event_matrix.empty:
        event_matrix = event_matrix.reindex(
            index=range(7),
            columns=range(24),
            fill_value=0
        )
    else:
        event_matrix = pd.DataFrame(
            0,
            index=range(7),
            columns=range(24)
        )

    # ------------------------------------------------------------------------
    # Normalisieren
    # ------------------------------------------------------------------------

    max_players = player_matrix.values.max()
    max_events = event_matrix.values.max()

    if max_players > 0:
        player_score = player_matrix / max_players
    else:
        player_score = player_matrix * 0

    if max_events > 0:
        event_score = event_matrix / max_events
    else:
        event_score = event_matrix * 0

    activity = (
        player_score * PLAYER_WEIGHT
        +
        event_score * EVENT_WEIGHT
    ) * 100

    # ------------------------------------------------------------------------
    # Zeichnen
    # ------------------------------------------------------------------------

    fig, ax = plt.subplots(
        figsize=(15, 5)
    )

    colors = [
        "#161b22",
        "#0e4429",
        "#006d32",
        "#26a641",
        "#39d353",
    ]

    cmap = LinearSegmentedColormap.from_list(
        "github_green",
        colors
    )

    image = ax.imshow(
        activity.values,
        cmap=cmap,
        vmin=0,
        vmax=100,
        aspect="auto"
    )

    ax.set_xticks(range(24))
    ax.set_xticklabels([
        f"{hour:02d}" for hour in range(24)
    ])

    ax.set_yticks(range(7))
    ax.set_yticklabels([
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday"
    ])

    ax.set_xlabel("Hour")
    ax.set_ylabel("Weekday")

    ax.set_title(
        "DayZ Server - Activity by Weekday and Hour",
        fontsize=15,
        fontweight="bold",
        pad=15
    )

    cbar = fig.colorbar(
        image,
        ax=ax
    )

    cbar.set_label("Activity")

    plt.tight_layout()

    fig.savefig(
        output_path,
        dpi=200,
        bbox_inches="tight"
    )

    plt.close(fig)

    print(f"✓ Hour heatmap saved: {output_path}")


# ============================================================================
# TEXTREPORT
# ============================================================================

def create_report(df, daily, output_path):
    online = df[df["online"]].copy()

    if online.empty:
        return

    # Grundstatistiken
    avg_players = online["players"].mean()
    max_players = online["players"].max()

    avg_ping = online["ping_ms"].mean()

    joins = (df["event"] == "join").sum()
    leaves = (
        df["event"]
        .isin(["leave", "server_empty"])
        .sum()
    )

    first_date = df["timestamp"].min()
    last_date = df["timestamp"].max()

    # Peak-Zeit
    peak_index = (
        online
        .groupby(online["timestamp"].dt.hour)["players"]
        .mean()
        .idxmax()
    )

    # Aktivster Tag
    most_active_day = daily["activity_score"].idxmax()

    most_active_score = daily.loc[
        most_active_day,
        "activity_score"
    ]

    # Zeitraum
    duration = last_date - first_date

    with open(
        output_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write("=" * 60 + "\n")
        f.write("DAYZ SERVER ANALYTICS\n")
        f.write("=" * 60 + "\n\n")

        f.write(
            f"Period:         {first_date:%Y-%m-%d %H:%M}"
            f" - {last_date:%Y-%m-%d %H:%M}\n"
        )

        f.write(
            f"Duration:       {duration}\n\n"
        )

        f.write("-" * 60 + "\n")
        f.write("SERVER\n")
        f.write("-" * 60 + "\n")

        f.write(
            f"Avg Players:    {avg_players:.2f}\n"
        )

        f.write(
            f"Peak Players:   {max_players:.0f}\n"
        )

        f.write(
            f"Avg Ping:       {avg_ping:.1f} ms\n"
            if pd.notna(avg_ping)
            else "Avg Ping:       no data\n"
        )

        f.write("\n")

        f.write("-" * 60 + "\n")
        f.write("ACTIVITY\n")
        f.write("-" * 60 + "\n")

        f.write(
            f"Joins:          {joins}\n"
        )

        f.write(
            f"Leaves:         {leaves}\n"
        )

        f.write(
            f"Most Active Day: {most_active_day:%Y-%m-%d}\n"
        )

        f.write(
            f"Activity:       {most_active_score:.1f}/100\n"
        )

        f.write(
            f"Peak Hour:      {peak_index:02d}:00 - "
            f"{(peak_index + 1) % 24:02d}:00\n"
        )

        f.write("\n")

        f.write("-" * 60 + "\n")
        f.write("DAILY STATISTICS\n")
        f.write("-" * 60 + "\n\n")

        for date, row in daily.iterrows():

            f.write(
                f"{date} | "
                f"Avg Players: {row['avg_players']:.1f} | "
                f"Peak: {row['max_players']:.0f} | "
                f"Events: {row['events']:.0f} | "
                f"Score: {row['activity_score']:.1f}\n"
            )

    print(f"✓ Report saved: {output_path}")


# ============================================================================
# MAIN
# ============================================================================

def main():

    args = parse_args()

    print()
    print("=" * 60)
    print("DAYZ SERVER ANALYZER")
    print("=" * 60)
    print()

    # Output-Verzeichnis
    output_dir = Path(args.output)
    output_dir.mkdir(
        parents=True,
        exist_ok=True
    )

    # CSV laden
    print(f"Reading log: {Path(args.log).resolve()}")

    df = load_log(args.log)

    print(
        f"✓ {len(df):,} log entries loaded."
    )

    if df.empty:
        print("No data available.")
        return

    print()

    # Aktivität
    print("Calculating activity...")

    daily = calculate_activity(df)

    if daily.empty:
        print("No online data available.")
        return

    print(
        f"✓ {len(daily)} days analyzed."
    )

    print()

    # ------------------------------------------------------------------------
    # Grafiken
    # ------------------------------------------------------------------------

    create_calendar_heatmap(
        daily,
        output_dir / "dayz_activity_calendar.png"
    )

    create_hour_heatmap(
        df,
        output_dir / "dayz_activity_heatmap.png"
    )

    # ------------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------------

    create_report(
        df,
        daily,
        output_dir / "dayz_report.txt"
    )

    print()
    print("=" * 60)
    print("ANALYSIS COMPLETE")
    print("=" * 60)
    print()
    print(
        f"Output: {output_dir.resolve()}"
    )
    print()


if __name__ == "__main__":
    main()
