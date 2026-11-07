# OpenAQ Air Quality

A [Dux Shell](https://github.com/duxlabs-dev/dux-shell) plugin that adds
[OpenAQ](https://openaq.org) as a weather provider for air quality only. It
takes PM2.5 and PM10 from the nearest air quality station OpenAQ collects,
so the air quality tile shows what a station measured instead of the
values Open-Meteo models for your location.

The plugin has no widget of its own and no weather. It feeds the air quality
readings of the built-in weather (`european_aqi`, `us_aqi`, `pm2_5`, `pm10`,
`pm2_5_mean_24h`) and sits in the weather provider list next to Open-Meteo
and MET Norway.

## Requirements

- Dux Shell 2.1.1 or newer, the first release whose dms runs weather provider
  plugins (the `weather.plugins` capability). The air quality readings and
  their tile arrive with a later release; until then the plugin runs, but
  nothing shows its values.
- `python3` (standard library only)
- A free OpenAQ API key, see below.
- A station within 25 km of your weather location that reports PM2.5.
  Elsewhere the plugin reports an error and Dux Shell moves on.

## API key

1. Sign up at [explore.openaq.org/register](https://explore.openaq.org/register).
2. Copy the API key from your account settings.
3. Save it where the plugin finds it:

   ```sh
   mkdir -p ~/.config/dms-openaq-air-quality
   install -m 600 /dev/null ~/.config/dms-openaq-air-quality/api-key
   ```

   and paste the key into that file with an editor, so it does not end up
   in your shell history.

The plugin reads `OPENAQ_API_KEY` first, then
`${XDG_CONFIG_HOME:-~/.config}/dms-openaq-air-quality/api-key`. The file is
the safer choice, since Dux Shell does not always start with your login
shell's environment. The key is only ever sent to `api.openaq.org` in the
`X-API-Key` header and is never printed.

## Installation

```sh
cd ~/.config/dms/plugins
git clone https://github.com/joshii-h/dms-openaq-air-quality openaqAirQuality
```

Then:

1. Enable **OpenAQ Air Quality** in Settings → Plugins.
2. In Settings → Date, time & region → Weather → Weather providers, switch on
   **OpenAQ** and place it anywhere **below** your weather provider.
3. Add the air quality tile to the weather dash.

### Where to put it in the list

The first provider that answers is the source of the forecast, the ones
below it only fill in what it lacks. OpenAQ has no weather at all, so it
must never be first: as the source it would leave you with a forecast of
one empty hour.

Anywhere below that it does its job. Open-Meteo's modelled air quality fills
in after all providers, so the station's values win over it wherever OpenAQ
sits, and Open-Meteo still covers whatever the station does not measure.

| Order | Result |
|---|---|
| Open-Meteo, MET Norway, OpenAQ | ✓ weather from Open-Meteo, air quality from the station |
| MeteoSwiss, OpenAQ, Open-Meteo | ✓ the same, with MeteoSwiss weather |
| OpenAQ, Open-Meteo | ✗ OpenAQ is the source, the forecast is empty |

## How it works

About every 15 minutes Dux Shell runs `weather.py` with the coordinates as
JSON on stdin (`{"latitude": 46.95, "longitude": 7.45}`). The script:

1. Rounds the coordinates to two decimals, about 1 km.
2. Asks OpenAQ for stations within 25 km that measure PM2.5. Reference
   monitors (official stations) come first, nearest first; low-cost sensors
   only when there is no monitor in range. Mobile stations are skipped. The
   list is cached for a day per rounded location in
   `~/.cache/dms-openaq-air-quality/stations.json`; a location without any
   station is looked up again after an hour.
3. Takes the latest readings of the nearest station whose PM2.5 is at most
   3 hours old. A station that went quiet is skipped for the next one, up to
   three stations.
4. Fetches the station's hourly PM2.5 and PM10 of the past 24 hours.
5. Prints the result in the Open-Meteo response format, with every weather
   reading null and a single hour and day without readings, which Dux Shell
   needs to accept the answer. The result is cached for 30 minutes in
   `~/.cache/dms-openaq-air-quality/results.json`, and so is the lack of a
   fresh reading. Dux Shell refreshes about every 15 minutes and at every
   start, so OpenAQ is asked at most twice an hour per location; in between
   the plugin answers from the cache. A network failure is not cached, the
   next refresh tries again.

Test it by hand:

```sh
echo '{"latitude": 46.95, "longitude": 7.45}' | ./weather.py | python3 -m json.tool
```

`./weather.py 46.95 7.45` works as well. Run the tests with
`python3 -m unittest discover -s tests`.

## How the values are computed

The indices use the same tables and interpolation as Open-Meteo, so they read
the same as the modelled values on the same tile.

| Reading | From |
|---|---|
| `pm2_5`, `pm10` | the station's latest reading, µg/m³ |
| `pm2_5_mean_24h` | the mean of the hourly PM2.5 of the past 24 hours, null with fewer than 18 of them (the EEA's 75 %), what the WHO guideline is for |
| `european_aqi` | the [European Air Quality Index](https://airindex.eea.europa.eu/AQI/index.html), bands as revised by the EEA in 2024, from the latest hourly concentrations, interpolated to 0–100 (20 per band, above 100 for extremely poor) |
| `us_aqi` | the [US EPA AQI](https://www.airnow.gov/aqi/aqi-basics/) for particulate matter, PM2.5 breakpoints as revised in 2024, from the 24-hour means |

- **European index**: the worse of PM2.5 and PM10. Where the station also
  measures NO₂, O₃ or SO₂ and the reading is fresh, those count too, as the
  EEA's index does. Most stations measure only some of them, so the index is
  often PM-based. Gases reported in ppm or ppb are converted at 20 °C.
- **US index**: PM2.5 and PM10 only; the EPA's ozone index needs 8-hour
  averages this plugin does not build. Without 18 hours for a 24-hour mean,
  the latest reading stands in, which reacts faster than the EPA's own
  NowCast. Values are interpolated between breakpoints rather than taken
  from the EPA's rounded integer table.
- Negative readings, which low-cost sensors report around zero, count as
  missing.

## Limits

- **Coverage.** Stations are dense in Europe and North America and sparse
  elsewhere. In the country a station 25 km away may say little about the
  air at your place.
- **Sensor quality.** Where there is no reference monitor in range, a
  low-cost sensor is used. These read PM2.5 high in humid air.
- **Delay.** Many networks publish hourly or later, so readings are often an
  hour or two old. Older than 3 hours counts as no reading.
- **Rate limits.** The free key allows 60 requests per minute and 2000 per
  hour. A refresh needs up to 3 requests, the station lookup once a day 1 or 2
  more, well within that.
- **Privacy.** The coordinates, rounded to about 1 km, go to OpenAQ once a
  day per location; the station ID goes on each refresh, together with your
  API key, which identifies your account.

## Data and attribution

Air quality data: OpenAQ, from the station operators it collects. OpenAQ
only aggregates data released for redistribution, but the licenses vary per
source, from Creative Commons and open government licenses to bespoke terms
that require naming the source. See the
[OpenAQ terms of use](https://docs.openaq.org/about/terms) and, for a
station's own license and operator,
[explore.openaq.org](https://explore.openaq.org). This plugin is not
affiliated with or endorsed by OpenAQ.

## License

MIT
