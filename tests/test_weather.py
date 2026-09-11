import copy
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from jarvis_ui import weather

PLACE = {'name': 'Ghaziabad', 'admin1': 'Uttar Pradesh', 'country': 'India',
         'country_code': 'IN', 'latitude': 28.67, 'longitude': 77.44}
DATA = {'timezone': 'Asia/Kolkata', 'current': {
    'time': '2026-09-08T15:30', 'temperature_2m': 30.2,
    'relative_humidity_2m': 65, 'weather_code': 3, 'wind_speed_10m': 8.2, 'is_day': 1}}


class WeatherTests(unittest.TestCase):
    def lookup(self, data=None, city='Ghaziabad', places=None):
        fetch = Mock(side_effect=[{'results': [PLACE] if places is None else places}, data or DATA])
        return weather.fetch_current_weather(city, get_json=fetch), fetch

    def test_spoken_locations(self):
        for phrase, expected in [
            ('weather in Delhi today', 'Delhi'),
            ("what's the weather like in São Paulo right now?", 'São Paulo'),
            ('weather in New York, US please', 'New York, US'),
            ('weather today in Ghaziabad', 'Ghaziabad'),
            ('Delhi weather', 'Delhi'),
            ('weather at my location', None),
            ("what's the weather today", None),
        ]:
            with self.subTest(phrase=phrase):
                self.assertEqual(weather.extract_location(phrase), expected)

    def test_missing_location_does_not_guess(self):
        with patch.dict(os.environ, {'AURORA_WEATHER_CITY': ''}):
            with self.assertRaises(weather.WeatherError) as error:
                weather.fetch_current_weather(get_json=Mock())
            self.assertEqual(error.exception.code, 'location_required')

    def test_configured_default(self):
        with patch.dict(os.environ, {'AURORA_WEATHER_CITY': 'Ghaziabad'}):
            result, request = self.lookup(city=None)
        self.assertEqual(request.call_args_list[0].args[1]['name'], 'Ghaziabad')
        self.assertIn('India', result['location'])

    def test_structured_data_units_and_timestamp(self):
        result, request = self.lookup()
        self.assertEqual(result['temp_c'], 30.2)
        self.assertEqual(result['source'], 'Open-Meteo')
        self.assertEqual(result['updated_at'], '2026-09-08 15:30')
        self.assertEqual(request.call_args_list[1].args[1]['wind_speed_unit'], 'kmh')
        self.assertEqual(request.call_args_list[1].args[1]['temperature_unit'], 'celsius')

    def test_country_qualifier(self):
        result, _ = self.lookup(city='Ghaziabad, IN')
        self.assertIn('India', result['location'])
        with self.assertRaises(weather.WeatherError):
            self.lookup(city='Ghaziabad, US')

    def test_unknown_city(self):
        with self.assertRaises(weather.WeatherError) as error:
            self.lookup(places=[])
        self.assertEqual(error.exception.code, 'not_found')

    def test_null_nan_and_out_of_range_temperature_rejected(self):
        for value in (None, float('nan'), float('inf'), 500, '30', True):
            with self.subTest(value=value):
                data = copy.deepcopy(DATA)
                data['current']['temperature_2m'] = value
                with self.assertRaises(weather.WeatherError):
                    self.lookup(data)

    def test_optional_measurements_can_be_missing(self):
        data = copy.deepcopy(DATA)
        del data['current']['relative_humidity_2m']
        del data['current']['wind_speed_10m']
        result, _ = self.lookup(data)
        self.assertIsNone(result['humidity'])
        self.assertIsNone(result['wind_kph'])

    def test_night_and_weather_codes(self):
        self.assertEqual(weather.condition_for(0, 0)[0], 'clear_night')
        for code, expected in ((0, 'sunny'), (2, 'partly_cloudy'), (45, 'fog'),
                               (61, 'rain'), (73, 'snow'), (95, 'storm')):
            self.assertEqual(weather.condition_for(code)[0], expected)

    def test_timeout_and_rate_limit(self):
        for exc, code in [(URLError('offline'), 'network'), (TimeoutError(), 'network'),
                          (HTTPError('url', 429, 'busy', {}, None), 'rate_limit')]:
            with patch.object(weather, 'urlopen', side_effect=exc):
                with self.assertRaises(weather.WeatherError) as error:
                    weather._get_json('https://example.com', {})
                self.assertEqual(error.exception.code, code)

    def test_incomplete_payload(self):
        with self.assertRaises(weather.WeatherError):
            self.lookup({'current': {}})


class VoiceWeatherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Real command-handler code, mocked device/graphics imports.
        from test_regressions import load_hologram
        hologram, _ = load_hologram()
        with patch.dict(sys.modules, {'speech_recognition': Mock(), 'pygame': Mock(),
                                     'jarvis_ui.hologram': hologram}):
            from jarvis_ui.voice_assistant import VoiceAssistant
            cls.voice_class = VoiceAssistant

    def setUp(self):
        self.voice = self.voice_class.__new__(self.voice_class)
        self.voice.hologram = Mock()
        self.voice._speak = Mock()
        self.voice._on_log = Mock()
        self.voice.client = None

    def test_weather_works_without_groq_and_resets_old_error(self):
        self.voice.last_weather_error = 'network'
        with patch('jarvis_ui.voice_assistant.fetch_current_weather', return_value={
            'temp_c': 30, 'description': 'overcast', 'location': 'Delhi', 'updated_at': '15:30'
        }) as fetch:
            self.assertTrue(self.voice._handle_local_command('weather in Delhi today'))
        fetch.assert_called_once_with('delhi')
        self.voice.hologram.show_weather.assert_called_once()
        self.assertIsNone(self.voice.last_weather_error)

    def test_failure_surfaces_on_hud(self):
        with patch('jarvis_ui.voice_assistant.fetch_current_weather',
                   side_effect=weather.WeatherError('network', 'Offline')):
            self.voice._handle_local_command('weather in Delhi')
        self.voice.hologram.set_weather_status.assert_called_with('error', 'Offline')
        self.voice.hologram.show_weather.assert_not_called()

    def test_forecast_not_misrepresented_as_current(self):
        with patch('jarvis_ui.voice_assistant.fetch_current_weather') as fetch:
            self.voice._handle_local_command('weather in Delhi tomorrow')
        fetch.assert_not_called()
        self.voice.hologram.set_weather_status.assert_called_once()

    def test_close_weather_does_not_fetch(self):
        with patch('jarvis_ui.voice_assistant.fetch_current_weather') as fetch:
            self.voice._handle_local_command('close the weather')
        fetch.assert_not_called()
        self.voice.hologram.hide_weather.assert_called_once()


if __name__ == '__main__':
    unittest.main()
