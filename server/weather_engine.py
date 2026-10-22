import logging
from datetime import date, datetime, timedelta

import requests

from config import (
    WEATHER_DEFAULT_CITY,
    WEATHER_DEFAULT_LAT,
    WEATHER_DEFAULT_LON,
    WEATHER_TIMEZONE,
)

logger = logging.getLogger(__name__)

RIDING_RAIN_MM_THRESHOLD = 2.0
RIDING_RAIN_PROB_THRESHOLD = 50
RIDING_COLD_THRESHOLD_F = 45
RIDING_VERY_COLD_THRESHOLD_F = 35
RIDING_WIND_THRESHOLD_MPH = 25
RIDING_WIND_DANGER_MPH = 40

FEELS_LIKE_MIN_DIFF_F = 3

WMO_CONDITIONS = {
    0:  "Clear skies",
    1:  "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Foggy", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Heavy showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms",
    96: "Thunderstorms with hail", 99: "Severe thunderstorms with hail",
}

WEATHER_UNAVAILABLE = "Sorry, I couldn't reach the weather service right now."
TEMPERATURE_UNAVAILABLE = "Sorry, I couldn't get the current temperature right now."
TIME_UNAVAILABLE = "Sorry, I only have the forecast through tomorrow."


class WeatherEngine:
    def daily_forecast(self, city: str | None = None, when: dict | None = None) -> str:
        data, name = self._load(city)
        if data is None:
            return WEATHER_UNAVAILABLE
        if when:
            return self._format_forecast_at(data, name, when)
        return self._format_daily(data, name)

    def current_temperature(self, city: str | None = None, when: dict | None = None) -> str:
        data, name = self._load(city)
        if data is None:
            return WEATHER_UNAVAILABLE
        if when:
            return self._format_temperature_at(data, name, when)

        reading = _current_reading(data)
        if reading is None:
            return TEMPERATURE_UNAVAILABLE

        temp, feels_like = reading
        message = f"It's {temp}° in {name} right now"
        if abs(feels_like - temp) >= FEELS_LIKE_MIN_DIFF_F:
            message += f", but it feels like {feels_like}°"
        return message + "."

    def riding_assessment(self, city: str | None = None) -> str:
        data, name = self._load(city)
        if data is None:
            return WEATHER_UNAVAILABLE
        return self._format_riding(data, name)

    def morning_brief(self, city: str | None = None) -> str:
        day_name = datetime.now().strftime("%A, %B %-d")
        data, _ = self._load(city)

        if data is None:
            return f"Good morning! Today is {day_name}. I couldn't reach the weather service right now."

        daily = data.get("daily", {})
        high = round(daily["temperature_2m_max"][0])
        low = round(daily["temperature_2m_min"][0])
        reading = _current_reading(data)

        if reading:
            condition = _current_condition(data) or WMO_CONDITIONS.get(daily["weathercode"][0], "Mixed conditions")
            forecast = f"It's currently {reading[0]}, {condition.lower()} with a high of {high} and a low of {low}."
        else:
            condition = WMO_CONDITIONS.get(daily["weathercode"][0], "Mixed conditions")
            forecast = f"It's currently {condition.lower()} with a high of {high} and a low of {low}."

        return f"Good morning! Today is {day_name}. {forecast}"

    def _load(self, city: str | None) -> tuple[dict | None, str]:
        lat, lon, name = self._resolve_location(city)
        return self._fetch_forecast(lat, lon), name

    def _resolve_location(self, city: str | None) -> tuple[float, float, str]:
        default = (WEATHER_DEFAULT_LAT, WEATHER_DEFAULT_LON, WEATHER_DEFAULT_CITY)
        if not city or city.lower() in (WEATHER_DEFAULT_CITY.lower(), "home", "here", "default"):
            return default

        try:
            resp = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city, "count": 1, "language": "en", "format": "json"},
                timeout=5,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if results:
                result = results[0]
                name = result.get("name", city)
                state = result.get("admin1", "")
                return result["latitude"], result["longitude"], f"{name}, {state}" if state else name
        except Exception as e:
            logger.warning(f"Geocoding failed for '{city}': {e}")

        logger.info(f"Could not geocode '{city}', falling back to {WEATHER_DEFAULT_CITY}.")
        return default

    def _fetch_forecast(self, lat: float, lon: float) -> dict | None:
        try:
            resp = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude":  lat,
                    "longitude": lon,
                    "hourly": ",".join([
                        "temperature_2m", "apparent_temperature", "precipitation_probability",
                        "precipitation", "windspeed_10m", "weathercode",
                    ]),
                    "daily": ",".join([
                        "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
                        "precipitation_probability_max", "windspeed_10m_max", "weathercode",
                        "sunrise", "sunset",
                    ]),
                    "current":            "temperature_2m,apparent_temperature,weather_code",
                    "temperature_unit":   "fahrenheit",
                    "windspeed_unit":     "mph",
                    "precipitation_unit": "inch",
                    "timezone":           WEATHER_TIMEZONE,
                    "forecast_days":      2,
                },
                timeout=8,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Weather fetch failed: {e}")
            return None

    def _hour_snapshot(self, data: dict, when: dict) -> dict | None:
        hourly = data.get("hourly", {})
        slot = _locate_hour(hourly, when)
        if slot is None:
            return None
        index, target, now = slot

        temp = _hourly_value(hourly, "temperature_2m", index)
        if temp is None:
            return None
        feels_like = _hourly_value(hourly, "apparent_temperature", index)

        return {
            "label":      _slot_label(target, now),
            "tense":      _tense(target, now),
            "temp":       round(temp),
            "feels_like": round(temp if feels_like is None else feels_like),
            "code":       _hourly_value(hourly, "weathercode", index),
            "rain_pct":   _hourly_value(hourly, "precipitation_probability", index) or 0,
            "wind":       round(_hourly_value(hourly, "windspeed_10m", index) or 0),
        }

    def _format_temperature_at(self, data: dict, city: str, when: dict) -> str:
        snap = self._hour_snapshot(data, when)
        if snap is None:
            return TIME_UNAVAILABLE

        lead = {"past": "It was", "present": "It's", "future": "It'll be"}[snap["tense"]]
        message = f"{lead} {snap['temp']}° in {city} at {snap['label']}"
        if abs(snap["feels_like"] - snap["temp"]) >= FEELS_LIKE_MIN_DIFF_F:
            message += f", with a feels-like of {snap['feels_like']}°"
        return message + "."

    def _format_forecast_at(self, data: dict, city: str, when: dict) -> str:
        snap = self._hour_snapshot(data, when)
        if snap is None:
            return TIME_UNAVAILABLE

        parts = [f"At {snap['label']} in {city}:"]
        parts.append(WMO_CONDITIONS.get(snap["code"], "Mixed conditions") + ".")

        temp = f"{snap['temp']}°"
        if abs(snap["feels_like"] - snap["temp"]) >= FEELS_LIKE_MIN_DIFF_F:
            temp += f", feels like {snap['feels_like']}°"
        parts.append(temp + ".")

        if snap["rain_pct"] >= 70:
            parts.append(f"Expect rain — {snap['rain_pct']}% chance.")
        elif snap["rain_pct"] >= 30:
            parts.append(f"There's a {snap['rain_pct']}% chance of rain.")

        if snap["wind"] >= RIDING_WIND_DANGER_MPH:
            parts.append(f"Very windy — {snap['wind']} mph winds.")
        elif snap["wind"] >= RIDING_WIND_THRESHOLD_MPH:
            parts.append(f"Winds around {snap['wind']} mph.")

        return " ".join(parts)

    def _format_daily(self, data: dict, city: str) -> str:
        daily = data.get("daily", {})

        high = round(daily["temperature_2m_max"][0])
        low = round(daily["temperature_2m_min"][0])
        rain_mm = daily["precipitation_sum"][0]
        rain_pct = daily["precipitation_probability_max"][0]
        wind_max = round(daily["windspeed_10m_max"][0])
        condition = WMO_CONDITIONS.get(daily["weathercode"][0], "Mixed conditions")

        parts = [f"Today in {city}:", f"{condition}."]

        reading = _current_reading(data)
        if reading:
            temp, feels_like = reading
            currently = f"Currently {temp}°"
            if abs(feels_like - temp) >= FEELS_LIKE_MIN_DIFF_F:
                currently += f", feels like {feels_like}°"
            parts.append(currently + ".")

        parts.append(f"High of {high}°, low of {low}°.")

        if rain_pct >= 70:
            parts.append(f"Expect rain — {rain_pct}% chance with {rain_mm:.2f} inches of precipitation.")
        elif rain_pct >= 30:
            parts.append(f"There's a {rain_pct}% chance of rain.")

        if wind_max >= RIDING_WIND_DANGER_MPH:
            parts.append(f"Very windy — gusts up to {wind_max} mph.")
        elif wind_max >= RIDING_WIND_THRESHOLD_MPH:
            parts.append(f"Winds up to {wind_max} mph.")

        parts.extend(self._hourly_alerts(data.get("hourly", {})))
        return " ".join(parts)

    def _format_riding(self, data: dict, city: str) -> str:
        daily = data.get("daily", {})
        hourly = data.get("hourly", {})

        high = round(daily["temperature_2m_max"][0])
        low = round(daily["temperature_2m_min"][0])
        rain_mm = daily["precipitation_sum"][0]
        rain_pct = daily["precipitation_probability_max"][0]
        wind_max = round(daily["windspeed_10m_max"][0])

        issues = []
        cautions = []
        windows = []

        if low < RIDING_VERY_COLD_THRESHOLD_F:
            issues.append(f"It'll be very cold — a low of {low}°. Not ideal riding conditions.")
        elif low < RIDING_COLD_THRESHOLD_F:
            cautions.append(f"It'll be chilly with a low of {low}°. Layer up if you're riding early.")

        if wind_max >= RIDING_WIND_DANGER_MPH:
            issues.append(f"Dangerous wind gusts up to {wind_max} mph — not recommended for riding.")
        elif wind_max >= RIDING_WIND_THRESHOLD_MPH:
            cautions.append(f"Winds up to {wind_max} mph — be cautious on exposed roads.")

        rain_windows = self._rain_windows(hourly)
        if rain_mm > 0.5 and rain_pct >= RIDING_RAIN_PROB_THRESHOLD:
            if rain_windows:
                window_str = " and ".join(f"{w['start']} to {w['end']}" for w in rain_windows)
                issues.append(f"Rain is expected from {window_str}. Plan your ride around those times.")
                dry = self._best_dry_window(hourly)
                if dry:
                    windows.append(f"Best riding window looks like {dry}.")
            else:
                issues.append(f"There's a {rain_pct}% chance of rain with {rain_mm:.2f} inches expected.")
        elif rain_pct >= 30:
            cautions.append(f"There's a {rain_pct}% chance of some light rain — keep an eye on the sky.")

        parts = [f"Riding assessment for {city} today:"]

        if not issues and not cautions:
            parts.append(
                f"Great day to ride! It'll be {high}° and mostly clear. Standard gear and enjoy the road."
            )
        elif not issues:
            parts.append(f"Decent riding day with a few things to watch. High of {high}°.")
            parts.extend(cautions)
            parts.append("Ride carefully and you should be fine.")
        else:
            parts.extend(issues)
            parts.extend(cautions)
            parts.extend(windows or ["You might want to hold off or plan carefully today."])

        return " ".join(parts)

    def _hourly_alerts(self, hourly: dict) -> list[str]:
        alerts = []
        weather_codes = hourly.get("weathercode", [])
        today = date.today().isoformat()

        seen = set()
        for i, timestamp in enumerate(hourly.get("time", [])):
            if not timestamp.startswith(today):
                continue
            code = weather_codes[i]
            if code >= 95 and "thunderstorm" not in seen:
                hour = datetime.fromisoformat(timestamp).strftime("%-I %p")
                alerts.append(f"Thunderstorms possible around {hour}.")
                seen.add("thunderstorm")
            elif code in (71, 73, 75, 77) and "snow" not in seen:
                alerts.append("Snow is in the forecast today.")
                seen.add("snow")

        return alerts

    def _rain_windows(self, hourly: dict) -> list[dict]:
        times = hourly.get("time", [])
        precipitation = hourly.get("precipitation", [])
        probability = hourly.get("precipitation_probability", [])
        today = date.today().isoformat()

        windows = []
        in_window = False
        start = None

        for i, timestamp in enumerate(times):
            if not timestamp.startswith(today):
                if in_window:
                    windows.append({"start": start, "end": _fmt_hour(times[i - 1])})
                    in_window = False
                continue

            # The API returns inches while the threshold is in mm.
            raining = (
                i < len(precipitation) and precipitation[i] > RIDING_RAIN_MM_THRESHOLD / 25.4
                or i < len(probability) and probability[i] >= RIDING_RAIN_PROB_THRESHOLD
            )

            if raining and not in_window:
                start = _fmt_hour(timestamp)
                in_window = True
            elif not raining and in_window:
                windows.append({"start": start, "end": _fmt_hour(times[i - 1])})
                in_window = False

        return windows

    def _best_dry_window(self, hourly: dict) -> str | None:
        times = hourly.get("time", [])
        probability = hourly.get("precipitation_probability", [])
        today = date.today().isoformat()

        best_start = None
        best_end = None
        best_length = 0
        current_start = None
        current_length = 0

        for i, timestamp in enumerate(times):
            if not timestamp.startswith(today):
                continue
            dry = i >= len(probability) or probability[i] < 20
            if dry:
                if current_start is None:
                    current_start = timestamp
                current_length += 1
                if current_length > best_length:
                    best_length = current_length
                    best_start = current_start
                    best_end = timestamp
            else:
                current_start = None
                current_length = 0

        if best_start and best_length >= 2:
            return f"{_fmt_hour(best_start)} to {_fmt_hour(best_end)}"
        return None


def _current_reading(data: dict) -> tuple[int, int] | None:
    # temperature, feels-like right now, or None if the API didn't return them
    current = data.get("current") or {}
    temp = current.get("temperature_2m")
    if temp is None:
        return None
    feels_like = current.get("apparent_temperature")
    return round(temp), round(temp if feels_like is None else feels_like)


def _current_condition(data: dict) -> str | None:
    code = (data.get("current") or {}).get("weather_code")
    if code is None:
        return None
    return WMO_CONDITIONS.get(code, "Mixed conditions")


def _target_slot(when: dict, now: datetime) -> datetime:
    hour = when["hour"] + (1 if when.get("minute", 0) >= 30 else 0)  # nearest hour
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    target = midnight + timedelta(hours=hour)

    day_offset = when.get("day_offset")
    if day_offset is None:
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        day_offset = 1 if target < this_hour else 0
    return target + timedelta(days=day_offset)


def _locate_hour(hourly: dict, when: dict, now: datetime | None = None):
    # (index into the hourly arrays, target slot, now), or None if it isn't in the forecast
    now = now or datetime.now()
    target = _target_slot(when, now)
    try:
        index = hourly.get("time", []).index(target.strftime("%Y-%m-%dT%H:00"))
    except ValueError:
        return None
    return index, target, now


def _hourly_value(hourly: dict, key: str, index: int):
    values = hourly.get(key) or []
    return values[index] if index < len(values) else None


def _tense(target: datetime, now: datetime) -> str:
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    if target < this_hour:
        return "past"
    return "present" if target == this_hour else "future"


def _slot_label(target: datetime, now: datetime) -> str:
    label = target.strftime("%-I %p")
    return label if target.date() == now.date() else f"{label} tomorrow"


def _fmt_hour(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%-I %p")
    except Exception:
        return iso