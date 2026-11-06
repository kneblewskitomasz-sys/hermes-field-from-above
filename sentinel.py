"""Sentinel-2 L2A: wyszukanie sceny, odczyt pasm, NDVI/NDRE na działkę.

Ten moduł jest prawdziwym adapterem do szkieletu `lancuch.py`. Nie udaje:
* sceny szuka w STAC Earth Search (`sentinel-2-l2a`),
* pasma B04/B05/B08/SCL pobiera z publicznych COG-ów Sentinel COGs,
* liczy NDVI = (B08-B04)/(B08+B04) i NDRE = (B08-B05)/(B08+B05),
* chmury odsiewa po SCL (klasy 4/5/6/7 są czyste, 3 i 8–11 nie),
* każdą liczbę opatruje liczbą pikseli, z których powstała.

Dwa ograniczenia, które muszą być widoczne w wyniku, nie ukryte:
1. B05 (red edge 1) i SCL są w L2A co 20 m, pasma 10 m — B05 jest resample'owane
   do siatki 10 m (bilinear), więc NDRE ma realnie rozdzielczość 20 m.
2. Piksel 10 m na działce 0,3 ha to ~30 pikseli; granica działki przecina piksele,
   więc brzeg miesza sąsiednie pola. Dlatego wynik podaje przedział ufności,
   a nie samą średnią.

Pobieranie: GDAL na tym laptopie nie umie otworzyć COG po https (curl/schannel
`SEC_E_NO_CREDENTIALS`), więc pliki ściągamy `urllib`-em do cache i czytamy lokalnie.
To jest ograniczenie środowiska, nie źródła — zapisane w raporcie.
"""

from __future__ import annotations

import http.server
import json
import math
import os
import re
import socketserver
import threading
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

STAC = "https://earth-search.aws.element84.com/v1"
KOLEKCJA = "sentinel-2-l2a"

# Bez tych dwóch ustawień GDAL przy zdalnym COG próbuje listować katalog S3
# i pobiera cały plik zamiast okna (zmierzone 02.11.2026: 5,8 MB SCL zamiast
# ~20 kB okna). Ustawiamy je globalnie, bo dotyczą każdego odczytu COG.
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif")

# SCL: 4 roślinność, 5 gleba, 6 woda, 7 niesklasyfikowane; 3 cień, 8/9/10 chmury, 11 śnieg.
SCL_CZYSTE = frozenset({4, 5, 6, 7})
SCL_CHMURY = frozenset({8, 9, 10})
SCL_CIEN = 3
SCL_SNIEG = 11

PASMA_10M = {"B04": "red", "B08": "nir"}
PASMA_20M = {"B05": "rededge1", "SCL": "scl"}


def _get(url: str, timeout: int = 90) -> bytes:
    zadanie = urllib.request.Request(url, headers={"User-Agent": "field-from-above/0.1"})
    return urllib.request.urlopen(zadanie, timeout=timeout).read()


def szukaj_scen(bbox: list[float], start: str, end: str, ile: int = 50) -> list[dict]:
    """Sceny Sentinel-2 L2A nad bbox (WGS84) w przedziale dat. Zwraca itemy STAC."""
    cialo = json.dumps({
        "collections": [KOLEKCJA],
        "bbox": bbox,
        "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z",
        "limit": ile,
    }).encode("utf-8")
    req = urllib.request.Request(
        STAC + "/search", data=cialo,
        headers={"Content-Type": "application/json", "User-Agent": "field-from-above/0.1"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["features"]


def href_pasma(item: dict, nazwa: str) -> str:
    klucz = PASMA_10M.get(nazwa) or PASMA_20M.get(nazwa) or nazwa.lower()
    if klucz not in item["assets"]:
        raise KeyError(f"scena {item['id']} nie ma pasma {nazwa} (klucz {klucz})")
    return item["assets"][klucz]["href"]


def _nazwa_cache(href: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", href.rsplit("/", 2)[-2] + "_" + href.rsplit("/", 1)[-1])


def pobierz_plik(href: str, katalog: str | Path) -> Path:
    """Ściągnij COG do cache raz; drugie wywołanie nie rusza sieci."""
    katalog = Path(katalog)
    katalog.mkdir(parents=True, exist_ok=True)
    cel = katalog / _nazwa_cache(href)
    if cel.exists() and cel.stat().st_size > 0:
        return cel
    dane = _get(href, timeout=600)
    cel.write_bytes(dane)
    return cel


# ────────────────────────────────────────────────────────────────────────────
# Siatka i odczyt pasm
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Siatka:
    """Docelowa siatka 10 m w EPSG:32633 (UTM 33N) — jedna dla wszystkich działek."""
    transform: object
    szer: int
    wys: int
    crs: str = "EPSG:32633"
    res: float = 10.0


def zrob_siatek(geom_utm, res: float = 10.0, margines: float = 30.0) -> Siatka:
    """Siatka obejmująca geometrię (EPSG:32633) + margines, wyrównana do wielokrotności res."""
    minx, miny, maxx, maxy = geom_utm.bounds
    minx -= margines
    miny -= margines
    maxx += margines
    maxy += margines
    x0 = math.floor(minx / res) * res
    y0 = math.floor(miny / res) * res
    szer = int(math.ceil((maxx - x0) / res))
    wys = int(math.ceil((maxy - y0) / res))
    from rasterio.transform import from_origin
    return Siatka(transform=from_origin(x0, y0 + wys * res, res, res), szer=szer, wys=wys)


def wczytaj_pasmo(sciezka: str | Path, siatka: Siatka, metoda: str = "bilinear") -> np.ndarray:
    """Wczytaj pasmo COG do siatki docelowej. Zwraca float32 z nan dla nodata.

    `metoda`: 'bilinear' dla odbiciowości, 'nearest' dla SCL.
    """
    import rasterio
    from rasterio.warp import Resampling, reproject
    from rasterio.windows import from_bounds
    from rasterio.warp import transform_bounds

    with rasterio.open(sciezka) as src:
        b = transform_bounds(siatka.crs, src.crs, *(
            siatka.transform.c, siatka.transform.f + siatka.wys * siatka.transform.e,
            siatka.transform.c + siatka.szer * siatka.transform.a, siatka.transform.f))
        okno = from_bounds(*b, transform=src.transform).round_offsets().round_lengths()
        # Dwa piksele zapasu: bez tego ostatnia kolumna siatki 10 m wypada poza
        # okno pasma 20 m (B05/SCL) i wychodzi pasmo NaN na brzegu mapy.
        from rasterio.windows import Window
        okno = Window(okno.col_off - 2, okno.row_off - 2, okno.width + 4, okno.height + 4)
        dane = src.read(1, window=okno, boundless=True, fill_value=0)
        zrodlo_transform = src.window_transform(okno)
        zrodlo_crs = src.crs
        nodata = src.nodata

    wynik = np.full((siatka.wys, siatka.szer), np.nan, dtype=np.float32)
    reproject(
        source=dane.astype(np.float32),
        destination=wynik,
        src_transform=zrodlo_transform,
        src_crs=zrodlo_crs,
        dst_transform=siatka.transform,
        dst_crs=siatka.crs,
        resampling=Resampling.bilinear if metoda == "bilinear" else Resampling.nearest,
        src_nodata=nodata if nodata is not None else 0,
        dst_nodata=np.nan,
    )
    return wynik


def odbicie(dn: np.ndarray) -> np.ndarray:
    """L2A: odbicie = DN * 0,0001. DN=0 to brak danych.

    **Rozbieżność źródeł, zmierzona — nie przepisana z dokumentacji:** STAC podaje
    dla tego samego kafla `scale: 0.0001, offset: -0.1`. Zastosowanie offsetu daje
    ujemne odbicie w czerwieni (B04 percentyl 1% = -0,075) i NDVI > 1,3, czyli
    wartości fizycznie niemożliwe. Plik COG ma `scales=(1.0,)`, `offsets=(0.0,)`,
    a DN×0,0001 daje czerwieni 0,013–0,218 i NIR 0,103–0,598 — zakres prawdziwy.
    Wniosek: COG-i Earth Search są już zharmonizowane (offset zdjęty), a pole
    `offset` w STAC opisuje surowy produkt ESA, nie ten plik. Liczymy bez offsetu
    i mówimy o tym w raporcie; nie „poprawiamy" danych pod z góry założony wynik.
    """
    x = dn.astype(np.float32) * 0.0001
    x[dn == 0] = np.nan
    return x


def ndvi(nir: np.ndarray, red: np.ndarray) -> np.ndarray:
    licz = nir.astype(np.float64) - red.astype(np.float64)
    mian = nir.astype(np.float64) + red.astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(mian > 0, licz / mian, np.nan)
    return out.astype(np.float32)


def ndre(nir: np.ndarray, red_edge: np.ndarray) -> np.ndarray:
    licz = nir.astype(np.float64) - red_edge.astype(np.float64)
    mian = nir.astype(np.float64) + red_edge.astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(mian > 0, licz / mian, np.nan)
    return out.astype(np.float32)


def maska_dzialki(geom_utm, siatka: Siatka) -> np.ndarray:
    """Rasteryzacja granicy ULDK: piksel należy do działki, gdy jego środek jest w środku."""
    from rasterio.features import rasterize
    zrodlo = [(geom_utm.__geo_interface__, 1)]
    return rasterize(zrodlo, out_shape=(siatka.wys, siatka.szer), transform=siatka.transform,
                     fill=0, all_touched=False, dtype="uint8").astype(bool)


@dataclass
class StatystykiPola:
    pole_id: str
    powierzchnia_ha_uldk: float
    piksele_pola: int
    piksele_czyste: int
    chmury_pct: float
    piksele_uzyte: int
    ndvi_srednia: float
    ndvi_min: float
    ndvi_max: float
    ndvi_odch: float
    ndre_srednia: float
    ndre_min: float
    ndre_max: float
    ndre_odch: float
    blad_std_ndvi: float
    blad_std_ndre: float
    ci95_ndvi: float
    ci95_ndre: float

    def do_dict(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def _scl_int(scl: np.ndarray) -> np.ndarray:
    """SCL po resamplingu ma nan poza sceną; nan → 0 (brak danych), nie rzutowanie w ciemno."""
    return np.nan_to_num(scl, nan=0.0, posinf=0.0, neginf=0.0).astype(int)


def statystyki(maska: np.ndarray, scl: np.ndarray, ndvi_arr: np.ndarray,
               ndre_arr: np.ndarray, pole_id: str, powierzchnia_ha: float) -> StatystykiPola:
    """Policz wskaźniki tylko z czystych pikseli; chmury są wynikiem, nie wyjątkiem."""
    piksele = int(maska.sum())
    if piksele == 0:
        raise ValueError(f"działka {pole_id}: maska nie obejmuje żadnego piksela")
    czyste = maska & np.isin(_scl_int(scl), list(SCL_CZYSTE))
    uzyte = czyste & np.isfinite(ndvi_arr) & np.isfinite(ndre_arr)
    if int(uzyte.sum()) == 0:
        raise ValueError(
            f"działka {pole_id}: 0 pikseli pomiarowych (chmury {100 * (1 - czyste.sum() / piksele):.0f}%)")
    v_ndvi = ndvi_arr[uzyte].astype(np.float64)
    v_ndre = ndre_arr[uzyte].astype(np.float64)
    n = len(v_ndvi)
    sd_ndvi = float(v_ndvi.std(ddof=1)) if n > 1 else 0.0
    sd_ndre = float(v_ndre.std(ddof=1)) if n > 1 else 0.0
    se_ndvi = sd_ndvi / math.sqrt(n)
    se_ndre = sd_ndre / math.sqrt(n)
    return StatystykiPola(
        pole_id=pole_id,
        powierzchnia_ha_uldk=round(float(powierzchnia_ha), 4),
        piksele_pola=piksele,
        piksele_czyste=int(czyste.sum()),
        chmury_pct=round(100.0 * (1.0 - czyste.sum() / piksele), 1),
        piksele_uzyte=n,
        ndvi_srednia=float(v_ndvi.mean()),
        ndvi_min=float(v_ndvi.min()),
        ndvi_max=float(v_ndvi.max()),
        ndvi_odch=sd_ndvi,
        ndre_srednia=float(v_ndre.mean()),
        ndre_min=float(v_ndre.min()),
        ndre_max=float(v_ndre.max()),
        ndre_odch=sd_ndre,
        blad_std_ndvi=se_ndvi,
        blad_std_ndre=se_ndre,
        ci95_ndvi=1.96 * se_ndvi,
        ci95_ndre=1.96 * se_ndre,
    )


def chmury_nad_dzialka(maska: np.ndarray, scl: np.ndarray) -> dict:
    """Rozkład klas SCL nad działką — do wyboru sceny i do uczciwego „ile chmur"."""
    if maska.sum() == 0:
        return {"piksele": 0, "czyste_pct": 0.0, "chmury_pct": 0.0, "cien_pct": 0.0,
                "snieg_pct": 0.0, "brak_danych_pct": 0.0}
    s = _scl_int(scl[maska])
    n = len(s)
    return {
        "piksele": n,
        "czyste_pct": round(100.0 * np.isin(s, list(SCL_CZYSTE)).sum() / n, 1),
        "chmury_pct": round(100.0 * np.isin(s, list(SCL_CHMURY)).sum() / n, 1),
        "cien_pct": round(100.0 * (s == SCL_CIEN).sum() / n, 1),
        "snieg_pct": round(100.0 * (s == SCL_SNIEG).sum() / n, 1),
        "brak_danych_pct": round(100.0 * (s == 0).sum() / n, 1),
    }


def data_sceny(item: dict) -> str:
    return str(item["properties"]["datetime"])[:10]


# ────────────────────────────────────────────────────────────────────────────
# Lokalne przekierowanie HTTP — obejście schannel na tym laptopie
# ────────────────────────────────────────────────────────────────────────────
#
# Zmierzone 02.11.2026: GDAL z tego koła (rasterio 3.12.1, libcurl/schannel)
# nie otwiera COG po https — `AcquireCredentialsHandle failed:
# SEC_E_NO_CREDENTIALS`. To nie jest wina S3: `urllib` (OpenSSL) czyta ten sam
# plik bez problemu. Dlatego GDAL dostaje adres `http://127.0.0.1:port/<https>`,
# a lokalny wątek przepisuje żądania (w tym Range) przez `urllib`. Ruch idzie
# tylko na pętlę zwrotną; serwer żyje tyle, co przebieg.

class _Przekierowanie(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):  # cisza: nie zaśmiecamy logu przebiegu
        pass

    def do_HEAD(self):
        self._obsluz(True)

    def do_GET(self):
        self._obsluz(False)

    def _obsluz(self, head: bool):
        url = urllib.parse.unquote(self.path[1:])
        if not url.startswith(("http://", "https://")):
            self.send_error(400, "adres nie jest http(s)")
            return
        naglowki = {"User-Agent": "field-from-above/0.1"}
        zakres = self.headers.get("Range")
        if zakres:
            naglowki["Range"] = zakres
        zadanie = urllib.request.Request(url, headers=naglowki,
                                         method="HEAD" if head else "GET")
        try:
            with urllib.request.urlopen(zadanie, timeout=180) as odp:
                self.send_response(odp.status)
                for klucz in ("Content-Type", "Content-Length", "Content-Range",
                              "Last-Modified", "ETag"):
                    wartosc = odp.headers.get(klucz)
                    if wartosc:
                        self.send_header(klucz, wartosc)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                if not head:
                    dl = int(odp.headers.get("Content-Length") or 0)
                    pozostalo = dl
                    while pozostalo > 0:
                        porcja = odp.read(min(65536, pozostalo))
                        if not porcja:
                            break
                        self.wfile.write(porcja)
                        pozostalo -= len(porcja)
        except Exception as blad:  # noqa: BLE001 — klient GDAL ma dostać kod, nie wyjątek
            try:
                self.send_error(502, str(blad).encode("ascii", "replace").decode("ascii"))
            except Exception:  # noqa: BLE001 — klient już zamknął połączenie
                pass


class _SerwerPrzekierowania(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class Przekierowanie:
    """Kontekst: start lokalnego proxy i tłumaczenie adresów COG na nie."""

    def __init__(self):
        self.serwer: _SerwerPrzekierowania | None = None
        self.port: int | None = None

    def __enter__(self):
        self.serwer = _SerwerPrzekierowania(("127.0.0.1", 0), _Przekierowanie)
        self.port = self.serwer.server_address[1]
        threading.Thread(target=self.serwer.serve_forever, daemon=True).start()
        return self

    def adres(self, href: str) -> str:
        if self.port is None:
            raise RuntimeError("przekierowanie nie zostało uruchomione")
        return f"http://127.0.0.1:{self.port}/{href}"

    def __exit__(self, *_):
        if self.serwer is not None:
            self.serwer.shutdown()
            self.serwer.server_close()
