"""Current weather via Open-Meteo JSON APIs; no LLM-generated measurements."""
import json
import math
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class WeatherError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def extract_location(command):
    """Extract common spoken city phrases without ASCII-only truncation."""
    text = command.strip().rstrip('?.!')
    text = re.sub(r"\s+(?:please|right now|today|currently|now)\b[\s,.!?]*", " ", text, flags=re.I).strip()
    match = re.search(r"\b(?:in|at|for)\s+(.+)$", text, re.I)
    if match:
        city = match.group(1).strip(' ,.?!')
        if city.lower() not in {'my location', 'my city', 'here', 'me'}:
            return city
    # "Delhi weather" or "what is Delhi's weather"
    match = re.search(r"^(?:what(?:'s| is)\s+)?([\wÀ-ž .'-]+?)(?:'s)?\s+weather$", text, re.I)
    if match:
        city = match.group(1).strip()
        if city.lower() not in {'the', 'current', 'local', 'my', 'show', 'show me', 'tell me the'}:
            return city
    return None


def _get_json(url, params):
    request = Request(url + '?' + urlencode(params), headers={'User-Agent': 'Aurora-AI/1.0'})
    try:
        with urlopen(request, timeout=8) as response:
            data = json.loads(response.read(1_000_000))
        if not isinstance(data, dict) or data.get('error'):
            raise WeatherError('invalid_data', 'Weather provider returned an invalid response.')
        return data
    except HTTPError as exc:
        code = 'rate_limit' if exc.code == 429 else 'network'
        raise WeatherError(code, 'Weather provider is busy. Please try again later.') from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise WeatherError('network', 'Could not reach the weather provider. Check your internet connection.') from exc
    except (ValueError, UnicodeError) as exc:
        raise WeatherError('invalid_data', 'Weather provider returned unreadable data.') from exc


def _number(value, low, high, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise WeatherError('invalid_data', 'Weather provider returned missing or invalid measurements.')
    return value


def condition_for(code, is_day=1):
    if code == 0:
        return ('sunny', 'clear sky') if is_day else ('clear_night', 'clear night')
    if code in (1, 2):
        return 'partly_cloudy', 'partly cloudy'
    if code == 3:
        return 'cloudy', 'overcast'
    if code in (45, 48):
        return 'fog', 'foggy'
    if code in (51, 53, 55, 56, 57):
        return 'rain', 'drizzle'
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return 'rain', 'rain showers'
    if code in (71, 73, 75, 77, 85, 86):
        return 'snow', 'snow'
    if code in (95, 96, 99):
        return 'storm', 'thunderstorms'
    raise WeatherError('invalid_data', 'Weather provider returned an unknown condition code.')


def fetch_current_weather(city=None, get_json=None):
    city = (city or os.getenv('AURORA_WEATHER_CITY', '')).strip()
    if not city:
        raise WeatherError('location_required', 'Which city? Say Aurora, weather in Delhi, or name your city.')
    get_json = get_json or _get_json
    parts = [part.strip() for part in city.split(',') if part.strip()]
    if not parts:
        raise WeatherError('location_required', 'Please name a city for the weather lookup.')
    geo = get_json('https://geocoding-api.open-meteo.com/v1/search',
                   {'name': parts[0], 'count': 10, 'language': 'en', 'format': 'json'})
    if not isinstance(geo, dict):
        raise WeatherError('invalid_data', 'Weather provider returned invalid locations.')
    places = geo.get('results') or []
    if not isinstance(places, list):
        raise WeatherError('invalid_data', 'Weather provider returned invalid locations.')
    if len(parts) > 1:
        qualifiers = [p.casefold() for p in parts[1:]]
        places = [p for p in places if isinstance(p, dict) and all(
            q in {str(p.get(k, '')).casefold() for k in ('admin1', 'country', 'country_code', 'admin2')}
            for q in qualifiers)]
    if not places:
        raise WeatherError('not_found', f'I could not find {city}. Try the city and country, separated by a comma.')
    place = places[0]  # provider's ranked result; resolved location always displayed/spoken
    try:
        latitude = _number(place['latitude'], -90, 90)
        longitude = _number(place['longitude'], -180, 180)
        forecast = get_json('https://api.open-meteo.com/v1/forecast', {
            'latitude': latitude, 'longitude': longitude, 'timezone': 'auto',
            'temperature_unit': 'celsius', 'wind_speed_unit': 'kmh',
            'current': 'temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,is_day',
        })
        current = forecast['current']
        temperature = _number(current.get('temperature_2m'), -100, 65)
        humidity = _number(current.get('relative_humidity_2m'), 0, 100, optional=True)
        wind = _number(current.get('wind_speed_10m'), 0, 500, optional=True)
        code = _number(current.get('weather_code'), 0, 99)
        condition, description = condition_for(code, current.get('is_day', 1))
        stamp = current['time']
        if not isinstance(stamp, str) or not re.match(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}', stamp):
            raise WeatherError('invalid_data', 'Weather provider returned an invalid update time.')
        location = ', '.join(dict.fromkeys(str(place[k]) for k in ('name', 'admin1', 'country') if place.get(k)))
        if not location:
            raise WeatherError('invalid_data', 'Weather provider did not resolve a location.')
        return {'location': location, 'temp_c': temperature, 'humidity': humidity,
                'wind_kph': wind, 'condition': condition, 'description': description,
                'updated_at': stamp.replace('T', ' '), 'timezone': forecast.get('timezone', ''),
                'source': 'Open-Meteo'}
    except (KeyError, TypeError, AttributeError) as exc:
        raise WeatherError('invalid_data', 'Weather provider returned incomplete data.') from exc
