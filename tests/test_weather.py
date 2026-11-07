"""Tests for weather.py. Standard library only, no network: get_json is
replaced by a fake OpenAQ that answers from canned data and logs requests.

python3 -m unittest discover -s tests
"""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import weather  # noqa: E402

NOW = datetime(2026, 11, 7, 14, 20, tzinfo=timezone.utc)
BERN = (46.95, 7.45)


def iso(at):
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def sensor(sensor_id, name, units="µg/m³"):
    return {"id": sensor_id, "name": f"{name} {units}", "parameter": {"id": 0, "name": name, "units": units}}


def location(location_id, lat, lon, sensors, monitor=True, mobile=False, last=NOW, tz="Europe/Zurich"):
    return {"id": location_id, "name": f"Station {location_id}", "isMonitor": monitor, "isMobile": mobile,
            "timezone": tz, "coordinates": {"latitude": lat, "longitude": lon}, "sensors": sensors,
            "datetimeLast": {"utc": iso(last), "local": iso(last)} if last else None}


def latest(sensor_id, value, at=NOW - timedelta(minutes=20)):
    return {"datetime": {"utc": iso(at), "local": iso(at)}, "value": value, "sensorsId": sensor_id,
            "locationsId": 0, "coordinates": {"latitude": 0, "longitude": 0}}


def hours(values, end=NOW.replace(minute=0)):
    """Hourly results, the last one ending at end, oldest first; None leaves a gap."""
    results = []
    for i, value in enumerate(reversed(values)):
        hour_end = end - timedelta(hours=i)
        if value is not None:
            results.append({"value": value, "period": {"datetimeTo": {"utc": iso(hour_end)},
                                                       "datetimeFrom": {"utc": iso(hour_end - timedelta(hours=1))}}})
    return list(reversed(results))


class FakeOpenAQ:
    def __init__(self, monitors=(), sensors=(), latest_by_location=None, hours_by_sensor=None):
        self.locations = {"true": list(monitors), "false": list(sensors)}
        self.latest = latest_by_location or {}
        self.hours = hours_by_sensor or {}
        self.calls = []

    def __call__(self, path, params, key):
        self.calls.append((path, dict(params or {}), key))
        if path == "/locations":
            return {"results": self.locations[params["monitor"]]}
        if path.endswith("/latest"):
            return {"results": self.latest.get(int(path.split("/")[2]), [])}
        if path.endswith("/hours"):
            return {"results": self.hours.get(int(path.split("/")[2]), [])}
        raise AssertionError(f"unexpected request {path}")

    def paths(self):
        return [call[0] for call in self.calls]


class TempDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"XDG_CACHE_HOME": os.path.join(self.tmp.name, "cache"),
               "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("OPENAQ_API_KEY", None)

    def use(self, fake):
        patcher = mock.patch.object(weather, "get_json", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


class EuropeanIndex(unittest.TestCase):
    def test_every_band_boundary(self):
        for pollutant, thresholds in weather.EAQI_THRESHOLDS.items():
            for step, concentration in enumerate(thresholds):
                with self.subTest(pollutant=pollutant, concentration=concentration):
                    self.assertAlmostEqual(weather.eaqi(pollutant, concentration), step * 20)

    def test_interpolates_inside_a_band(self):
        # PM2.5 15-50 is "moderate", 40-60.
        self.assertAlmostEqual(weather.eaqi("pm25", 32.5), 50)
        self.assertAlmostEqual(weather.eaqi("pm10", 7.5), 10)
        # Just below a boundary stays in the lower band.
        self.assertLess(weather.eaqi("pm25", 4.99), 20)

    def test_extrapolates_above_the_top(self):
        # Last PM2.5 step is 90-140, 50 µg/m³ per 20 points.
        self.assertAlmostEqual(weather.eaqi("pm25", 190), 120)
        self.assertAlmostEqual(weather.eaqi("o3", 200), 120)

    def test_missing_or_negative_is_none(self):
        for value in (None, -1, float("nan"), float("inf"), "12", True):
            with self.subTest(value=value):
                self.assertIsNone(weather.eaqi("pm25", value))
        self.assertIsNone(weather.eaqi("co", 5))

    def test_worst_pollutant_wins(self):
        # PM2.5 10 → 30, PM10 20 → 26.7, NO2 70 → 65.
        index = weather.european_aqi({"pm25": 10, "pm10": 20, "no2": 70})
        self.assertAlmostEqual(index, 65)
        self.assertAlmostEqual(weather.european_aqi({"pm25": 10, "pm10": None}), 30)
        self.assertIsNone(weather.european_aqi({}))
        self.assertIsNone(weather.european_aqi({"pm25": None}))


class USIndex(unittest.TestCase):
    STEPS = [0, 50, 100, 150, 200, 300, 500]

    def test_every_breakpoint(self):
        for pollutant, thresholds in weather.US_THRESHOLDS.items():
            for concentration, index in zip(thresholds, self.STEPS):
                with self.subTest(pollutant=pollutant, concentration=concentration):
                    self.assertAlmostEqual(weather.us_index(pollutant, concentration), index)

    def test_2024_pm25_breakpoints(self):
        # The revision moved "good" from 12 to 9 µg/m³.
        self.assertAlmostEqual(weather.us_index("pm25", 9), 50)
        self.assertGreater(weather.us_index("pm25", 12), 50)

    def test_interpolates_and_extrapolates(self):
        self.assertAlmostEqual(weather.us_index("pm25", 4.5), 25)
        # 225.5-325.5 spans 300-500, so 275.5 is 400.
        self.assertAlmostEqual(weather.us_index("pm25", 275.5), 400)
        # The scale ends at 500: the top breakpoint and anything above it.
        self.assertAlmostEqual(weather.us_index("pm25", 325.4), 499.8)
        self.assertEqual(weather.us_index("pm25", 325.5), 500)
        self.assertEqual(weather.us_index("pm25", 425.5), 500)
        self.assertEqual(weather.us_index("pm10", 900), 500)
        self.assertAlmostEqual(weather.us_index("pm10", 105), 75)

    def test_worse_of_pm25_and_pm10(self):
        self.assertAlmostEqual(weather.us_aqi(9, 155), 100)
        self.assertAlmostEqual(weather.us_aqi(35.5, 0), 100)
        self.assertAlmostEqual(weather.us_aqi(None, 55), 50)
        self.assertIsNone(weather.us_aqi(None, None))
        self.assertIsNone(weather.us_aqi(-3, None))


class Rounding(unittest.TestCase):
    def test_indices_round_half_up(self):
        self.assertEqual(weather.round_half_up(50.5), 51)
        self.assertEqual(weather.round_half_up(52.5), 53)
        self.assertEqual(weather.round_half_up(50.49), 50)
        self.assertEqual(weather.round_half_up(0), 0)
        self.assertIsNone(weather.round_half_up(None))
        self.assertIsInstance(weather.round_half_up(7.5), int)


class Units(unittest.TestCase):
    def test_micrograms_pass_through(self):
        for units in ("µg/m³", "μg/m³", "ug/m3", " µg/m3 "):
            with self.subTest(units=units):
                self.assertEqual(weather.to_ug_m3("pm25", 12.5, units), 12.5)

    def test_ppm_and_ppb_for_gases(self):
        # 0.1 ppm NO2 at 20 °C: 0.1 * 1000 * 46.0055 / 24.055.
        self.assertAlmostEqual(weather.to_ug_m3("no2", 0.1, "ppm"), 191.25, places=1)
        self.assertAlmostEqual(weather.to_ug_m3("o3", 50, "ppb"), 99.77, places=1)

    def test_unknown_units_are_dropped(self):
        self.assertIsNone(weather.to_ug_m3("pm25", 12, "particles/cm³"))
        self.assertIsNone(weather.to_ug_m3("pm25", 0.01, "ppm"))
        self.assertIsNone(weather.to_ug_m3("no2", 12, None))


class Mean24h(unittest.TestCase):
    def series(self, count, value=10.0):
        return [(NOW.replace(minute=0) - timedelta(hours=i), value) for i in range(count)]

    def test_needs_18_hours(self):
        self.assertEqual(weather.mean_24h(self.series(18), NOW), 10.0)
        self.assertIsNone(weather.mean_24h(self.series(17), NOW))

    def test_ignores_hours_outside_the_window(self):
        old = [(NOW - timedelta(hours=30 + i), 500.0) for i in range(10)]
        future = [(NOW + timedelta(hours=1), 500.0)]
        self.assertEqual(weather.mean_24h(self.series(24) + old + future, NOW), 10.0)
        # 24 hours back exactly is outside, the hour ending now is inside.
        self.assertIsNone(weather.mean_24h([(NOW - timedelta(hours=24), 1.0)] + self.series(17), NOW))

    def test_rounds_and_skips_bad_values(self):
        values = [(NOW.replace(minute=0) - timedelta(hours=i), v)
                  for i, v in enumerate([1, 2] * 9 + [None, -2, float("nan")])]
        self.assertEqual(weather.mean_24h(values, NOW), 1.5)

    def test_duplicate_hours_count_once(self):
        self.assertIsNone(weather.mean_24h(self.series(10) + self.series(10), NOW))


class Stations(unittest.TestCase):
    def test_skips_unusable_locations(self):
        pm = [sensor(1, "pm25")]
        self.assertIsNone(weather.station(location(1, 46.95, 7.45, pm, mobile=True), *BERN, NOW))
        self.assertIsNone(weather.station(location(1, 46.95, 7.45, [sensor(2, "pm10")]), *BERN, NOW))
        self.assertIsNone(weather.station(location(1, 46.95, 7.45, pm, last=NOW - timedelta(days=8)), *BERN, NOW))
        self.assertIsNotNone(weather.station(location(1, 46.95, 7.45, pm, last=None), *BERN, NOW))

    def test_monitor_beats_nearer_low_cost_sensor(self):
        fake = FakeOpenAQ(monitors=[location(1, 47.10, 7.45, [sensor(11, "pm25")]),
                                    location(2, 46.98, 7.45, [sensor(21, "pm25")])],
                          sensors=[location(3, 46.951, 7.45, [sensor(31, "pm25")], monitor=False)])
        with mock.patch.object(weather, "get_json", fake):
            found = weather.find_stations(*BERN, "k", NOW)
        self.assertEqual([s["id"] for s in found], [2, 1])
        # Low-cost sensors are not even asked for when a monitor is in range.
        self.assertEqual([c[1]["monitor"] for c in fake.calls], ["true"])
        self.assertEqual(fake.calls[0][1]["coordinates"], "46.95,7.45")
        self.assertEqual(fake.calls[0][1]["radius"], 25000)

    def test_monitors_sort_first_even_in_a_mixed_answer(self):
        # Should the monitor filter be ignored, monitors still come first.
        fake = FakeOpenAQ(monitors=[location(3, 46.951, 7.45, [sensor(31, "pm25")], monitor=False),
                                    location(1, 47.10, 7.45, [sensor(11, "pm25")])])
        with mock.patch.object(weather, "get_json", fake):
            found = weather.find_stations(*BERN, "k", NOW)
        self.assertEqual([s["id"] for s in found], [1, 3])

    def test_falls_back_to_low_cost_sensors(self):
        fake = FakeOpenAQ(sensors=[location(3, 46.96, 7.45, [sensor(31, "pm25")], monitor=False)])
        with mock.patch.object(weather, "get_json", fake):
            found = weather.find_stations(*BERN, "k", NOW)
        self.assertEqual([s["id"] for s in found], [3])


def bern_station(**latest_overrides):
    """A monitor with PM2.5, PM10 and NO2 in ppm, and readings for all three."""
    station = location(10, 46.95, 7.44, [sensor(101, "pm25"), sensor(102, "pm10"), sensor(103, "no2", "ppm")])
    readings = [latest(101, 12.34), latest(102, 20.0), latest(103, 0.01)]
    return station, readings


class Lookup(TempDirs):
    def full_day(self, value):
        return hours([value] * 24)

    def test_values_from_the_nearest_fresh_station(self):
        station, readings = bern_station()
        fake = self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings},
                                   hours_by_sensor={101: self.full_day(8.0), 102: self.full_day(55.0)}))
        current, zone_name = weather.air_quality(*BERN, "k", NOW)
        self.assertEqual(zone_name, "Europe/Zurich")
        self.assertEqual(current["pm2_5"], 12.3)
        self.assertEqual(current["pm10"], 20.0)
        self.assertEqual(current["pm2_5_mean_24h"], 8.0)
        # EAQI from the latest hour: PM2.5 12.34 → 34.7, NO2 0.01 ppm = 19.1 µg/m³ → 32.2.
        self.assertEqual(current["european_aqi"], 35)
        # US AQI from the 24-hour means: PM2.5 8 → 44.4, PM10 55 → 50.
        self.assertEqual(current["us_aqi"], 50)
        self.assertEqual(fake.paths(), ["/locations", "/locations/10/latest", "/sensors/101/hours", "/sensors/102/hours"])

    def test_without_24_hours_the_latest_value_stands_in(self):
        station, readings = bern_station()
        self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings},
                            hours_by_sensor={101: hours([8.0] * 17), 102: []}))
        current, _ = weather.air_quality(*BERN, "k", NOW)
        self.assertIsNone(current["pm2_5_mean_24h"])
        # PM2.5 12.34 latest → 56.3, above PM10 20 → 18.2.
        self.assertEqual(current["us_aqi"], 56)

    def test_index_of_exactly_half_rounds_up(self):
        # PM2.5 9.25 µg/m³ → European index 28.5, alone at the station.
        station = location(10, 46.95, 7.44, [sensor(101, "pm25")])
        self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: [latest(101, 9.25)]},
                            hours_by_sensor={}))
        current, _ = weather.air_quality(*BERN, "k", NOW)
        self.assertEqual(current["european_aqi"], 29)

    def test_stale_station_is_skipped_for_the_next(self):
        stale = location(1, 46.95, 7.451, [sensor(11, "pm25")])
        fresh = location(2, 47.00, 7.45, [sensor(21, "pm25")], tz="Europe/Berlin")
        self.use(FakeOpenAQ(monitors=[stale, fresh],
                            latest_by_location={1: [latest(11, 99.0, at=NOW - timedelta(hours=3, minutes=1))],
                                                2: [latest(21, 5.0)]},
                            hours_by_sensor={21: []}))
        current, zone_name = weather.air_quality(*BERN, "k", NOW)
        self.assertEqual(current["pm2_5"], 5.0)
        self.assertEqual(zone_name, "Europe/Berlin")
        self.assertIsNone(current["pm10"])

    def test_stale_secondary_pollutant_is_left_out(self):
        station, readings = bern_station()
        readings[1] = latest(102, 500.0, at=NOW - timedelta(hours=5))
        self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings}, hours_by_sensor={}))
        current, _ = weather.air_quality(*BERN, "k", NOW)
        self.assertIsNone(current["pm10"])
        self.assertLess(current["european_aqi"], 40)

    def test_all_stations_stale(self):
        station, _ = bern_station()
        fake = self.use(FakeOpenAQ(monitors=[station],
                                   latest_by_location={10: [latest(101, 10, at=NOW - timedelta(hours=4))]}))
        with self.assertRaises(SystemExit) as raised:
            weather.air_quality(*BERN, "k", NOW)
        self.assertIn("last 3 hours", str(raised.exception.code))
        self.assertNotIn("/sensors/101/hours", fake.paths())

    def test_tries_at_most_three_stations(self):
        monitors = [location(i, 46.95 + i / 100, 7.45, [sensor(i * 10, "pm25")]) for i in range(1, 6)]
        fake = self.use(FakeOpenAQ(monitors=monitors, latest_by_location={5: [latest(50, 10.0)]}))
        with self.assertRaises(SystemExit):
            weather.air_quality(*BERN, "k", NOW)
        self.assertEqual(fake.paths().count("/locations/5/latest"), 0)
        self.assertEqual(sum(p.endswith("/latest") for p in fake.paths()), 3)

    def test_no_station_in_range_is_remembered(self):
        fake = self.use(FakeOpenAQ())
        for _ in range(2):
            with self.assertRaises(SystemExit) as raised:
                weather.air_quality(*BERN, "k", NOW)
            self.assertIn("within 25 km", str(raised.exception.code))
        # Both monitor and low-cost lists asked once, not again on the second run.
        self.assertEqual(fake.paths(), ["/locations", "/locations"])

    def test_station_list_expires_after_a_day(self):
        fake = self.use(FakeOpenAQ())
        for at in (NOW, NOW + timedelta(hours=23), NOW + timedelta(hours=25)):
            with self.assertRaises(SystemExit):
                weather.air_quality(*BERN, "k", at)
        self.assertEqual(fake.paths().count("/locations"), 4)

    def test_result_is_cached_for_ten_minutes(self):
        station, readings = bern_station()
        fake = self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings}, hours_by_sensor={}))
        first, _ = weather.air_quality(*BERN, "k", NOW)
        second, _ = weather.air_quality(*BERN, "k", NOW + timedelta(minutes=9))
        self.assertEqual(first, second)
        self.assertEqual(fake.paths().count("/locations/10/latest"), 1)
        weather.air_quality(*BERN, "k", NOW + timedelta(minutes=11))
        self.assertEqual(fake.paths().count("/locations/10/latest"), 2)
        # The station list is still cached.
        self.assertEqual(fake.paths().count("/locations"), 1)

    def test_cache_is_per_location(self):
        station, readings = bern_station()
        fake = self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings}, hours_by_sensor={}))
        weather.air_quality(*BERN, "k", NOW)
        weather.air_quality(47.05, 7.45, "k", NOW)
        self.assertEqual(fake.paths().count("/locations"), 2)

    def test_corrupt_cache_is_ignored(self):
        cache = os.path.join(os.environ["XDG_CACHE_HOME"], weather.APP)
        os.makedirs(cache)
        for name in ("results.json", "stations.json"):
            with open(os.path.join(cache, name), "w") as f:
                f.write('{"46.95,7.45": {"at": "soon", "stations": 3}')
        station, readings = bern_station()
        self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings}, hours_by_sensor={}))
        current, _ = weather.air_quality(*BERN, "k", NOW)
        self.assertEqual(current["pm2_5"], 12.3)
        with open(os.path.join(cache, "stations.json")) as f:
            self.assertEqual(json.load(f)["46.95,7.45"]["stations"][0]["id"], 10)


class Key(TempDirs):
    def test_missing_key_names_both_places(self):
        with self.assertRaises(SystemExit) as raised:
            weather.api_key()
        message = str(raised.exception.code)
        self.assertIn("OPENAQ_API_KEY", message)
        self.assertIn(os.path.join(os.environ["XDG_CONFIG_HOME"], weather.APP, "api-key"), message)

    def test_key_from_file_and_environment(self):
        path = os.path.join(os.environ["XDG_CONFIG_HOME"], weather.APP)
        os.makedirs(path)
        with open(os.path.join(path, "api-key"), "w") as f:
            f.write("  file-key\n")
        self.assertEqual(weather.api_key(), "file-key")
        with mock.patch.dict(os.environ, {"OPENAQ_API_KEY": "env-key"}):
            self.assertEqual(weather.api_key(), "env-key")
        with mock.patch.dict(os.environ, {"OPENAQ_API_KEY": "   "}):
            self.assertEqual(weather.api_key(), "file-key")

    def test_empty_key_file_counts_as_missing(self):
        path = os.path.join(os.environ["XDG_CONFIG_HOME"], weather.APP)
        os.makedirs(path)
        open(os.path.join(path, "api-key"), "w").close()
        with self.assertRaises(SystemExit):
            weather.api_key()

    def test_main_without_key_asks_nothing(self):
        fake = self.use(FakeOpenAQ())
        with mock.patch.object(sys, "argv", ["weather.py", "46.95", "7.45"]), \
                self.assertRaises(SystemExit):
            weather.main()
        self.assertEqual(fake.calls, [])


class Http(unittest.TestCase):
    def fail_with(self, error):
        return mock.patch("urllib.request.urlopen", side_effect=error)

    def test_rejected_key_is_not_printed(self):
        import urllib.error
        error = urllib.error.HTTPError("https://api.openaq.org/v3/locations", 401, "Unauthorized", {}, None)
        with self.fail_with(error), self.assertRaises(SystemExit) as raised:
            weather.get_json("/locations", {}, "secret-key-123")
        self.assertIn("rejected the API key", raised.exception.code)
        self.assertNotIn("secret-key-123", raised.exception.code)

    def test_rate_limit_and_network_errors(self):
        import urllib.error
        with self.fail_with(urllib.error.HTTPError("u", 429, "Too Many", {}, None)), \
                self.assertRaises(SystemExit) as raised:
            weather.get_json("/locations", {}, "k")
        self.assertIn("rate limit", raised.exception.code)
        with self.fail_with(urllib.error.URLError("no route")), self.assertRaises(SystemExit) as raised:
            weather.get_json("/locations", {}, "k")
        self.assertIn("unreachable", raised.exception.code)

    def test_key_is_not_passed_on_redirects(self):
        captured = {}

        def urlopen(request, timeout):
            captured["request"] = request
            return io.BytesIO(b'{"results": []}')

        with mock.patch("urllib.request.urlopen", urlopen):
            weather.get_json("/locations", {"radius": 25000}, "k")
        request = captured["request"]
        self.assertEqual(request.unredirected_hdrs.get("X-api-key"), "k")
        self.assertNotIn("X-api-key", request.headers)
        self.assertTrue(request.full_url.startswith("https://api.openaq.org/v3/locations?"))


class Output(TempDirs):
    def run_main(self, argv, stdin=""):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stdout", out):
            weather.main()
        return json.loads(out.getvalue())

    def setUp(self):
        super().setUp()
        os.environ["OPENAQ_API_KEY"] = "k"
        station, readings = bern_station()
        self.fake = self.use(FakeOpenAQ(monitors=[station], latest_by_location={10: readings}, hours_by_sensor={}))

    def test_shape_dux_go_accepts(self):
        forecast = self.run_main(["weather.py"], '{"latitude": 46.9481, "longitude": 7.4474}')
        # dux-go's check(): current, hourly and daily, at least one hour and one day.
        self.assertIsInstance(forecast["current"], dict)
        self.assertEqual(len(forecast["hourly"]["time"]), 1)
        self.assertEqual(len(forecast["daily"]["time"]), 1)
        # Nothing for fill() to take into other forecasts' hours and days.
        self.assertEqual(set(forecast["hourly"]), {"time"})
        self.assertEqual(set(forecast["daily"]), {"time"})
        for field in weather.WEATHER_FIELDS:
            self.assertIsNone(forecast["current"][field], field)
        self.assertEqual(forecast["current"]["pm2_5"], 12.3)
        self.assertEqual(forecast["timezone"], "Europe/Zurich")
        self.assertIn(forecast["utc_offset_seconds"], (3600, 7200))
        # Rounded to about 1 km before anything leaves the machine.
        self.assertEqual((forecast["latitude"], forecast["longitude"]), (46.95, 7.45))
        self.assertEqual(self.fake.calls[0][1]["coordinates"], "46.95,7.45")
        hour = forecast["hourly"]["time"][0]
        self.assertTrue(forecast["current"]["time"].startswith(hour[:13]))
        self.assertTrue(hour.endswith(":00"))
        self.assertEqual(hour[:10], forecast["daily"]["time"][0])

    def test_arguments_for_a_test_by_hand(self):
        forecast = self.run_main(["weather.py", "46.95", "7.45"])
        self.assertEqual(forecast["current"]["pm10"], 20.0)

    def test_bad_input(self):
        for argv, stdin in ((["weather.py"], "not json"), (["weather.py"], '{"latitude": 1}'),
                            (["weather.py", "91", "7"], ""), (["weather.py", "x", "7"], ""),
                            (["weather.py", "1"], "")):
            with self.subTest(argv=argv, stdin=stdin), self.assertRaises(SystemExit) as raised:
                self.run_main(argv, stdin)
            self.assertNotEqual(raised.exception.code, 0)

    def test_times_follow_the_station_zone(self):
        forecast = weather.to_open_meteo(46.95, 7.45, {}, "Asia/Tokyo", NOW)
        self.assertEqual(forecast["current"]["time"], "2026-11-07T23:20")
        self.assertEqual(forecast["hourly"]["time"], ["2026-11-07T23:00"])
        self.assertEqual(forecast["utc_offset_seconds"], 9 * 3600)
        forecast = weather.to_open_meteo(46.95, 7.45, {}, "Not/AZone", NOW)
        self.assertNotIn("timezone", forecast)


if __name__ == "__main__":
    unittest.main()
