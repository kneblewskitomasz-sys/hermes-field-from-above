"""Field from Above: a small, fail-closed Hermes tool layer.

The copied Sentinel-2, terrain and map modules live beside this file. No
external project path is imported at runtime. Network failures become JSON
data returned to Hermes; a tool handler never leaks a traceback to the agent.
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ULDK_URL = "https://uldk.gugik.gov.pl/"
STAC_URL = "https://earth-search.aws.element84.com/v1/search"


def _data_dir() -> Path:
    """Plugin data lives under HERMES_HOME, never inside the install directory."""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - outside Hermes (tests, CLI) fall back to the documented default
        import os
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "plugin-data" / "field-from-above"
DISCLAIMER = "This is a dated satellite measurement and terrain context, not a soil test, fertiliser recommendation, plant-protection recommendation, or agronomic diagnosis."
_WKT_RE = re.compile(r"^(?:MULTI)?POLYGON\s*\(", re.IGNORECASE)


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(code: str, message: str, **extra: Any) -> str:
    return _json({"status": "ERROR", "error": {"code": code, "message": message}, **extra})


def _http_get(url: str, *, data: bytes | None = None, timeout: int = 90) -> bytes:
    headers = {
        "Accept": "application/json,text/plain",
        "User-Agent": "field-from-above/0.1",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_uldk(body: str) -> dict[str, str]:
    """Parse ULDK's line-oriented response without treating an empty result as zero."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines or lines[0].startswith("-1") or "no result" in body.lower() or "brak wyników" in body.lower():
        raise LookupError("ULDK did not find this parcel")
    records = lines[1:] if re.fullmatch(r"\d+", lines[0]) else lines
    for record in records:
        fields = record.split("|")
        if len(fields) < 2:
            continue
        geometry_field = fields[1]
        source_crs = "EPSG:2180"
        if ";" in geometry_field:
            srid, geometry_field = geometry_field.split(";", 1)
            match = re.fullmatch(r"SRID=(\d+)", srid.strip(), re.IGNORECASE)
            if match:
                source_crs = f"EPSG:{match.group(1)}"
        geometry_field = geometry_field.strip()
        if _WKT_RE.match(geometry_field):
            result = {
                "id": fields[0],
                "wkt": geometry_field,
                "source_crs": source_crs,
            }
            if len(fields) > 2:
                result["teryt"] = fields[2]
            if len(fields) > 3:
                result["parcel"] = fields[3]
            return result
    raise ValueError("ULDK response did not contain a polygon geometry")


def _lookup_parcel(parcel_id: str) -> dict[str, str]:
    if not isinstance(parcel_id, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{6,100}", parcel_id):
        raise ValueError("parcel_id must be a full Polish parcel identifier")
    query = urllib.parse.urlencode(
        {
            "request": "GetParcelByIdOrNr",
            "id": parcel_id,
            "result": "id,geom_wkt,teryt,parcel",
        }
    )
    parsed = _parse_uldk(_http_get(f"{ULDK_URL}?{query}").decode("utf-8", errors="replace"))
    parsed["requested_id"] = parcel_id
    parsed["source"] = "GUGiK ULDK"
    return parsed


def _transform_geometry(geometry: Any, source: str, target: str) -> Any:
    from pyproj import Transformer
    from shapely.ops import transform

    transformer = Transformer.from_crs(source, target, always_xy=True)
    return transform(transformer.transform, geometry)


def _utm_epsg(longitude: float, latitude: float) -> str:
    zone = min(60, max(1, int((longitude + 180) // 6) + 1))
    return f"EPSG:{32600 + zone if latitude >= 0 else 32700 + zone}"


def _point_geometry(latitude: float, longitude: float, radius_m: float) -> Any:
    from shapely.geometry import Point

    source = Point(float(longitude), float(latitude))
    crs = _utm_epsg(float(longitude), float(latitude))
    local = _transform_geometry(source, "EPSG:4326", crs).buffer(float(radius_m))
    return _transform_geometry(local, crs, "EPSG:4326")


def _is_probably_poland(latitude: float, longitude: float) -> bool:
    # This is a service-availability guard, not a political border dataset.
    return 49.0 <= latitude <= 55.0 and 14.0 <= longitude <= 24.5


def _terrain(geometry: Any, latitude: float, longitude: float) -> dict[str, Any]:
    if not _is_probably_poland(latitude, longitude):
        return {
            "status": "BRAK_DANYCH",
            "message": "Outside the Poland NMT coverage used by this plugin: no terrain data, not flat terrain.",
            "source": "GUGiK NMT GetHByXY (EPSG:2180)",
        }
    try:
        from . import teren
    except ImportError:
        import teren
    try:
        geometry_2180 = _transform_geometry(geometry, "EPSG:4326", "EPSG:2180")
        result = teren.profil_dzialki(geometry_2180, odstep=25.0)
        return {"status": "OK" if not result.get("brak_danych") else "BRAK_DANYCH", **result}
    except Exception as exc:  # noqa: BLE001 - the tool returns named dependency failure
        return {
            "status": "AWARIA_USLUGI",
            "message": f"Terrain service failed: {type(exc).__name__}: {exc}",
            "source": "GUGiK NMT GetHByXY (EPSG:2180)",
        }


def _stac_scene(geometry: Any, season: int) -> dict[str, Any]:
    bbox = list(geometry.bounds)
    payload = json.dumps(
        {
            "collections": ["sentinel-2-l2a"],
            "bbox": bbox,
            "datetime": f"{season}-03-01T00:00:00Z/{season}-11-30T23:59:59Z",
            "limit": 20,
        }
    ).encode("utf-8")
    data = json.loads(_http_get(STAC_URL, data=payload, timeout=120).decode("utf-8"))
    features = data.get("features", [])
    if not features:
        raise LookupError("Sentinel-2 returned no scene for this area and season")
    return sorted(features, key=lambda item: (
        float(item.get("properties", {}).get("eo:cloud_cover", 100.0)),
        str(item.get("properties", {}).get("datetime", "")),
    ))[0]


def _grid_for(geometry: Any, crs: str) -> Any:
    import math as _math
    from rasterio.transform import from_origin
    try:
        from . import sentinel
    except ImportError:
        import sentinel

    local = _transform_geometry(geometry, "EPSG:4326", crs)
    minx, miny, maxx, maxy = local.bounds
    res = 10.0
    x0 = _math.floor((minx - 30) / res) * res
    y0 = _math.floor((miny - 30) / res) * res
    width = max(1, int(_math.ceil((maxx + 30 - x0) / res)))
    height = max(1, int(_math.ceil((maxy + 30 - y0) / res)))
    return sentinel.Siatka(
        transform=from_origin(x0, y0 + height * res, res, res),
        szer=width,
        wys=height,
        crs=crs,
        res=res,
    ), local


def _render_map(path: Path, ndre: Any, grid: Any, geometry: Any, scene_date: str, crs: str) -> None:
    import matplotlib.pyplot as plt

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        from . import mapy
    except ImportError:
        import mapy
    if crs == "EPSG:32633":
        map_grid = {
            "transform": grid.transform,
            "szer": grid.szer,
            "wys": grid.wys,
        }
        mapy.rysuj_indeks(
            path,
            ndre,
            map_grid,
            _transform_geometry(geometry, "EPSG:4326", crs).bounds,
            geometry,
            f"Field from Above — NDRE {scene_date}",
            "Sentinel-2 L2A · NDRE · 10 m grid · not a soil test",
            etykieta="NDRE",
            vmin=-0.1,
            vmax=0.9,
        )
        return
    # The copied map module has a historical EPSG:32633 boundary helper. For
    # other UTM zones, keep the measurement valid and render a neutral raster.
    fig, ax = plt.subplots(figsize=(7.0, 5.0), dpi=140)
    ax.imshow(ndre, cmap="RdYlGn", vmin=-0.1, vmax=0.9, interpolation="nearest")
    ax.set_title(f"Field from Above — NDRE {scene_date}")
    ax.set_xticks([])
    ax.set_yticks([])
    fig.text(0.01, 0.01, "Sentinel-2 L2A · NDRE · neutral global renderer · not a soil test", fontsize=7)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _measure_scene(geometry: Any, label: str, season: int) -> dict[str, Any]:
    import numpy as np
    try:
        from . import sentinel
    except ImportError:
        import sentinel

    scene = _stac_scene(geometry, season)
    scene_date = sentinel.data_sceny(scene)
    longitude = float(geometry.centroid.x)
    latitude = float(geometry.centroid.y)
    crs = _utm_epsg(longitude, latitude)
    grid, geometry_local = _grid_for(geometry, crs)
    data_dir = _data_dir()
    cache = data_dir / "cache"
    bands: dict[str, Any] = {}
    for name, method in (("B05", "bilinear"), ("B08", "bilinear"), ("SCL", "nearest")):
        href = sentinel.href_pasma(scene, name)
        bands[name] = sentinel.wczytaj_pasmo(sentinel.pobierz_plik(href, cache), grid, metoda=method)
    mask = sentinel.maska_dzialki(geometry_local, grid)
    ndre = sentinel.ndre(sentinel.odbicie(bands["B08"]), sentinel.odbicie(bands["B05"]))
    ndvi = ndre.copy()  # NDVI needs B04; keep a distinct field only when B04 is fetched below.
    href_b04 = sentinel.href_pasma(scene, "B04")
    b04 = sentinel.wczytaj_pasmo(sentinel.pobierz_plik(href_b04, cache), grid, metoda="bilinear")
    ndvi = sentinel.ndvi(sentinel.odbicie(bands["B08"]), sentinel.odbicie(b04))
    stats = sentinel.statystyki(mask, bands["SCL"], ndvi, ndre, label, float(geometry_local.area / 10000.0))
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label)
    map_path = data_dir / "maps" / f"{safe_label}-ndre-{scene_date}.png"
    _render_map(map_path, np.where(mask, ndre, np.nan), grid, geometry, scene_date, crs)
    return {
        "status": "OK",
        "scene_date": scene_date,
        "scene_id": scene.get("id"),
        "cloud_cover_scene_pct": scene.get("properties", {}).get("eo:cloud_cover"),
        "measurement": stats.do_dict(),
        "map_png": str(map_path),
        "source": "Copernicus Sentinel-2 L2A via Earth Search STAC",
    }


def _run_analysis(label: str, geometry: Any, latitude: float, longitude: float, season: int, parcel: dict[str, Any] | None = None) -> str:
    started = time.perf_counter()
    terrain = _terrain(geometry, latitude, longitude)
    try:
        satellite = _measure_scene(geometry, label, season)
    except Exception as exc:  # noqa: BLE001 - named JSON failure is the public contract
        satellite = {
            "status": "AWARIA_USLUGI",
            "message": f"Sentinel-2 measurement failed: {type(exc).__name__}: {exc}",
            "source": "Copernicus Sentinel-2 L2A via Earth Search STAC",
        }
    result: dict[str, Any] = {
        "status": "OK" if satellite.get("status") == "OK" else satellite.get("status", "ERROR"),
        "query": label,
        "season": season,
        "satellite": satellite,
        "terrain": terrain,
        "disclaimer": DISCLAIMER,
        "elapsed_s": round(time.perf_counter() - started, 3),
    }
    if parcel is not None:
        result["parcel"] = {k: v for k, v in parcel.items() if k != "wkt"}
    return _json(result)


def field_from_above_parcel(args: dict, **kwargs: Any) -> str:
    """Hermes handler: resolve a Polish parcel through ULDK and measure it."""
    try:
        parcel_id = args.get("parcel_id")
        parcel = _lookup_parcel(parcel_id)
        from shapely import wkt

        geometry = wkt.loads(parcel["wkt"])
        geometry = _transform_geometry(
            geometry,
            parcel.get("source_crs", "EPSG:2180"),
            "EPSG:4326",
        )
        centroid = geometry.centroid
        season = int(args.get("season", time.gmtime().tm_year))
        return _run_analysis(parcel_id, geometry, centroid.y, centroid.x, season, parcel=parcel)
    except urllib.error.URLError as exc:
        return _error("NETWORK", f"Network request failed: {exc}")
    except (LookupError, ValueError, KeyError) as exc:
        return _error("INPUT_OR_LOOKUP", str(exc))
    except Exception as exc:  # noqa: BLE001 - handler must never throw into Hermes
        return _error("INTERNAL", f"{type(exc).__name__}: {exc}")


def field_from_above_point(args: dict, **kwargs: Any) -> str:
    """Hermes handler: measure a radius around a WGS84 point worldwide."""
    try:
        latitude = float(args["latitude"])
        longitude = float(args["longitude"])
        radius = float(args.get("radius_m", 100.0))
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("latitude/longitude are outside WGS84 bounds")
        if not (10 <= radius <= 5000):
            raise ValueError("radius_m must be between 10 and 5000")
        geometry = _point_geometry(latitude, longitude, radius)
        season = int(args.get("season", time.gmtime().tm_year))
        label = f"point-{latitude:.5f}-{longitude:.5f}-{radius:.0f}m"
        return _run_analysis(label, geometry, latitude, longitude, season)
    except (KeyError, TypeError, ValueError) as exc:
        return _error("INPUT", str(exc))
    except urllib.error.URLError as exc:
        return _error("NETWORK", f"Network request failed: {exc}")
    except Exception as exc:  # noqa: BLE001 - handler must never throw into Hermes
        return _error("INTERNAL", f"{type(exc).__name__}: {exc}")
