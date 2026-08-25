import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
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

    try:
        with psycopg2.connect(**PG_CONN_PARAMS) as conn:
            init_db(conn)
            new_flares = insert_flares(conn, flares)
            written_kp = upsert_kp_readings(conn, kp_readings)
    except Exception as e:
        log.error(f"Error writing to PostgreSQL: {e}")
        sys.exit(1)

    log.info(
        f"Done. {new_flares} new solar flares inserted (of {len(flares)} matched this run); "
        f"{written_kp} Kp-index periods written (of {len(kp_readings)} fetched)."
    )
