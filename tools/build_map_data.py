#!/usr/bin/env python3
"""
build_map_data.py - development tool, NOT part of the runtime.

Turns a Natural Earth world map (GeoJSON) into the static map file
``dayz_map_data.py`` that ``dayz_report.py`` uses for every region
except Europe (Europe stays hand-drawn).

Why generate it at all?
    The map in the report is deliberately offline: no Leaflet, no tiles,
    no download at runtime. It is nothing but lon/lat lists in Python
    code. Drawing that by hand is doable for Europe, but not for North
    America/Asia/Africa - so generate it once here and commit the result.

What the script does per region:
    1. load every country whose bounding box touches the region
    2. count edges: edge belongs to exactly 1 country  -> coastline
                    edge belongs to >= 2 countries     -> land border
    3. stitch coastline edges into closed rings (= the landmass; inland
       borders are already gone, so no seams inside the fill), and
       border edges into open polylines
    4. clip to the region (Sutherland-Hodgman / Liang-Barsky)
    5. simplify with Douglas-Peucker, drop tiny islands
    6. nest the rings: a ring inside a ring is inland water (Great Lakes,
       Caspian Sea) and gets cut out, a ring inside that is an island again
    7. print it as a Python literal

Usage:
    python tools/build_map_data.py                      # all regions
    python tools/build_map_data.py --region north_america
    python tools/build_map_data.py --geojson path/to/countries.geojson

GeoJSON source: Natural Earth "admin 0 countries" (1:10m), e.g.
https://raw.githubusercontent.com/datasets/geo-countries/master/data/countries.geojson
"""

import argparse
import json
import math
import os
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)

DEFAULT_GEOJSON = os.path.join(PROJECT_ROOT, ".dayz_europe.geojson")
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "dayz_map_data.py")

# Coordinate grid used for counting edges. Natural Earth uses identical
# vertices on shared borders, so 6 decimals (~10 cm) are plenty to
# recognise them again.
QUANT = 6

# Decimals in the output (~1 km). A stamp-sized map needs no more than
# that, and it keeps the file small.
OUT_PRECISION = 2


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------
#
# bbox      = (lon_min, lat_min, lon_max, lat_max), the map's crop
# ref_lat   = reference parallel of the projection (see project_point)
# tolerance = Douglas-Peucker tolerance in degrees (larger = coarser)
# min_area  = islands below this area (deg^2) are dropped
# padding   = margin around the landmass in projection units
#
# A new region = one entry here + re-run the script.

REGIONS = {
    "north_america": {
        "name": "North America",
        "bbox": (-168.0, 7.0, -52.0, 72.0),
        "ref_lat": 40.0,
        "tolerance": 0.18,
        "min_area": 0.55,
        "padding": 2.5,
    },
    "asia": {
        "name": "Asia",
        "bbox": (25.0, -11.0, 154.0, 78.0),
        "ref_lat": 35.0,
        "tolerance": 0.20,
        "min_area": 0.70,
        "padding": 2.5,
    },
    "africa": {
        "name": "Africa",
        "bbox": (-19.0, -36.0, 52.0, 38.0),
        "ref_lat": 5.0,
        "tolerance": 0.16,
        "min_area": 0.40,
        "padding": 2.5,
    },
}

# Countries that reach into the box geographically but should not be
# part of it (otherwise a sliver of South America sticks to Panama).
REGION_EXCLUDE = {
    "north_america": {"COL", "VEN", "ECU", "BRA", "PER", "GUY", "SUR", "RUS"},
    "asia": set(),
    "africa": set(),
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def rings_of(geometry):
    """Every ring (outer + holes) of a Polygon/MultiPolygon geometry."""
    gtype = geometry.get("type")

    if gtype == "Polygon":
        return list(geometry["coordinates"])

    if gtype == "MultiPolygon":
        out = []

        for polygon in geometry["coordinates"]:
            out.extend(polygon)

        return out

    return []


def feature_bbox(geometry):
    lons = []
    lats = []

    for ring in rings_of(geometry):
        for lon, lat in ring:
            lons.append(lon)
            lats.append(lat)

    if not lons:
        return None

    return min(lons), min(lats), max(lons), max(lats)


def bbox_overlaps(a, b):
    return not (
        a[2] < b[0] or a[0] > b[2] or
        a[3] < b[1] or a[1] > b[3]
    )


def ring_area(points):
    """Absolute shoelace area in deg^2 - only used to compare sizes."""
    total = 0.0

    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1

    return abs(total) / 2.0


def polyline_length(points):
    return sum(
        math.hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(points, points[1:])
    )


def point_in_ring(point, ring):
    """Ray casting. Points exactly on an edge do not matter here - the
    test only ever runs on vertices of other rings."""
    lon, lat = point
    inside = False

    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat):
            crossing = x1 + (lat - y1) / (y2 - y1) * (x2 - x1)

            if lon < crossing:
                inside = not inside

    return inside


def ring_inside_ring(inner, outer):
    """Rings never overlap (shared edges are filtered out), so a single
    vertex decides. Probing an edge midpoint avoids hits on shared
    corners."""
    (x1, y1), (x2, y2) = inner[0], inner[1]
    probe = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    return point_in_ring(probe, outer)


def split_land_and_water(rings):
    """Work out the nesting depth: 0 = landmass, 1 = lake inside it,
    2 = island in that lake, ...

    Without this the Great Lakes or the Caspian Sea would be filled as
    land - in the country data set that is exactly what they are: a ring
    enclosed by coastline edges.
    """
    ordered = sorted(rings, key=ring_area, reverse=True)
    land = []
    water = []

    for index, ring in enumerate(ordered):
        depth = 0

        # Only larger rings can possibly contain this one.
        for other in ordered[:index]:
            if ring_inside_ring(ring, other):
                depth += 1

        if depth % 2 == 0:
            land.append(ring)
        else:
            water.append(ring)

    return land, water


# ---------------------------------------------------------------------------
# Edges -> polylines
# ---------------------------------------------------------------------------

def quantize(point):
    return (round(point[0], QUANT), round(point[1], QUANT))


def edge_key(a, b):
    return (a, b) if a <= b else (b, a)


def collect_edges(features):
    """Counts how many countries share each edge.

    Returns: dict edge -> number of countries. The edge is undirected
    (endpoints sorted) so USA->CAN and CAN->USA are the same one.
    """
    counts = {}

    for _iso, geometry in features:
        seen = set()

        for ring in rings_of(geometry):
            quantized = [quantize(point) for point in ring]

            for a, b in zip(quantized, quantized[1:]):
                if a == b:
                    continue

                key = edge_key(a, b)

                # A single country can contain the same edge twice
                # (enclaves etc.) - that must not count as "shared".
                if key in seen:
                    continue

                seen.add(key)
                counts[key] = counts.get(key, 0) + 1

    return counts


def build_adjacency(edges):
    adjacency = {}

    for a, b in edges:
        adjacency.setdefault(a, []).append(b)
        adjacency.setdefault(b, []).append(a)

    return adjacency


def stitch(edges, closed):
    """Stitches a set of edges back into polylines.

    closed=True  -> closed rings expected (coastlines)
    closed=False -> open chains allowed (land borders)
    """
    remaining = set(edges)
    adjacency = build_adjacency(remaining)
    lines = []

    def walk(start):
        line = [start]
        current = start

        while True:
            nxt = None

            for candidate in adjacency.get(current, ()):
                if edge_key(current, candidate) in remaining:
                    nxt = candidate
                    break

            if nxt is None:
                return line

            remaining.discard(edge_key(current, nxt))
            line.append(nxt)
            current = nxt

            if current == start:
                return line

    if not closed:
        # Start at real endpoints/junctions first so borders are not
        # cut apart somewhere in the middle.
        starts = [
            point for point, neighbours in adjacency.items()
            if len(neighbours) != 2
        ]

        for start in starts:
            while any(
                edge_key(start, neighbour) in remaining
                for neighbour in adjacency.get(start, ())
            ):
                lines.append(walk(start))

    while remaining:
        a, _b = next(iter(remaining))
        lines.append(walk(a))

    return [line for line in lines if len(line) >= 2]


# ---------------------------------------------------------------------------
# Clipping
# ---------------------------------------------------------------------------

def clip_ring(points, bbox):
    """Sutherland-Hodgman: clip a polygon to the bounding box."""
    lon_min, lat_min, lon_max, lat_max = bbox

    def clip_half_plane(polygon, inside, intersect):
        if not polygon:
            return []

        result = []
        previous = polygon[-1]

        for current in polygon:
            if inside(current):
                if not inside(previous):
                    result.append(intersect(previous, current))

                result.append(current)
            elif inside(previous):
                result.append(intersect(previous, current))

            previous = current

        return result

    def hit_x(value):
        def intersect(p, q):
            t = (value - p[0]) / (q[0] - p[0])
            return (value, p[1] + t * (q[1] - p[1]))

        return intersect

    def hit_y(value):
        def intersect(p, q):
            t = (value - p[1]) / (q[1] - p[1])
            return (p[0] + t * (q[0] - p[0]), value)

        return intersect

    polygon = clip_half_plane(
        points, lambda p: p[0] >= lon_min, hit_x(lon_min)
    )
    polygon = clip_half_plane(
        polygon, lambda p: p[0] <= lon_max, hit_x(lon_max)
    )
    polygon = clip_half_plane(
        polygon, lambda p: p[1] >= lat_min, hit_y(lat_min)
    )
    polygon = clip_half_plane(
        polygon, lambda p: p[1] <= lat_max, hit_y(lat_max)
    )

    return polygon


def clip_segment(p, q, bbox):
    """Liang-Barsky for a single segment."""
    lon_min, lat_min, lon_max, lat_max = bbox

    t0, t1 = 0.0, 1.0
    dx = q[0] - p[0]
    dy = q[1] - p[1]

    for edge, delta in (
        (p[0] - lon_min, -dx),
        (lon_max - p[0], dx),
        (p[1] - lat_min, -dy),
        (lat_max - p[1], dy),
    ):
        if delta == 0:
            if edge < 0:
                return None

            continue

        t = edge / delta

        if delta < 0:
            if t > t1:
                return None

            t0 = max(t0, t)
        else:
            if t < t0:
                return None

            t1 = min(t1, t)

    if t0 > t1:
        return None

    return (
        (p[0] + t0 * dx, p[1] + t0 * dy),
        (p[0] + t1 * dx, p[1] + t1 * dy),
    )


def clip_polyline(points, bbox):
    """Clip segment by segment and join contiguous pieces back into
    polylines."""
    pieces = []
    current = []

    for p, q in zip(points, points[1:]):
        segment = clip_segment(p, q, bbox)

        if segment is None:
            if len(current) >= 2:
                pieces.append(current)

            current = []
            continue

        a, b = segment

        if current and current[-1] == a:
            current.append(b)
        else:
            if len(current) >= 2:
                pieces.append(current)

            current = [a, b]

    if len(current) >= 2:
        pieces.append(current)

    return pieces


# ---------------------------------------------------------------------------
# Simplification
# ---------------------------------------------------------------------------

def farthest_point(points, first, last):
    ax, ay = points[first]
    bx, by = points[last]

    dx = bx - ax
    dy = by - ay
    norm = math.hypot(dx, dy)

    best_index = first
    best_distance = -1.0

    for index in range(first + 1, last):
        px, py = points[index]

        if norm == 0:
            distance = math.hypot(px - ax, py - ay)
        else:
            distance = abs(dy * px - dx * py + bx * ay - by * ax) / norm

        if distance > best_distance:
            best_distance = distance
            best_index = index

    return best_index, best_distance


def simplify(points, tolerance):
    """Douglas-Peucker, iterative (recursion blows the stack at 100k
    points)."""
    if len(points) < 3:
        return list(points)

    keep = [False] * len(points)
    keep[0] = True
    keep[-1] = True
    stack = [(0, len(points) - 1)]

    while stack:
        first, last = stack.pop()

        if last <= first + 1:
            continue

        index, distance = farthest_point(points, first, last)

        if distance > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))

    return [point for point, flag in zip(points, keep) if flag]


def round_points(points, precision=OUT_PRECISION):
    """Round, and drop duplicate points created by the rounding."""
    out = []

    for lon, lat in points:
        point = (round(lon, precision), round(lat, precision))

        if not out or out[-1] != point:
            out.append(point)

    return out


# ---------------------------------------------------------------------------
# Building the regions
# ---------------------------------------------------------------------------

def load_features(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    features = []

    for feature in data.get("features", []):
        properties = feature.get("properties", {})
        iso = (
            properties.get("ISO3166-1-Alpha-3")
            or properties.get("ISO_A3")
            or properties.get("name")
        )
        geometry = feature.get("geometry") or {}

        if geometry:
            features.append((iso, geometry))

    return features


def select_features(features, region_key, config, margin=8.0):
    """Every country touching the region (plus a margin).

    The margin makes sure neighbouring countries are counted too -
    otherwise a land border right at the edge of the frame would be
    mistaken for a coastline.
    """
    lon_min, lat_min, lon_max, lat_max = config["bbox"]
    padded = (
        lon_min - margin, lat_min - margin,
        lon_max + margin, lat_max + margin,
    )

    excluded = REGION_EXCLUDE.get(region_key) or set()
    selected = []

    for iso, geometry in features:
        if iso in excluded:
            continue

        box = feature_bbox(geometry)

        if box and bbox_overlaps(box, padded):
            selected.append((iso, geometry))

    return selected


def build_region(features, region_key, config, verbose=True):
    selected = select_features(features, region_key, config)
    counts = collect_edges(selected)

    coast_edges = [edge for edge, n in counts.items() if n == 1]
    border_edges = [edge for edge, n in counts.items() if n >= 2]

    if verbose:
        print(
            "  countries: {:>3}   coast edges: {:>7}   border edges: {:>6}".format(
                len(selected), len(coast_edges), len(border_edges)
            )
        )

    bbox = config["bbox"]
    tolerance = config["tolerance"]
    min_area = config["min_area"]

    # --- Landmass + inland water -----------------------------------------
    rings = []

    for ring in stitch(coast_edges, closed=True):
        clipped = clip_ring(ring, bbox)

        if len(clipped) < 4:
            continue

        simplified = round_points(simplify(clipped, tolerance))

        if len(simplified) < 4:
            continue

        if ring_area(simplified) < min_area:
            continue

        rings.append(simplified)

    shapes, lakes = split_land_and_water(rings)

    # --- Land borders ----------------------------------------------------
    borders = []
    min_border_length = tolerance * 12

    for line in stitch(border_edges, closed=False):
        for clipped in clip_polyline(line, bbox):
            simplified = round_points(simplify(clipped, tolerance))

            if len(simplified) < 2:
                continue

            if polyline_length(simplified) < min_border_length:
                continue

            borders.append(simplified)

    borders.sort(key=polyline_length, reverse=True)

    if verbose:
        print(
            "  -> shapes: {:>3} ({:>5} points)   "
            "water: {:>2} ({:>4} points)   "
            "borders: {:>4} ({:>5} points)".format(
                len(shapes), sum(len(s) for s in shapes),
                len(lakes), sum(len(w) for w in lakes),
                len(borders), sum(len(b) for b in borders),
            )
        )

    return {
        "name": config["name"],
        "ref_lat": config["ref_lat"],
        "padding": config["padding"],
        "bbox": bbox,
        "shapes": shapes,
        "lakes": lakes,
        "borders": borders,
    }


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def format_points(points, indent):
    """Four pairs per line - keeps the file readable and the diff small
    when it is regenerated."""
    pad = " " * indent
    lines = []

    for start in range(0, len(points), 4):
        chunk = points[start:start + 4]
        lines.append(
            pad + " ".join(
                "({}, {}),".format(lon, lat) for lon, lat in chunk
            )
        )

    return "\n".join(lines)


def render_module(regions, source_name):
    out = [
        '"""',
        "dayz_map_data.py - GENERATED, do not edit by hand.",
        "",
        "Built by tools/build_map_data.py from " + source_name + ".",
        "Rebuild with:  python tools/build_map_data.py",
        "",
        "Static region outlines for the offline map in the report.",
        "Europe is still the hand-drawn one in dayz_report.py.",
        "",
        "Per region:",
        "    name     label shown on the map",
        "    ref_lat  reference parallel of the projection",
        "    padding  margin around the landmass",
        "    bbox     (lon_min, lat_min, lon_max, lat_max), also used to",
        "             pick the region from the server location",
        "    shapes   closed coastlines -> filled, no stroke",
        "    lakes    inland water (Great Lakes etc.) -> painted over the",
        "             landmass in the water colour",
        "    borders  open polylines (land borders) -> stroke only",
        '"""',
        "",
        "REGIONS = {",
    ]

    for key in sorted(regions):
        region = regions[key]

        out.append("")
        out.append('    "{}": {{'.format(key))
        out.append('        "name": "{}",'.format(region["name"]))
        out.append('        "ref_lat": {},'.format(region["ref_lat"]))
        out.append('        "padding": {},'.format(region["padding"]))
        out.append('        "bbox": {},'.format(tuple(region["bbox"])))
        out.append('        "shapes": [')

        for shape in region["shapes"]:
            out.append("            [")
            out.append(format_points(shape, 16))
            out.append("            ],")

        out.append("        ],")
        out.append('        "lakes": [')

        for lake in region["lakes"]:
            out.append("            [")
            out.append(format_points(lake, 16))
            out.append("            ],")

        out.append("        ],")
        out.append('        "borders": [')

        for border in region["borders"]:
            out.append("            [")
            out.append(format_points(border, 16))
            out.append("            ],")

        out.append("        ],")
        out.append("    },")

    out.append("")
    out.append("}")
    out.append("")

    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser(
        description="Builds dayz_map_data.py from a Natural Earth GeoJSON."
    )
    parser.add_argument("--geojson", default=DEFAULT_GEOJSON)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--region",
        action="append",
        choices=sorted(REGIONS),
        help="build only this region (may be repeated)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.geojson):
        print("GeoJSON not found: " + args.geojson, file=sys.stderr)
        print(
            "Download Natural Earth 'admin 0 countries' (1:10m) and "
            "pass it with --geojson.",
            file=sys.stderr,
        )
        return 1

    keys = args.region or sorted(REGIONS)

    print("Loading " + args.geojson + " ...")
    features = load_features(args.geojson)
    print("  {} countries loaded".format(len(features)))

    regions = {}

    for key in keys:
        print("Region: " + key)
        regions[key] = build_region(features, key, REGIONS[key])

    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(render_module(regions, os.path.basename(args.geojson)))

    size = os.path.getsize(args.output)
    print("Written: {} ({:.1f} KB)".format(args.output, size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
