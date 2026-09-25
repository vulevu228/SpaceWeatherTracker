import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "space_weather_tracker.log"

# Connects as the local 'postgres' user with no password argument - libpq
# picks the password up from %APPDATA%\postgresql\pgpass.conf, same pattern
# as earthquake_tracker.py, so it never has to live in source code or in the
# Scheduled Task definition.
PG_CONN_PARAMS = {
    "host": "localhost",
    "port": 5432,
    "dbname": "space_weather_tracker",
    "user": "postgres",
}

handlers = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:
    handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=handlers,
)
log = logging.getLogger(__name__)

# NOAA SWPC's real-time GOES X-ray flare event list - free, no API key.
# Rolling 7-day window rather than a shorter one, same reasoning as
# earthquake_tracker's all_month feed: a missed run or a sleeping laptop
# can never cause a gap, since whatever happened while the machine was off
# just shows up whole the next time it runs.
XRAY_FLARES_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-7-day.json"

# NOAA doesn't publish a stable per-event id for flares, so the natural key
# is composed from the two fields that actually identify a distinct flare:
# when it started, and which satellite observed it (two satellites can
# report overlapping flares).
def flare_id(flare):
    return f"{flare['begin_time']}_{flare['satellite']}"

# NOAA's real-time planetary K-index - free, no API key, one row per
# trailing 3-hour period, rolling ~20-day window.
KP_INDEX_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"

# NOAA SWPC's monthly solar-cycle record - free, no API key. The observed
# file goes back to January 1749 (international sunspot number) and October
# 2004 (F10.7 radio flux); the predicted file carries NOAA's forecast for the rest
# of the current cycle. Both are small (~3,300 rows), so every run simply
# re-writes them. They give the 7-day flare and Kp feeds a long-run
# reference point: how active is the sun right now, compared with the last
# 275 years?
OBSERVED_CYCLE_URL = "https://services.swpc.noaa.gov/json/solar-cycle/observed-solar-cycle-indices.json"
PREDICTED_CYCLE_URL = "https://services.swpc.noaa.gov/json/solar-cycle/predicted-solar-cycle.json"

# First month of each numbered solar cycle (the smoothed sunspot minimum it
# starts from), per the SILSO/NOAA convention. Used to label every month with
# its cycle number so the dashboard can compare cycle peaks.
CYCLE_STARTS = [
    (1, "1755-02"), (2, "1766-06"), (3, "1775-06"), (4, "1784-09"), (5, "1798-05"),
    (6, "1810-08"), (7, "1823-05"), (8, "1833-11"), (9, "1843-07"), (10, "1855-12"),
    (11, "1867-03"), (12, "1878-12"), (13, "1890-03"), (14, "1902-01"), (15, "1913-07"),
    (16, "1923-08"), (17, "1933-09"), (18, "1944-02"), (19, "1954-04"), (20, "1964-10"),
    (21, "1976-03"), (22, "1986-09"), (23, "1996-08"), (24, "2008-12"), (25, "2019-12"),
]


def fetch_json(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def parse_utc(time_str):
    """NOAA's flare timestamps end in 'Z'; its Kp timestamps don't, but both
    are UTC - fromisoformat only accepts one of those forms, so this
    normalizes both to an aware UTC datetime."""
    if time_str.endswith("Z"):
        return datetime.fromisoformat(time_str[:-1]).replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc)


def extract_flares(raw_flares):
    events = []
    for f in raw_flares:
        try:
            events.append(
                {
                    "flare_id": flare_id(f),
                    "begin_time": parse_utc(f["begin_time"]),
                    "begin_class": f.get("begin_class"),
                    "max_time": parse_utc(f["max_time"]) if f.get("max_time") else None,
                    "max_class": f.get("max_class"),
                    "max_xrlong": f.get("max_xrlong"),
                    "max_ratio": f.get("max_ratio"),
                    "end_time": parse_utc(f["end_time"]) if f.get("end_time") else None,
                    "end_class": f.get("end_class"),
                    "satellite": f.get("satellite"),
                }
            )
        except (KeyError, ValueError) as e:
            log.warning(f"Skipping malformed flare record: {e} ({f})")
    return events


def extract_kp_readings(raw_kp):
    readings = []
    for row in raw_kp:
        try:
            readings.append(
                {
                    "time_tag": parse_utc(row["time_tag"]),
                    "kp": float(row["Kp"]),
                    "a_running": int(row["a_running"]) if row.get("a_running") is not None else None,
                    "station_count": int(row["station_count"]) if row.get("station_count") is not None else None,
                }
            )
        except (KeyError, ValueError) as e:
            log.warning(f"Skipping malformed Kp-index record: {e} ({row})")
    return readings


def cycle_number(month):
    """'YYYY-MM' -> solar cycle number, or None before cycle 1 began."""
    number = None
    for n, start in CYCLE_STARTS:
        if month >= start:
            number = n
    return number


def extract_solar_cycle(raw_observed, raw_predicted):
    """Merge the observed and predicted monthly files into one row per month.
    NOAA marks a missing value as -1, which becomes NULL here."""

    def value(row, key):
        v = row.get(key)
        return None if v is None or v < 0 else float(v)

    months = {}
    for row in raw_observed:
        month = row["time-tag"]
        months[month] = {
            "ssn": value(row, "ssn"),
            "smoothed_ssn": value(row, "smoothed_ssn"),
            "f107": value(row, "f10.7"),
            "smoothed_f107": value(row, "smoothed_f10.7"),
        }
    for row in raw_predicted:
        month = row["time-tag"]
        months.setdefault(month, {}).update(
            {
                "predicted_ssn": value(row, "predicted_ssn"),
                "predicted_ssn_low": value(row, "low_ssn"),
                "predicted_ssn_high": value(row, "high_ssn"),
                "predicted_f107": value(row, "predicted_f10.7"),
            }
        )
    rows = []
    for month, v in sorted(months.items()):
        rows.append(
            {
                "month": datetime.strptime(month, "%Y-%m").date(),
                "cycle": cycle_number(month),
                "ssn": v.get("ssn"),
                "smoothed_ssn": v.get("smoothed_ssn"),
                "f107": v.get("f107"),
                "smoothed_f107": v.get("smoothed_f107"),
                "predicted_ssn": v.get("predicted_ssn"),
                "predicted_ssn_low": v.get("predicted_ssn_low"),
                "predicted_ssn_high": v.get("predicted_ssn_high"),
                "predicted_f107": v.get("predicted_f107"),
            }
        )
    return rows


# Reporting views read by the Power BI dashboard. They keep the raw tables
# untouched and do the cleaning in one reviewable place:
#   - flare_events: one row per flare. When NOAA switches its primary GOES
#     satellite, both satellites report the same flare for a while (43 such
#     pairs in Aug-Sep 2026), so rows are de-duplicated on begin_time,
#     preferring the stronger reading. Flares with no peak class yet are
#     left out. All times are plain UTC timestamps.
#   - kp_periods: the 3-hour Kp periods in UTC, with NOAA's G-scale level.
VIEWS_SQL = """
CREATE OR REPLACE VIEW flare_events AS
SELECT
    f.flare_id,
    f.begin_time AT TIME ZONE 'UTC'                         AS begin_utc,
    f.max_time   AT TIME ZONE 'UTC'                         AS peak_utc,
    f.end_time   AT TIME ZONE 'UTC'                         AS end_utc,
    (f.max_time AT TIME ZONE 'UTC')::date                   AS peak_date,
    EXTRACT(HOUR FROM f.max_time AT TIME ZONE 'UTC')::int   AS peak_hour_utc,
    f.begin_class,
    f.max_class,
    f.end_class,
    LEFT(f.max_class, 1)                                    AS class_letter,
    CASE LEFT(f.max_class, 1) WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3
                              WHEN 'M' THEN 4 WHEN 'X' THEN 5 END AS class_rank,
    f.max_xrlong                                            AS peak_flux_wm2,
    -- log10 of the peak X-ray flux, shifted so the class boundaries fall on
    -- whole numbers: 1 = B1, 2 = C1, 3 = M1, 4 = X1 (one class = 10x flux)
    ROUND((LOG(f.max_xrlong) + 8)::numeric, 3)::float       AS class_scale,
    f.max_ratio,
    ROUND((EXTRACT(EPOCH FROM (f.end_time - f.begin_time)) / 60)::numeric)::int AS duration_min,
    CASE WHEN f.end_time - f.begin_time < INTERVAL '15 minutes' THEN 'Under 15 min'
         WHEN f.end_time - f.begin_time < INTERVAL '30 minutes' THEN '15 to 30 min'
         WHEN f.end_time - f.begin_time < INTERVAL '60 minutes' THEN '30 to 60 min'
         ELSE '1 hour or more' END                         AS duration_band,
    CASE WHEN f.end_time - f.begin_time < INTERVAL '15 minutes' THEN 1
         WHEN f.end_time - f.begin_time < INTERVAL '30 minutes' THEN 2
         WHEN f.end_time - f.begin_time < INTERVAL '60 minutes' THEN 3
         ELSE 4 END                                         AS duration_order,
    -- NOAA radio blackout scale, from the peak X-ray flux
    CASE WHEN f.max_xrlong >= 2e-3 THEN 'R5 Extreme'
         WHEN f.max_xrlong >= 1e-3 THEN 'R4 Severe'
         WHEN f.max_xrlong >= 1e-4 THEN 'R3 Strong'
         WHEN f.max_xrlong >= 5e-5 THEN 'R2 Moderate'
         WHEN f.max_xrlong >= 1e-5 THEN 'R1 Minor'
         ELSE 'None' END                                    AS radio_blackout,
    'GOES-' || f.satellite                                  AS satellite
FROM (
    SELECT DISTINCT ON (begin_time) *
    FROM solar_flares
    WHERE max_class IS NOT NULL AND max_xrlong > 0
    ORDER BY begin_time, max_xrlong DESC, satellite
) f;

CREATE OR REPLACE VIEW kp_periods AS
SELECT
    time_tag AT TIME ZONE 'UTC'                             AS period_start_utc,
    (time_tag AT TIME ZONE 'UTC')::date                     AS period_date,
    LPAD(EXTRACT(HOUR FROM time_tag AT TIME ZONE 'UTC')::int::text, 2, '0') || ':00' AS slot_utc,
    EXTRACT(HOUR FROM time_tag AT TIME ZONE 'UTC')::int     AS slot_order,
    kp,
    a_running,
    station_count,
    CASE WHEN kp >= 9 THEN 'G5 Extreme' WHEN kp >= 8 THEN 'G4 Severe'
         WHEN kp >= 7 THEN 'G3 Strong'  WHEN kp >= 6 THEN 'G2 Moderate'
         WHEN kp >= 5 THEN 'G1 Minor'   WHEN kp >= 4 THEN 'Active'
         ELSE 'Quiet' END                                   AS storm_level,
    CASE WHEN kp >= 9 THEN 7 WHEN kp >= 8 THEN 6 WHEN kp >= 7 THEN 5
         WHEN kp >= 6 THEN 4 WHEN kp >= 5 THEN 3 WHEN kp >= 4 THEN 2
         ELSE 1 END                                         AS storm_order
FROM geomagnetic_kp;
"""


def init_db(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS solar_flares (
                flare_id     TEXT PRIMARY KEY,
                begin_time   TIMESTAMPTZ,
                begin_class  TEXT,
                max_time     TIMESTAMPTZ,
                max_class    TEXT,
                max_xrlong   DOUBLE PRECISION,
                max_ratio    DOUBLE PRECISION,
                end_time     TIMESTAMPTZ,
                end_class    TEXT,
                satellite    INTEGER,
                ingested_at  TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS geomagnetic_kp (
                time_tag       TIMESTAMPTZ PRIMARY KEY,
                kp             DOUBLE PRECISION,
                a_running      INTEGER,
                station_count  INTEGER,
                ingested_at    TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS solar_cycle (
                month               DATE PRIMARY KEY,
                cycle               INTEGER,
                ssn                 DOUBLE PRECISION,
                smoothed_ssn        DOUBLE PRECISION,
                f107                DOUBLE PRECISION,
                smoothed_f107       DOUBLE PRECISION,
                predicted_ssn       DOUBLE PRECISION,
                predicted_ssn_low   DOUBLE PRECISION,
                predicted_ssn_high  DOUBLE PRECISION,
                predicted_f107      DOUBLE PRECISION,
                ingested_at         TIMESTAMPTZ
            )
            """
        )
        cur.execute(VIEWS_SQL)
    conn.commit()


def insert_flares(conn, flares):
    """Insert-only, keyed on the composed (begin_time, satellite) id - a
    flare's classification/timing is settled shortly after it ends, so
    (like earthquake_tracker's events) re-processing the same flare on a
    later run never creates a duplicate or needs to change anything."""
    ingested_at = datetime.now(timezone.utc)
    inserted = 0
    with conn.cursor() as cur:
        for f in flares:
            cur.execute(
                """
                INSERT INTO solar_flares
                    (flare_id, begin_time, begin_class, max_time, max_class,
                     max_xrlong, max_ratio, end_time, end_class, satellite, ingested_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (flare_id) DO NOTHING
                """,
                (
                    f["flare_id"], f["begin_time"], f["begin_class"], f["max_time"], f["max_class"],
                    f["max_xrlong"], f["max_ratio"], f["end_time"], f["end_class"], f["satellite"],
                    ingested_at,
                ),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def upsert_kp_readings(conn, readings):
    """Unlike the flares table, this upserts (ON CONFLICT DO UPDATE) rather
    than insert-only: NOAA's most recent 1-2 periods are running estimates
    that get refined as more ground stations report in (station_count
    climbs from a handful up to the full network over the following hours).
    Insert-only would freeze a storm-relevant Kp value at its earliest,
    least-complete reading forever - upserting keeps the last few periods
    current while older, already-settled periods are simply re-written with
    the same values each run."""
    ingested_at = datetime.now(timezone.utc)
    written = 0
    with conn.cursor() as cur:
        for r in readings:
            cur.execute(
                """
                INSERT INTO geomagnetic_kp (time_tag, kp, a_running, station_count, ingested_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (time_tag) DO UPDATE SET
                    kp = EXCLUDED.kp,
                    a_running = EXCLUDED.a_running,
                    station_count = EXCLUDED.station_count,
                    ingested_at = EXCLUDED.ingested_at
                """,
                (r["time_tag"], r["kp"], r["a_running"], r["station_count"], ingested_at),
            )
            written += 1
    conn.commit()
    return written


def upsert_solar_cycle(conn, rows):
    """Upsert, like the Kp table: NOAA revises the latest months (the
    smoothed values settle six months later) and re-issues its forecast."""
    ingested_at = datetime.now(timezone.utc)
    cols = ["month", "cycle", "ssn", "smoothed_ssn", "f107", "smoothed_f107",
            "predicted_ssn", "predicted_ssn_low", "predicted_ssn_high", "predicted_f107"]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols[1:])
    with conn.cursor() as cur:
        # A month can drop out of the predicted file once it is observed, so
        # stale forecast values are cleared before the fresh ones are written.
        cur.execute(
            "UPDATE solar_cycle SET predicted_ssn = NULL, predicted_ssn_low = NULL, "
            "predicted_ssn_high = NULL, predicted_f107 = NULL"
        )
        psycopg2.extras.execute_values(
            cur,
            f"""
            INSERT INTO solar_cycle ({", ".join(cols)}, ingested_at) VALUES %s
            ON CONFLICT (month) DO UPDATE SET {updates}, ingested_at = EXCLUDED.ingested_at
            """,
            [tuple(r[c] for c in cols) + (ingested_at,) for r in rows],
        )
    conn.commit()
    return len(rows)


if __name__ == "__main__":
    log.info("Fetching NOAA SWPC space weather feeds...")

    try:
        raw_flares = fetch_json(XRAY_FLARES_URL)
        flares = extract_flares(raw_flares)
        log.info(f"{len(raw_flares)} flare records fetched, {len(flares)} parsed cleanly")
    except Exception as e:
        # Exit non-zero rather than silently continuing with an empty batch -
        # same reasoning as earthquake_tracker: a real outage shouldn't look
        # identical to a genuinely quiet run.
        log.error(f"Error fetching solar flare feed: {e}")
        sys.exit(1)

    try:
        raw_kp = fetch_json(KP_INDEX_URL)
        kp_readings = extract_kp_readings(raw_kp)
        log.info(f"{len(raw_kp)} Kp-index records fetched, {len(kp_readings)} parsed cleanly")
    except Exception as e:
        log.error(f"Error fetching Kp-index feed: {e}")
        sys.exit(1)

    # The solar-cycle files are context, not the live signal, so a failure
    # here is logged and skipped rather than failing the whole run.
    cycle_rows = []
    try:
        cycle_rows = extract_solar_cycle(fetch_json(OBSERVED_CYCLE_URL), fetch_json(PREDICTED_CYCLE_URL))
        log.info(f"{len(cycle_rows)} solar-cycle months fetched")
    except Exception as e:
        log.warning(f"Skipping solar-cycle update this run: {e}")

    try:
        with psycopg2.connect(**PG_CONN_PARAMS) as conn:
            init_db(conn)
            new_flares = insert_flares(conn, flares)
            written_kp = upsert_kp_readings(conn, kp_readings)
            written_cycle = upsert_solar_cycle(conn, cycle_rows) if cycle_rows else 0
    except Exception as e:
        log.error(f"Error writing to PostgreSQL: {e}")
        sys.exit(1)

    log.info(
        f"Done. {new_flares} new solar flares inserted (of {len(flares)} matched this run); "
        f"{written_kp} Kp-index periods written (of {len(kp_readings)} fetched); "
        f"{written_cycle} solar-cycle months written."
    )
