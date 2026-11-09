import json
import sys
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import field_from_above as plugin


class PluginContractTests(unittest.TestCase):
    def test_parse_uldk_preserves_default_epsg_2180_and_metadata(self):
        body = (
            "1\n"
            "300310_5.0016.90/8|SRID=2180;POLYGON((416455.7 505756.1,416435.1 505724.5,416455.7 505756.1))|"
            "300310_5.0016.90/8|90/8\n"
        )
        result = plugin._parse_uldk(body)
        self.assertEqual(result["id"], "300310_5.0016.90/8")
        self.assertEqual(result["source_crs"], "EPSG:2180")
        self.assertEqual(result["parcel"], "90/8")
        self.assertTrue(result["wkt"].startswith("POLYGON(("))

    def test_parse_uldk_accepts_getparcelbyid_zero_header_when_row_exists(self):
        body = (
            "0\n"
            "141201_1.0001.6509|SRID=2180;POLYGON((677047 481558,677082 481626,677047 481558))|"
            "141201_1.0001.6509|6509\n"
        )
        result = plugin._parse_uldk(body)
        self.assertEqual(result["id"], "141201_1.0001.6509")

    def test_parse_uldk_names_missing_result(self):
        with self.assertRaises(LookupError):
            plugin._parse_uldk("-1 brak wyników\nbłędny format odpowiedzi XML\n")

    def test_parcel_handler_transforms_uldk_geometry_to_wgs84(self):
        fake_parcel = {
            "requested_id": "300310_5.0016.90/8",
            "wkt": "POLYGON((416455 505756,416435 505724,416455 505756))",
            "source_crs": "EPSG:2180",
            "source": "GUGiK ULDK",
        }
        with patch.object(plugin, "_lookup_parcel", return_value=fake_parcel), patch.object(
            plugin, "_run_analysis", return_value='{"status":"OK"}'
        ) as run:
            result = json.loads(plugin.field_from_above_parcel({"parcel_id": fake_parcel["requested_id"], "season": 2026}))
        self.assertEqual(result["status"], "OK")
        geometry = run.call_args.args[1]
        self.assertTrue(17 < geometry.centroid.x < 18)
        self.assertTrue(52 < geometry.centroid.y < 53)

    def test_valid_parcel_returns_json(self):
        fake_parcel = {"requested_id": "300310_5.0016.90/8", "wkt": "POLYGON((17 52,17.001 52,17.001 52.001,17 52))", "source_crs": "EPSG:4326", "source": "GUGiK ULDK"}
        with patch.object(plugin, "_lookup_parcel", return_value=fake_parcel), patch.object(
            plugin, "_terrain", return_value={"status": "OK"}
        ), patch.object(plugin, "_measure_scene", return_value={"status": "OK", "map_png": "artifacts/map.png"}):
            result = json.loads(plugin.field_from_above_parcel({"parcel_id": fake_parcel["requested_id"], "season": 2026}))
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["satellite"]["status"], "OK")
        self.assertIn("disclaimer", result)

    def test_invalid_parcel_does_not_call_network(self):
        with patch.object(plugin, "_http_get") as http:
            result = json.loads(plugin.field_from_above_parcel({"parcel_id": "not a parcel"}))
        self.assertEqual(result["status"], "ERROR")
        http.assert_not_called()

    def test_network_failure_is_named_json(self):
        with patch.object(
            plugin,
            "_lookup_parcel",
            side_effect=urllib.error.HTTPError("https://uldk.gugik.gov.pl/", 500, "fixture", {}, None),
        ):
            result = json.loads(plugin.field_from_above_parcel({"parcel_id": "300310_5.0016.90/8"}))
        self.assertEqual(result["status"], "ERROR")
        self.assertEqual(result["error"]["code"], "NETWORK")

    def test_point_outside_poland_never_says_flat(self):
        with patch.object(plugin, "_terrain", return_value={"status": "BRAK_DANYCH", "message": "no terrain data, not flat terrain"}), patch.object(
            plugin, "_measure_scene", return_value={"status": "OK", "map_png": "artifacts/map.png"}
        ):
            result = json.loads(plugin.field_from_above_point({"latitude": 40.7, "longitude": -74.0, "radius_m": 100, "season": 2026}))
        self.assertEqual(result["terrain"]["status"], "BRAK_DANYCH")
        self.assertIn("not flat", result["terrain"]["message"])
        self.assertNotIn("flat terrain", result["terrain"]["message"].replace("not flat terrain", ""))


class ReviewFixTests(unittest.TestCase):
    """Fixes requested in NousResearch/hermes-agent PR #151213 review (09.11.2026)."""

    def test_schema_is_full_function_definition(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ffa_pkg", Path(__file__).resolve().parents[1] / "__init__.py",
            submodule_search_locations=[str(Path(__file__).resolve().parents[1])])
        pkg = importlib.util.module_from_spec(spec)
        sys.modules["ffa_pkg"] = pkg
        spec.loader.exec_module(pkg)
        seen = {}

        class Ctx:
            def register_tool(self, name, toolset, schema, handler):
                seen[name] = schema

            def register_skill(self, *a):
                pass

        pkg.register(Ctx())
        self.assertEqual(set(seen), {"field_from_above_parcel", "field_from_above_point"})
        for name, schema in seen.items():
            self.assertEqual(schema["name"], name)
            self.assertGreater(len(schema["description"]), 40)
            self.assertEqual(schema["parameters"]["type"], "object")
        self.assertIn("parcel_id", seen["field_from_above_parcel"]["parameters"]["properties"])
        self.assertIn("latitude", seen["field_from_above_point"]["parameters"]["properties"])

    def test_data_dir_is_under_hermes_home_not_plugin_dir(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"HERMES_HOME": home}):
            d = plugin._data_dir()
            self.assertEqual(d, Path(home) / "plugin-data" / "field-from-above")
        self.assertNotIn(str(Path(plugin.__file__).resolve().parent), str(d))

    def test_no_forwarding_proxy_ships(self):
        import sentinel
        self.assertFalse(hasattr(sentinel, "Przekierowanie"))
        self.assertFalse(hasattr(sentinel, "_Przekierowanie"))

    def test_band_url_must_be_https_on_asset_host(self):
        import sentinel, tempfile
        good = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/sentinel-s2-l2a-cogs/33/U/XU/x/B05.tif"
        self.assertEqual(sentinel.sprawdz_href(good), good)
        for bad in ("http://sentinel-cogs.s3.us-west-2.amazonaws.com/a.tif",
                    "https://evil.example.com/B05.tif",
                    "https://sentinel-cogs.s3.us-west-2.amazonaws.com.evil.com/B05.tif",
                    "file:///etc/passwd",
                    "https://127.0.0.1/B05.tif"):
            with self.assertRaises(ValueError):
                sentinel.sprawdz_href(bad)
        with tempfile.TemporaryDirectory() as d, patch("urllib.request.urlopen") as net:
            with self.assertRaises(ValueError):
                sentinel.pobierz_plik("https://evil.example.com/B05.tif", d)
            net.assert_not_called()

    def test_download_size_cap_and_cache_eviction(self):
        import sentinel, tempfile, io, os, time

        class Odp(io.BytesIO):
            def __init__(self, data, length):
                super().__init__(data)
                self.headers = {"Content-Length": str(length)}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        href = "https://sentinel-cogs.s3.us-west-2.amazonaws.com/p/S2/B05.tif"
        with tempfile.TemporaryDirectory() as d:
            with patch("urllib.request.urlopen", return_value=Odp(b"x" * 10, 10_000)):
                with self.assertRaises(ValueError):
                    sentinel.pobierz_plik(href, d, maks=1000)
            with patch("urllib.request.urlopen", return_value=Odp(b"x" * 2000, 0)):
                with self.assertRaises(ValueError):
                    sentinel.pobierz_plik(href, d, maks=1000)
            self.assertEqual(list(Path(d).iterdir()), [], "no partial file left behind")
            old = Path(d) / "old.tif"
            old.write_bytes(b"o" * 600)
            os.utime(old, (time.time() - 3600, time.time() - 3600))
            with patch("urllib.request.urlopen", return_value=Odp(b"n" * 600, 600)), patch.object(sentinel, "MAKS_CACHE", 1000):
                cel = sentinel.pobierz_plik(href, d, maks=1000)
            self.assertTrue(cel.exists())
            self.assertFalse(old.exists(), "oldest file evicted when cache exceeds cap")


if __name__ == "__main__":
    unittest.main(verbosity=2)
