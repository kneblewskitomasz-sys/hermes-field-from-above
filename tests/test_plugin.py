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


if __name__ == "__main__":
    unittest.main(verbosity=2)
