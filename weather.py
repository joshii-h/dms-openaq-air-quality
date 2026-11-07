#!/usr/bin/env python3
"""Air quality from the nearest OpenAQ station for Dux Shell, printed as an
Open-Meteo response with only the air quality readings.

Dux Shell sends {"latitude": 46.95, "longitude": 7.45} on stdin. For a test
by hand the coordinates can also be given as arguments:
weather.py <latitude> <longitude>

OpenAQ needs a free API key, read from OPENAQ_API_KEY or from
${XDG_CONFIG_HOME:-~/.config}/dms-openaq-air-quality/api-key. Without a key,
or without a station that measured PM2.5 in the last 3 hours within 25 km,
the script fails and Dux Shell moves on to the next provider.
"""

import json
import math
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

APP = "dms-openaq-air-quality"
USER_AGENT = f"{APP} (+https://github.com/joshii-h/{APP})"
API = "https://api.openaq.org/v3"
PM25_PARAMETER_ID = 2
# OpenAQ's maximum search radius, in metres.
RADIUS = 25000
# A station whose last PM2.5 reading is older than this is skipped.
STALE_AFTER = timedelta(hours=3)
# Stations without any reading for this long are left out of the station list.
RETIRED_AFTER = timedelta(days=7)
STATIONS_TTL = 24 * 3600
# A miss is remembered for less time, so a new or returning station is not
# left out for a whole day.
NO_STATIONS_TTL = 3600
# Dux Shell asks about every 15 minutes and at every start; OpenAQ is asked
# at most once in this time per location, the rest is answered from the cache.
RESULT_TTL = 30 * 60
# How many stations to try, nearest first, before giving up.
MAX_CANDIDATES = 3
# The EEA's 75 % of 24 hours for a daily mean, as in dux-go.
MIN_HOURS_FOR_MEAN = 18
# Dux Shell stops a plugin after 15 s; all requests share this budget.
DEADLINE = time.monotonic() + 12

POLLUTANTS = ("pm25", "pm10", "no2", "o3", "so2")
# g/mol, to turn ppm into µg/m³.
MOLAR_MASS = {"no2": 46.0055, "o3": 47.9982, "so2": 64.066}
# Litres per mole at 20 °C and 1013.25 hPa, the reference the EU directives use.
MOLAR_VOLUME = 24.055

# European Air Quality Index, bands as revised by the EEA in 2024, for hourly
# concentrations in µg/m³. The same tables and interpolation as Open-Meteo, so
# the index reads the same as the one it fills in.
EAQI_THRESHOLDS = {
    "pm25": [0, 5, 15, 50, 90, 140],
    "pm10": [0, 15, 45, 120, 195, 270],
    "no2": [0, 10, 25, 60, 100, 150],
    "o3": [0, 60, 100, 120, 160, 180],
    "so2": [0, 20, 40, 125, 190, 275],
}

# US AQI, the EPA's breakpoints for 24-hour means in µg/m³, PM2.5 as revised
# in 2024. Index steps 0, 50, 100, 150, 200, 300, 500.
US_THRESHOLDS = {
    "pm25": [0, 9, 35.5, 55.5, 125.5, 225.5, 325.5],
    "pm10": [0, 55, 155, 255, 355, 425, 605],
}
US_MAX = 500

WEATHER_FIELDS = ("temperature_2m", "apparent_temperature", "relative_humidity_2m", "is_day",
                  "weather_code", "surface_pressure", "wind_speed_10m", "wind_direction_10m")


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def position(thresholds, value):
    """Where value falls in thresholds, interpolated linearly between two of
    them and extrapolated with the last step's slope above the top."""
    for i in range(1, len(thresholds)):
        if value < thresholds[i]:
            low, high = thresholds[i - 1], thresholds[i]
            return i - 1 + (value - low) / (high - low)
    low, high = thresholds[-2], thresholds[-1]
    return len(thresholds) - 2 + (value - low) / (high - low)


def eaqi(pollutant, concentration):
    """European index, 0 to 100 and above, for one hourly concentration in
    µg/m³. None for a missing or negative concentration."""
    concentration = finite(concentration)
    if concentration is None or concentration < 0 or pollutant not in EAQI_THRESHOLDS:
        return None
    return position(EAQI_THRESHOLDS[pollutant], concentration) * 20


def european_aqi(concentrations):
    """The worst European index of the pollutants given, None without any."""
    indices = [eaqi(p, c) for p, c in concentrations.items()]
    indices = [i for i in indices if i is not None]
    return max(indices) if indices else None


def us_scale(step):
    # Steps 0-4 are 50 index points each, step 5 is 100 and step 6 is 200.
    # The EPA's scale ends at 500, "hazardous" covers everything above.
    if step <= 4:
        return step * 50
    if step <= 5:
        return step * 100 - 200
    return min(step * 200 - 700, US_MAX)


def us_index(pollutant, mean):
    """US index, 0 to 500, for one 24-hour mean in µg/m³, None when missing
    or negative."""
    mean = finite(mean)
    if mean is None or mean < 0 or pollutant not in US_THRESHOLDS:
        return None
    return us_scale(position(US_THRESHOLDS[pollutant], mean))


def us_aqi(pm25, pm10):
    indices = [i for i in (us_index("pm25", pm25), us_index("pm10", pm10)) if i is not None]
    return max(indices) if indices else None


def to_ug_m3(pollutant, value, units):
    """A concentration in µg/m³, or None for units this script does not know."""
    value = finite(value)
    if value is None or not isinstance(units, str):
        return None
    units = units.strip().lower().replace("μ", "µ")
    if units in ("µg/m³", "µg/m3", "ug/m3", "ug/m³"):
        return value
    if pollutant not in MOLAR_MASS:
        return None
    if units == "ppm":
        return value * 1000 * MOLAR_MASS[pollutant] / MOLAR_VOLUME
    if units == "ppb":
        return value * MOLAR_MASS[pollutant] / MOLAR_VOLUME
    return None


def parse_utc(text):
    if not isinstance(text, str):
        return None
    try:
        at = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def is_fresh(at, now):
    # A little leeway for clocks that run ahead.
    return at is not None and now - STALE_AFTER <= at <= now + timedelta(minutes=10)


def mean_24h(hours, now):
    """Mean of the hourly values that ended in the 24 hours before now, rounded
    to 0.1. hours is a list of (end of hour, value). None with fewer than
    MIN_HOURS_FOR_MEAN of those hours. Negative values are sensor noise and
    count as missing."""
    by_hour = {}
    for end, value in hours:
        value = finite(value)
        if end is None or value is None or value < 0 or not now - timedelta(hours=24) < end <= now:
            continue
        by_hour.setdefault(end.replace(minute=0, second=0, microsecond=0), value)
    if len(by_hour) < MIN_HOURS_FOR_MEAN:
        return None
    return round(sum(by_hour.values()) / len(by_hour), 1)


def distance_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 6371 * 2 * math.asin(min(1, math.sqrt(a)))


def config_dir():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP)


def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, APP)


def api_key():
    key = os.environ.get("OPENAQ_API_KEY", "").strip()
    if key:
        return key
    path = os.path.join(config_dir(), "api-key")
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        key = ""
    if not key:
        raise SystemExit(f"no OpenAQ API key: set OPENAQ_API_KEY or write it to {path}")
    return key


class HttpStatus(SystemExit):
    """An HTTP error that concerns only the resource asked for, such as 404
    for a station that was removed, unlike a rejected key or the rate limit.
    Unhandled it ends the script like any other SystemExit."""

    def __init__(self, status):
        super().__init__(f"OpenAQ: HTTP {status}")
        self.status = status


def get_json(path, params, key):
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    # Unredirected: a redirect to another host does not get the key.
    request.add_unredirected_header("X-API-Key", key)
    # The timeout only bounds each socket operation, so a request that would
    # start past the deadline is not started at all.
    remaining = DEADLINE - time.monotonic()
    if remaining <= 0:
        raise SystemExit("OpenAQ took too long, giving up before Dux Shell stops the plugin")
    try:
        with urllib.request.urlopen(request, timeout=max(1, remaining)) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise SystemExit(f"OpenAQ rejected the API key (HTTP {error.code})") from None
        if error.code == 429:
            raise SystemExit("OpenAQ rate limit reached, try again later") from None
        raise HttpStatus(error.code) from None
    except urllib.error.URLError as error:
        raise SystemExit(f"OpenAQ unreachable: {error.reason}") from None
    except (OSError, ValueError) as error:
        raise SystemExit(f"OpenAQ: {error}") from None


def read_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def write_cache(path, cache, ttl, now_s):
    # Drop what has expired, so the file does not grow with every location.
    cache = {k: v for k, v in cache.items()
             if isinstance(v, dict) and isinstance(v.get("at"), (int, float)) and now_s - v["at"] < ttl}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    except OSError:
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, allow_nan=False)
        os.replace(tmp, path)
    except (OSError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass


def cached(cache, key, ttl, now_s):
    entry = cache.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)) and 0 <= now_s - entry["at"] < ttl:
        return entry
    return None


def station(location, lat, lon, now):
    """The parts of an OpenAQ location this script uses, None for a location
    that cannot serve: mobile, without a PM2.5 sensor or long silent."""
    if not isinstance(location, dict) or location.get("isMobile") or not is_int(location.get("id")):
        return None
    coordinates = location.get("coordinates") or {}
    slat, slon = finite(coordinates.get("latitude")), finite(coordinates.get("longitude"))
    if slat is None or slon is None:
        return None
    last = parse_utc((location.get("datetimeLast") or {}).get("utc"))
    if last is not None and last < now - RETIRED_AFTER:
        return None
    sensors = {}
    for sensor in location.get("sensors") or []:
        parameter = (sensor or {}).get("parameter") or {}
        name = parameter.get("name")
        if name in POLLUTANTS and is_int(sensor.get("id")) and name not in sensors:
            sensors[name] = {"id": sensor["id"], "units": parameter.get("units")}
    if "pm25" not in sensors:
        return None
    return {
        "id": location["id"],
        "name": location.get("name") if isinstance(location.get("name"), str) else "",
        "monitor": bool(location.get("isMonitor")),
        "distance": round(distance_km(lat, lon, slat, slon), 2),
        "timezone": location.get("timezone") if isinstance(location.get("timezone"), str) else "",
        "sensors": sensors,
    }


def find_stations(lat, lon, key, now):
    """Stations with PM2.5 within RADIUS: reference monitors nearest first, then
    low-cost sensors nearest first."""
    found = []
    # The results are not ordered by distance, so the list must be complete
    # for the nearest station to be in it. OpenAQ's default is 100 and its
    # API description names no maximum; 1000 was tried against the live API
    # (November 2026) and is accepted, echoed back in meta.limit. Within
    # 25 km and filtered to PM2.5 even dense low-cost networks stay below it.
    base = {"coordinates": f"{lat:.2f},{lon:.2f}", "radius": RADIUS,
            "parameters_id": PM25_PARAMETER_ID, "limit": 1000}
    # Monitors first; the much larger list of low-cost sensors only when there is none.
    for monitor in ("true", "false"):
        data = get_json("/locations", {**base, "monitor": monitor}, key)
        results = data.get("results") if isinstance(data, dict) else None
        found = [s for s in (station(l, lat, lon, now) for l in results or []) if s]
        if found:
            break
    found.sort(key=lambda s: (not s["monitor"], s["distance"]))
    return found


def latest_readings(candidate, key, now):
    """Fresh latest concentrations in µg/m³ of the station, by pollutant."""
    data = get_json(f"/locations/{candidate['id']}/latest", {"limit": 100}, key)
    by_sensor = {}
    for item in (data.get("results") if isinstance(data, dict) else None) or []:
        if isinstance(item, dict) and is_int(item.get("sensorsId")):
            by_sensor[item["sensorsId"]] = item
    readings = {}
    for pollutant, sensor in candidate["sensors"].items():
        item = by_sensor.get(sensor["id"])
        if not item or not is_fresh(parse_utc((item.get("datetime") or {}).get("utc")), now):
            continue
        value = to_ug_m3(pollutant, item.get("value"), sensor["units"])
        if value is not None and value >= 0:
            readings[pollutant] = value
    return readings


def hourly_mean(sensor_id, key, now):
    params = {
        "datetime_from": (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datetime_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 100,
    }
    try:
        data = get_json(f"/sensors/{sensor_id}/hours", params, key)
    except HttpStatus:
        # Without the hours the latest reading stands in for the mean.
        return None
    hours = []
    for item in (data.get("results") if isinstance(data, dict) else None) or []:
        if isinstance(item, dict):
            end = ((item.get("period") or {}).get("datetimeTo") or {}).get("utc")
            hours.append((parse_utc(end), item.get("value")))
    return mean_24h(hours, now)


def round_or_none(value, digits=None):
    return None if value is None else round(value, digits)


def round_half_up(value):
    """An index rounded to a whole number, .5 up as the EPA and Open-Meteo do;
    round() would take 50.5 down to 50. None stays None."""
    return None if value is None else math.floor(value + 0.5)


def air_quality(lat, lon, key, now):
    """Air quality readings for the rounded location, cached for RESULT_TTL,
    as is the lack of a fresh reading."""
    now_s = now.timestamp()
    location = f"{lat:.2f},{lon:.2f}"
    results_path = os.path.join(cache_dir(), "results.json")
    results = read_cache(results_path)
    entry = cached(results, location, RESULT_TTL, now_s)
    if entry and isinstance(entry.get("current"), dict):
        return entry["current"], entry.get("timezone") or ""
    if entry and isinstance(entry.get("error"), str):
        raise SystemExit(entry["error"])

    stations_path = os.path.join(cache_dir(), "stations.json")
    stations_cache = read_cache(stations_path)
    entry = cached(stations_cache, location, STATIONS_TTL, now_s)
    if entry and isinstance(entry.get("stations"), list) and \
            (entry["stations"] or now_s - entry["at"] < NO_STATIONS_TTL):
        candidates = entry["stations"]
    else:
        candidates = find_stations(lat, lon, key, now)
        # Misses are remembered too, for NO_STATIONS_TTL, so a location
        # without stations is not looked up again on every refresh.
        stations_cache[location] = {"at": now_s, "stations": candidates}
        write_cache(stations_path, stations_cache, STATIONS_TTL, now_s)
    if not candidates:
        raise SystemExit(f"no OpenAQ station measures PM2.5 within {RADIUS // 1000} km")

    failed = []
    for candidate in candidates[:MAX_CANDIDATES]:
        try:
            readings = latest_readings(candidate, key, now)
        except HttpStatus as error:
            # The station list can be a day old; a station removed since, or
            # one OpenAQ fails to answer for, is skipped for the next.
            failed.append(f"station {candidate['id']}: HTTP {error.status}")
            continue
        if "pm25" in readings:
            break
    else:
        message = (f"no OpenAQ station within {RADIUS // 1000} km has measured PM2.5 "
                   f"in the last {int(STALE_AFTER.total_seconds() // 3600)} hours")
        message = f"{message} ({', '.join(failed)})" if failed else message
        # Remembered like a result, so stations without readings are not
        # asked again before RESULT_TTL is up.
        results[location] = {"at": now_s, "error": message}
        write_cache(results_path, results, RESULT_TTL, now_s)
        raise SystemExit(message)

    sensors = candidate["sensors"]
    pm25_mean = hourly_mean(sensors["pm25"]["id"], key, now)
    pm10_mean = hourly_mean(sensors["pm10"]["id"], key, now) if "pm10" in readings else None
    pm25, pm10 = readings.get("pm25"), readings.get("pm10")
    # The EPA's index is for 24-hour means; without one the latest value stands in.
    us = us_aqi(pm25_mean if pm25_mean is not None else pm25,
                pm10_mean if pm10_mean is not None else pm10)
    current = {
        "european_aqi": round_half_up(european_aqi(readings)),
        "us_aqi": round_half_up(us),
        "pm2_5": round_or_none(pm25, 1),
        "pm10": round_or_none(pm10, 1),
        "pm2_5_mean_24h": pm25_mean,
    }
    results[location] = {"at": now_s, "current": current, "timezone": candidate.get("timezone") or ""}
    write_cache(results_path, results, RESULT_TTL, now_s)
    return current, candidate.get("timezone") or ""


def zone(name):
    if name:
        try:
            return name, ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return "", None


def to_open_meteo(lat, lon, current, zone_name, now):
    """An Open-Meteo forecast with only the air quality. The single hour and day
    are there because Dux Shell rejects a forecast without any; they carry no
    readings, so they fill nothing in."""
    name, tz = zone(zone_name)
    local = now.astimezone(tz) if tz else now.astimezone()
    forecast = {
        "latitude": lat,
        "longitude": lon,
        "utc_offset_seconds": int(local.utcoffset().total_seconds()),
        "current": {
            "time": local.strftime("%Y-%m-%dT%H:%M"),
            **{field: None for field in WEATHER_FIELDS},
            "european_aqi": current.get("european_aqi"),
            "us_aqi": current.get("us_aqi"),
            "pm2_5": current.get("pm2_5"),
            "pm10": current.get("pm10"),
            "pm2_5_mean_24h": current.get("pm2_5_mean_24h"),
        },
        "hourly": {"time": [local.strftime("%Y-%m-%dT%H:00")]},
        "daily": {"time": [local.strftime("%Y-%m-%d")]},
    }
    if name:
        forecast["timezone"] = name
    return forecast


def read_location(argv, stdin):
    if len(argv) == 3:
        try:
            lat, lon = float(argv[1]), float(argv[2])
        except ValueError:
            raise SystemExit(__doc__) from None
    elif len(argv) == 1:
        try:
            query = json.load(stdin)
            lat, lon = float(query["latitude"]), float(query["longitude"])
        except (ValueError, KeyError, TypeError):
            raise SystemExit('expected {"latitude": ..., "longitude": ...} on stdin') from None
    else:
        raise SystemExit(__doc__)
    if not (math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180):
        raise SystemExit("latitude or longitude out of range")
    return lat, lon


def main():
    lat, lon = read_location(sys.argv, sys.stdin)
    key = api_key()
    # Rounded to about 1 km, which is all OpenAQ gets to see.
    lat, lon = round(lat, 2), round(lon, 2)
    now = datetime.now(timezone.utc)
    current, zone_name = air_quality(lat, lon, key, now)
    json.dump(to_open_meteo(lat, lon, current, zone_name, now), sys.stdout, allow_nan=False)


if __name__ == "__main__":
    main()
