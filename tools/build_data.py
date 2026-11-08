#!/usr/bin/env python3
"""
Builds the compact data file the app ships with (App/Resources/subway.json)
from the MTA subway static GTFS feed.

Usage:
    python3 tools/build_data.py                 # downloads the regular MTA subway GTFS
    python3 tools/build_data.py --gtfs feed.zip # uses a local GTFS zip
    python3 tools/build_data.py --out other.json

Only the Python standard library is used.

What the output contains
  routes     – id, display name, colours
  stations   – parent stations (one per platform group), with coordinates
  complexes  – groups of stations connected by in-system transfers (e.g. Times Sq)
  transfers  – walking transfers between stations with minimum time
  patterns   – every distinct stop sequence a route runs, with median travel
               times between consecutive stops and the number of trips that start
               in each hour of the day, separately for weekdays / Saturday / Sunday.
               This is what lets the app route "as of" a given day and time.
  shapes     – simplified line geometry per route for drawing the map
"""

import argparse
import csv
import datetime as dt
import io
import json
import statistics
import sys
import urllib.request
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

# The "supplemented" feed is the regular schedule plus most planned service changes
# for the next 7 days, refreshed hourly by MTA.
GTFS_URLS = [
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_supplemented.zip",
    "https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
    "http://web.mta.info/developers/data/nyct/subway/google_transit.zip",
]

# Official MTA line colours, used when routes.txt leaves them empty.
FALLBACK_COLORS = {
    **dict.fromkeys(["1", "2", "3"], "EE352E"),
    **dict.fromkeys(["4", "5", "6", "6X"], "00933C"),
    **dict.fromkeys(["7", "7X"], "B933AD"),
    **dict.fromkeys(["A", "C", "E"], "0039A6"),
    **dict.fromkeys(["B", "D", "F", "FX", "M"], "FF6319"),
    "G": "6CBE45",
    **dict.fromkeys(["J", "Z"], "996633"),
    "L": "A7A9AC",
    **dict.fromkeys(["N", "Q", "R", "W"], "FCCC0A"),
    **dict.fromkeys(["GS", "FS", "H"], "808183"),
    "SI": "1D2E86",
}
DARK_TEXT = {"N", "Q", "R", "W"}
DAY_TYPES = ("wkd", "sat", "sun")
DATE_WINDOW = 8  # today + 7 days get their own, date-specific service counts


def ny_today():
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).date()


def day_type(d):
    wd = d.weekday()
    return "wkd" if wd < 5 else ("sat" if wd == 5 else "sun")


def log(*a):
    print(*a, file=sys.stderr)


def download(urls):
    last = None
    for url in urls:
        try:
            log(f"Downloading {url} …")
            req = urllib.request.Request(url, headers={"User-Agent": "nyc-subway-app-data-builder"})
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except Exception as e:  # try the next mirror
            last = e
            log(f"  failed: {e}")
    raise SystemExit(f"Could not download GTFS: {last}")


def read_csv(z, name, required=True):
    names = {Path(n).name: n for n in z.namelist()}
    if name not in names:
        if required:
            raise SystemExit(f"{name} missing from GTFS")
        return []
    with z.open(names[name]) as f:
        return list(csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")))


def iter_csv(z, name):
    names = {Path(n).name: n for n in z.namelist()}
    with z.open(names[name]) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))


def secs(t):
    if not t:
        return None
    h, m, s = (int(x) for x in t.strip().split(":"))
    return h * 3600 + m * 60 + s


def simplify(points, tol):
    """Douglas–Peucker on (lat, lon) pairs."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        best, idx = 0.0, None
        for i in range(a + 1, b):
            px, py = points[i]
            if norm == 0:
                d = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, idx = d, i
        if idx is not None and best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def build(gtfs_bytes, version=None, today=None):
    z = zipfile.ZipFile(io.BytesIO(gtfs_bytes))

    # ---- stops → parent stations -------------------------------------------
    stops = read_csv(z, "stops.txt")
    parent_of = {}
    station_info = {}
    for s in stops:
        sid = s["stop_id"]
        lt = (s.get("location_type") or "0").strip() or "0"
        parent = (s.get("parent_station") or "").strip()
        if lt == "1" or (not parent and lt == "0"):
            station_info[sid] = {
                "id": sid,
                "name": s["stop_name"].strip(),
                "lat": round(float(s["stop_lat"]), 6),
                "lon": round(float(s["stop_lon"]), 6),
            }
            parent_of[sid] = sid
    for s in stops:
        parent = (s.get("parent_station") or "").strip()
        if parent:
            parent_of[s["stop_id"]] = parent

    # ---- routes ---------------------------------------------------------------
    routes = {}
    for r in read_csv(z, "routes.txt"):
        rid = r["route_id"].strip()
        color = (r.get("route_color") or "").strip().upper() or FALLBACK_COLORS.get(rid, "808183")
        text = (r.get("route_text_color") or "").strip().upper()
        if not text:
            text = "000000" if rid in DARK_TEXT else "FFFFFF"
        routes[rid] = {
            "id": rid,
            "name": (r.get("route_short_name") or rid).strip() or rid,
            "longName": (r.get("route_long_name") or "").strip(),
            "color": color,
            "textColor": text,
        }

    # ---- service calendar → dates and day types --------------------------------
    calendar = read_csv(z, "calendar.txt", required=False)
    exceptions = defaultdict(dict)  # service -> date -> added(True)/removed(False)
    for c in read_csv(z, "calendar_dates.txt", required=False):
        d = dt.datetime.strptime(c["date"], "%Y%m%d").date()
        exceptions[c["service_id"]][d] = c.get("exception_type") == "1"
    weekday_cols = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    cal_rows = {}
    for c in calendar:
        cal_rows[c["service_id"]] = (
            dt.datetime.strptime(c["start_date"], "%Y%m%d").date(),
            dt.datetime.strptime(c["end_date"], "%Y%m%d").date(),
            [c.get(w) == "1" for w in weekday_cols],
        )
    all_services = set(cal_rows) | set(exceptions)

    def runs_on(service, d):
        ex = exceptions.get(service, {})
        if d in ex:
            return ex[d]
        row = cal_rows.get(service)
        return bool(row and row[0] <= d <= row[1] and row[2][d.weekday()])

    today = today or ny_today()
    window = [today + dt.timedelta(days=i) for i in range(DATE_WINDOW)]
    # Typical day types: the first weekday / Saturday / Sunday after the window,
    # i.e. regular service without the short-term planned changes.
    reference = {}
    for i in range(DATE_WINDOW, DATE_WINDOW + 7):
        d = today + dt.timedelta(days=i)
        reference.setdefault(day_type(d), d)

    service_dates = {}   # service -> set of window dates
    service_days = {}    # service -> set of day types
    for sid in all_services:
        service_dates[sid] = {d for d in window if runs_on(sid, d)}
        service_days[sid] = {t for t, d in reference.items() if runs_on(sid, d)}
    # Feeds whose calendars end before the reference dates: fall back to weekday flags.
    if not any(service_days.values()):
        for sid, (_, _, flags) in cal_rows.items():
            days = set()
            if any(flags[:5]): days.add("wkd")
            if flags[5]: days.add("sat")
            if flags[6]: days.add("sun")
            service_days[sid] = days
        for sid, ex in exceptions.items():
            if not service_days.get(sid):
                service_days[sid] = {day_type(d) for d, added in ex.items() if added}

    # ---- trips ----------------------------------------------------------------
    trips = {}
    for t in read_csv(z, "trips.txt"):
        trips[t["trip_id"]] = (
            t["route_id"].strip(),
            t["service_id"],
            (t.get("direction_id") or "0").strip() or "0",
            (t.get("trip_headsign") or "").strip(),
            (t.get("shape_id") or "").strip(),
        )

    # ---- stop_times → patterns --------------------------------------------------
    by_trip = defaultdict(list)
    for st in iter_csv(z, "stop_times.txt"):
        tid = st["trip_id"]
        if tid not in trips:
            continue
        by_trip[tid].append((
            int(st["stop_sequence"]),
            parent_of.get(st["stop_id"], st["stop_id"]),
            secs(st.get("arrival_time")),
            secs(st.get("departure_time")),
        ))

    patterns = {}  # key -> dict
    for tid, rows in by_trip.items():
        route, service, direction, headsign, shape = trips[tid]
        days = service_days.get(service, set())
        dates = service_dates.get(service, set())
        if (not days and not dates) or route not in routes:
            continue
        rows.sort()
        seq = []
        for _, stop, arr, dep in rows:
            arr = arr if arr is not None else dep
            dep = dep if dep is not None else arr
            if arr is None or stop not in station_info:
                continue
            if seq and seq[-1][0] == stop:
                seq[-1] = (stop, seq[-1][1], dep)
            else:
                seq.append((stop, arr, dep))
        if len(seq) < 2:
            continue
        key = (route, direction, tuple(s for s, _, _ in seq))
        p = patterns.get(key)
        if p is None:
            p = patterns[key] = {
                "samples": [[] for _ in range(len(seq) - 1)],
                "service": defaultdict(lambda: [0] * 24),
                "headsigns": Counter(),
                "shapes": Counter(),
            }
        for i in range(len(seq) - 1):
            p["samples"][i].append(max(0, seq[i + 1][1] - seq[i][2]))
        start = seq[0][2]
        for d in days:  # typical day types: wrap times past midnight onto the same day type
            p["service"][d][(start // 3600) % 24] += 1
        for d in dates:  # real dates: 25:10 on Friday's service is 01:10 on Saturday
            actual = d + dt.timedelta(days=start // 86400)
            p["service"][actual.strftime("%Y%m%d")][(start % 86400) // 3600] += 1
        if headsign:
            p["headsigns"][headsign] += 1
        if shape:
            p["shapes"][shape] += 1

    out_patterns = []
    for (route, direction, stops_seq), p in sorted(patterns.items(), key=lambda kv: (kv[0][0], kv[0][1], -len(kv[0][2]))):
        times = [max(30, int(statistics.median(s))) for s in p["samples"]]
        headsign = p["headsigns"].most_common(1)[0][0] if p["headsigns"] else station_info[stops_seq[-1]]["name"]
        out_patterns.append({
            "id": len(out_patterns),
            "route": route,
            "dir": int(direction),
            "headsign": headsign,
            "stops": list(stops_seq),
            "times": times,
            "service": {
                **{d: p["service"].get(d, [0] * 24) for d in DAY_TYPES},
                **{k: v for k, v in sorted(p["service"].items()) if k not in DAY_TYPES and any(v)},
            },
            "_shape": p["shapes"].most_common(1)[0][0] if p["shapes"] else None,
        })

    # ---- stations actually served --------------------------------------------
    served = defaultdict(set)
    for p in out_patterns:
        for s in p["stops"]:
            served[s].add(p["route"])

    # ---- transfers & complexes -----------------------------------------------
    transfers = {}
    for t in read_csv(z, "transfers.txt", required=False):
        a = parent_of.get(t["from_stop_id"], t["from_stop_id"])
        b = parent_of.get(t["to_stop_id"], t["to_stop_id"])
        if a == b or a not in served or b not in served:
            continue
        seconds = int(t.get("min_transfer_time") or 0) or 180
        for x, y in ((a, b), (b, a)):
            transfers[(x, y)] = min(transfers.get((x, y), seconds), seconds)

    parent = {s: s for s in served}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in transfers:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups = defaultdict(list)
    for s in sorted(served):
        groups[find(s)].append(s)

    route_order = {rid: i for i, rid in enumerate(routes)}

    def sort_routes(rs):
        return sorted(rs, key=lambda r: route_order.get(r, 999))

    complexes, complex_of = [], {}
    for members in sorted(groups.values(), key=lambda m: station_info[m[0]]["name"]):
        main = max(members, key=lambda s: (len(served[s]), s))
        cid = "C" + main
        for s in members:
            complex_of[s] = cid
        all_routes = set().union(*(served[s] for s in members))
        complexes.append({
            "id": cid,
            "name": station_info[main]["name"],
            "lat": round(sum(station_info[s]["lat"] for s in members) / len(members), 6),
            "lon": round(sum(station_info[s]["lon"] for s in members) / len(members), 6),
            "stations": members,
            "routes": sort_routes(all_routes),
        })

    stations_out = [
        {**station_info[s], "routes": sort_routes(served[s]), "complex": complex_of[s]}
        for s in sorted(served)
    ]

    # ---- shapes -----------------------------------------------------------------
    shape_ids_needed = {p["_shape"] for p in out_patterns if p["_shape"]}
    shape_pts = defaultdict(list)
    if shape_ids_needed:
        try:
            for row in iter_csv(z, "shapes.txt"):
                if row["shape_id"] in shape_ids_needed:
                    shape_pts[row["shape_id"]].append((
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                    ))
        except KeyError:
            pass
    shapes_out = defaultdict(list)
    seen_geometry = defaultdict(set)
    for p in out_patterns:
        # one direction is enough for drawing; prefer direction 0 unless the route has none
        has_dir0 = any(q["route"] == p["route"] and q["dir"] == 0 for q in out_patterns)
        if has_dir0 and p["dir"] != 0:
            continue
        pts = None
        if p["_shape"] and shape_pts.get(p["_shape"]):
            pts = [(la, lo) for _, la, lo in sorted(shape_pts[p["_shape"]])]
        if not pts:
            pts = [(station_info[s]["lat"], station_info[s]["lon"]) for s in p["stops"]]
        pts = simplify(pts, 0.00008)
        line = [[round(la, 5), round(lo, 5)] for la, lo in pts]
        sig = (tuple(map(tuple, line[:: max(1, len(line) // 8)])))
        if sig in seen_geometry[p["route"]]:
            continue
        seen_geometry[p["route"]].add(sig)
        shapes_out[p["route"]].append(line)

    for p in out_patterns:
        del p["_shape"]

    used_routes = {p["route"] for p in out_patterns}
    return {
        "version": version or dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        # Dates with their own service counts; outside them the app uses wkd/sat/sun.
        "dates": [d.strftime("%Y%m%d") for d in window],
        "routes": [r for r in routes.values() if r["id"] in used_routes],
        "stations": stations_out,
        "complexes": complexes,
        "transfers": [{"from": a, "to": b, "seconds": s} for (a, b), s in sorted(transfers.items())],
        "patterns": out_patterns,
        "shapes": dict(shapes_out),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs", help="path to a GTFS zip (default: download from MTA)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "App" / "Resources" / "subway.json"))
    args = ap.parse_args()

    raw = Path(args.gtfs).read_bytes() if args.gtfs else download(GTFS_URLS)
    data = build(raw)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, separators=(",", ":")))
    log(
        f"Wrote {out} ({out.stat().st_size / 1024:.0f} KB): "
        f"{len(data['routes'])} routes, {len(data['stations'])} stations, "
        f"{len(data['complexes'])} complexes, {len(data['patterns'])} patterns"
    )


if __name__ == "__main__":
    main()
