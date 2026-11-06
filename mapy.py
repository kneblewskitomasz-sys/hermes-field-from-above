"""Mapy do demo „Pole z góry": ortofoto + ULDK + SAM, NDVI, NDRE.

Jedna zasada: mapa pokazuje **zmierzony raster**, nie wygładzoną ilustrację.
Dlatego NDVI/NDRE rysujemy w rozdzielczości 10 m (nearest), z podziałką i liczbą
pikseli; ortofoto jest tłem kontekstowym z własną datą. Granica ULDK i maski SAM
są osobnymi warstwami, każda z datą w podpisie.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import patheffects  # noqa: E402
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from PIL import Image  # noqa: E402

# Progi zapisane PRZED pierwszym uruchomieniem na danych (S155a, 02.11.2026).
# Te same progi na każdym terminie i na każdej działce — inaczej mapy kłamią.
PROGI_NDVI = [0.20, 0.35, 0.50, 0.70]
KLASY_NDVI = ["very low", "low", "medium", "good", "very good"]
KOLORY_NDVI = ["#d73027", "#fc8d59", "#fee08b", "#91cf60", "#1a9850"]

# Klasy SAM: kolor konturu i wypełnienia (spójne z S147o, żeby dało się porównać)
SAM_KOLORY = {
    "agricultural_field": ("#00ff66", 0.0),
    "crop_field": ("#00b7ff", 0.0),
    "grass": ("#a6ff00", 0.22),
    "bare_soil": ("#ffa500", 0.28),
    "building": ("#ff00ff", 0.45),
    "road": ("#ffee00", 0.0),
}
SAM_NAZWY = {
    "agricultural_field": "agricultural field",
    "crop_field": "crop field",
    "grass": "grass",
    "bare_soil": "bare soil",
    "building": "building",
    "road": "road",
}

# Na mapie rysujemy tylko klasy, które coś lokalizują. „pole uprawne" i „uprawa"
# pokrywają większość kadru i nachodzą na siebie — ich kontury zamalowałyby zdjęcie,
# więc są w liczbach (panel SAM), ale nie w rysunku.
SAM_RYSUJ = ("grass", "bare_soil", "building", "road")


def wczytaj_maski_sam(katalog: str | Path) -> dict[str, np.ndarray]:
    katalog = Path(katalog)
    out = {}
    for plik in sorted(katalog.glob("maski-*.npz")):
        nazwa = plik.stem.replace("maski-", "")
        z = np.load(plik)
        out[nazwa] = z["masks"].any(axis=0)
    if not out:
        raise FileNotFoundError(f"brak masek SAM w {katalog}")
    return out


def _przezroczysty(hex_kolor: str, alpha: float) -> tuple[float, float, float, float]:
    h = hex_kolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (r, g, b, alpha)


def _kadr_orto(bbox2180, bbox_orto, szer_orto: int, wys_orto: int, margines_m: float):
    x0, y0, x1, y1 = bbox2180
    x0 -= margines_m
    y0 -= margines_m
    x1 += margines_m
    y1 += margines_m
    ox0, oy0, ox1, oy1 = bbox_orto
    kol0 = int(max(0, (x0 - ox0) / (ox1 - ox0) * szer_orto))
    kol1 = int(min(szer_orto, (x1 - ox0) / (ox1 - ox0) * szer_orto))
    wier0 = int(max(0, (oy1 - y1) / (oy1 - oy0) * wys_orto))
    wier1 = int(min(wys_orto, (oy1 - y0) / (oy1 - oy0) * wys_orto))
    if kol1 <= kol0 or wier1 <= wier0:
        raise ValueError("kadr ortofoto wyszedł pusty")
    return slice(wier0, wier1), slice(kol0, kol1)


def _kadr_siatki(bbox32633, siatka, margines_m: float):
    x0, y0, x1, y1 = bbox32633
    x0 -= margines_m
    y0 -= margines_m
    x1 += margines_m
    y1 += margines_m
    # zapis w JSON to lista Affine (a, b, c, d, e, f), nie kolejność GDAL
    a, _b, c, _d, e, f = siatka["transform"][:6]
    kol0 = int(max(0, np.floor((x0 - c) / a)))
    kol1 = int(min(siatka["szer"], np.ceil((x1 - c) / a)))
    wier0 = int(max(0, np.floor((f - y1) / -e)))
    wier1 = int(min(siatka["wys"], np.ceil((f - y0) / -e)))
    return slice(wier0, wier1), slice(kol0, kol1), (c, a, f, e)


def _granica(ax, geom, crs_docelowy, kolor="#ffe100", grubosc=2.2, opis=None,
             odsuniecie=(0, 0), strzalka=False):
    from shapely.ops import transform as sh_transform
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:4326", crs_docelowy, always_xy=True)
    g = sh_transform(tr.transform, geom)
    x, y = g.exterior.xy
    ax.plot(x, y, color="black", lw=grubosc + 1.8, alpha=0.45, zorder=5)
    ax.plot(x, y, color=kolor, lw=grubosc, zorder=6)
    if opis:
        p = g.representative_point()
        ax.annotate(opis, (p.x, p.y), xytext=odsuniecie, textcoords="offset points",
                    fontsize=9.5, fontweight="bold", color="white", zorder=8,
                    ha="center", va="center",
                    arrowprops=({"arrowstyle": "-", "color": "white", "lw": 1.0,
                                 "shrinkA": 0, "shrinkB": 0} if strzalka else None),
                    path_effects=[patheffects.withStroke(linewidth=3, foreground="black")])


def rysuj_orto(sciezka_png, orto_png, bbox_orto, bbox2180, geom4326, maski_sam,
               tytul: str, podtytul: str, margines_m: float = 45.0, opis_pola: str = ""):
    im = Image.open(io.BytesIO(orto_png)).convert("RGB")
    arr = np.asarray(im)
    wier, kol = _kadr_orto(bbox2180, bbox_orto, im.width, im.height, margines_m)
    crop = arr[wier, kol]
    ox0, oy0, ox1, oy1 = bbox_orto
    x0 = ox0 + kol.start / im.width * (ox1 - ox0)
    x1 = ox0 + kol.stop / im.width * (ox1 - ox0)
    y1 = oy1 - wier.start / im.height * (oy1 - oy0)
    y0 = oy1 - wier.stop / im.height * (oy1 - oy0)
    proporcja = max(0.5, (x1 - x0) / (y1 - y0))
    fig, ax = plt.subplots(figsize=(7.4, max(3.6, min(6.4, 7.4 / proporcja + 1.9))), dpi=140)
    ax.imshow(crop, extent=(x0, x1, y0, y1), origin="upper")
    for nazwa in SAM_RYSUJ:
        maska = maski_sam.get(nazwa)
        if maska is None:
            continue
        m = maska[wier, kol]
        if not m.any():
            continue
        kolor, alpha = SAM_KOLORY[nazwa]
        if alpha > 0:
            rgba = np.zeros((*m.shape, 4), dtype=np.float32)
            rgba[m] = _przezroczysty(kolor, alpha)
            ax.imshow(rgba, extent=(x0, x1, y0, y1), origin="upper")
        if nazwa == "road":
            ax.contour(np.linspace(x0, x1, m.shape[1]), np.linspace(y1, y0, m.shape[0]),
                       m.astype(float), levels=[0.5], colors=[kolor], linewidths=1.0, alpha=0.9)
    _granica(ax, geom4326, "EPSG:2180", opis=opis_pola)
    ax.set_title(tytul, fontsize=12.5, fontweight="bold", loc="left", pad=8)
    ax.set_xlabel("EPSG:2180 [m]")
    ax.set_ylabel("EPSG:2180 [m]")
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.tick_params(labelsize=7.5)
    legenda = [Line2D([0], [0], color="#ffe100", lw=2.2, label="ULDK boundary")]
    legenda += [Patch(facecolor=SAM_KOLORY[n][0], alpha=max(SAM_KOLORY[n][1], 0.4),
                      edgecolor=SAM_KOLORY[n][0], label=SAM_NAZWY[n])
                for n in SAM_RYSUJ if maski_sam.get(n) is not None and maski_sam[n].any()]
    ax.legend(handles=legenda, loc="lower right", fontsize=6.8, framealpha=0.85)
    ax.set_aspect("equal")
    fig.text(0.01, 0.005, podtytul, fontsize=7.4, color="#333", va="bottom")
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    fig.savefig(sciezka_png, bbox_inches="tight")
    plt.close(fig)


def rysuj_indeks(sciezka_png, arr, siatka, bbox32633, geom4326, tytul, podtytul,
                 cmap="RdYlGn", vmin=-0.1, vmax=0.9, etykieta="NDVI", margines_m=45.0,
                 opis_pola="", scl=None):
    wier, kol, (c, a, f, e) = _kadr_siatki(bbox32633, siatka, margines_m)
    arr = arr[wier, kol].astype(float)
    if scl is not None:
        scl = scl[wier, kol].astype(float)
        arr = np.where(np.isin(scl, [4, 5, 6, 7]), arr, np.nan)
    x0, x1 = c + kol.start * a, c + kol.stop * a
    y1, y0 = f + wier.start * e, f + wier.stop * e
    proporcja = max(0.5, (x1 - x0) / (y1 - y0))
    fig, ax = plt.subplots(figsize=(7.4, max(3.6, min(6.4, 7.4 / proporcja + 1.9))), dpi=140)
    im = ax.imshow(arr, extent=(x0, x1, y0, y1), origin="upper", cmap=cmap,
                   vmin=vmin, vmax=vmax, interpolation="nearest")
    _granica(ax, geom4326, "EPSG:32633", opis=opis_pola)
    ax.set_title(tytul, fontsize=12.5, fontweight="bold", loc="left", pad=8)
    ax.set_xlabel("EPSG:32633 [m]   — 1 pixel = 10 m")
    ax.set_ylabel("EPSG:32633 [m]")
    ax.ticklabel_format(useOffset=False, style="plain")
    ax.tick_params(labelsize=7.5)
    ax.set_aspect("equal")
    cb = fig.colorbar(im, ax=ax, fraction=0.036, pad=0.02)
    cb.set_label(etykieta, fontsize=9)
    cb.ax.tick_params(labelsize=7.5)
    fig.text(0.01, 0.005, podtytul, fontsize=7.4, color="#333", va="bottom")
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    fig.savefig(sciezka_png, bbox_inches="tight")
    plt.close(fig)


ODSUNIECIA = {
    "90/8": (0, 14), "90/12": (0, 14), "92/6": (-46, 6), "92/10": (0, 20), "92/11": (0, -15),
}


def rysuj_przeglad(sciezka_png, orto_png, bbox_orto, geometrie4326, maski_sam,
                   arr_ndvi, arr_ndre, scl, siatka, statystyki, tytul="Field from Above — Mąkownica"):
    """Trzy panele: ortofoto+SAM, NDVI, NDRE — wspólny kadr wszystkich działek."""
    im = Image.open(io.BytesIO(orto_png)).convert("RGB")
    arr = np.asarray(im)
    fig, osie = plt.subplots(1, 3, figsize=(15.8, 3.4), dpi=150)
    ox0, oy0, ox1, oy1 = bbox_orto
    ax = osie[0]
    ax.imshow(arr, extent=(ox0, ox1, oy0, oy1), origin="upper")
    for nazwa in SAM_RYSUJ:
        maska = maski_sam.get(nazwa)
        if maska is None:
            continue
        kolor, alpha = SAM_KOLORY[nazwa]
        if alpha > 0:
            rgba = np.zeros((*maska.shape, 4), dtype=np.float32)
            rgba[maska] = _przezroczysty(kolor, alpha)
            ax.imshow(rgba, extent=(ox0, ox1, oy0, oy1), origin="upper")
        if nazwa == "road":
            ax.contour(np.linspace(ox0, ox1, maska.shape[1]), np.linspace(oy1, oy0, maska.shape[0]),
                       maska.astype(float), levels=[0.5], colors=[kolor], linewidths=0.7, alpha=0.9)
    for ident, g in geometrie4326.items():
        krotki = ident.split(".")[-1]
        ods = ODSUNIECIA.get(krotki, (0, 12))
        _granica(ax, g, "EPSG:2180", grubosc=1.5, opis=krotki, odsuniecie=ods,
                 strzalka=max(abs(ods[0]), abs(ods[1])) >= 18)
    ax.set_title("Orthophoto 2025-03-28 + ULDK + SAM 3.1 masks (2026-10-28)",
                 fontsize=9.8, fontweight="bold", loc="left", pad=6)
    ax.set_aspect("equal")
    ax.set_xlabel("EPSG:2180 [m]", fontsize=8)
    ax.set_ylabel("EPSG:2180 [m]", fontsize=8)
    ax.tick_params(labelsize=6.2)
    ax.ticklabel_format(useOffset=False, style="plain")
    legenda = [Line2D([0], [0], color="#ffe100", lw=2.0, label="ULDK boundary")]
    legenda += [Patch(facecolor=SAM_KOLORY[n][0], alpha=max(SAM_KOLORY[n][1], 0.4),
                      edgecolor=SAM_KOLORY[n][0], label=SAM_NAZWY[n])
                for n in SAM_RYSUJ if maski_sam.get(n) is not None and maski_sam[n].any()]
    ax.legend(handles=legenda, loc="lower right", fontsize=5.8, framealpha=0.85)

    a, _b, c, _d, e, f = siatka["transform"][:6]
    x0, x1 = c, c + siatka["szer"] * a
    y1, y0 = f, f + siatka["wys"] * e
    for ax, arrx, tyt, cmap, vmin, vmax, et in (
        (osie[1], arr_ndvi, "NDVI — Sentinel-2 L2A, 2026-10-26", "RdYlGn", -0.05, 0.9, "NDVI"),
        (osie[2], arr_ndre, "NDRE — Sentinel-2 L2A, 2026-10-26", "viridis", -0.05, 0.65, "NDRE"),
    ):
        scl_i = np.nan_to_num(scl, nan=0.0).astype(int)
        pokaz = np.where(np.isin(scl_i, [4, 5, 6, 7]), arrx, np.nan)
        im2 = ax.imshow(pokaz, extent=(x0, x1, y0, y1), origin="upper", cmap=cmap,
                        vmin=vmin, vmax=vmax, interpolation="nearest")
        for ident, g in geometrie4326.items():
            krotki = ident.split(".")[-1]
            ods = ODSUNIECIA.get(krotki, (0, 12))
            _granica(ax, g, "EPSG:32633", grubosc=1.4, opis=krotki, odsuniecie=ods,
                     strzalka=max(abs(ods[0]), abs(ods[1])) >= 18)
        ax.set_title(tyt, fontsize=9.8, fontweight="bold", loc="left", pad=6)
        ax.set_aspect("equal")
        ax.set_xlabel("EPSG:32633 [m] — 1 pixel = 10 m", fontsize=8)
        ax.set_ylabel("EPSG:32633 [m]", fontsize=8)
        ax.tick_params(labelsize=6.2)
        ax.ticklabel_format(useOffset=False, style="plain")
        cb = fig.colorbar(im2, ax=ax, fraction=0.03, pad=0.02)
        cb.set_label(et, fontsize=7.5)
        cb.ax.tick_params(labelsize=6.2)

    fig.suptitle(tytul, fontsize=12.0, fontweight="bold", x=0.012, ha="left", y=0.985)
    fig.text(0.012, 0.012,
             "Data: ULDK/GUGiK (read 2026-11-01) · orthophoto GUGiK WMS 1.1.1, photo 2025-03-28 · "
             "Sentinel-2 L2A Earth Search, scene 2026-10-26 · SAM 3.1, run 2026-10-28. "
             "This is not a soil test or a fertiliser recommendation.",
             fontsize=6.0, color="#444")
    fig.subplots_adjust(left=0.045, right=0.985, top=0.80, bottom=0.17, wspace=0.26)
    fig.savefig(sciezka_png, bbox_inches="tight")
    plt.close(fig)


# ────────────────────────────────────────────────────────────────────────────
# S155a: sezon, krzywa, teren
# ────────────────────────────────────────────────────────────────────────────

def _mapa_klas():
    cmap = ListedColormap(KOLORY_NDVI)
    cmap.set_bad("white")
    norm = BoundaryNorm([-1.0] + PROGI_NDVI + [2.0], cmap.N)
    return cmap, norm


def rysuj_sezon(sciezka_png, obrazy, maska, extent, terminy, tytul):
    """2×2 terminy, jedna skala pięciu klas, tylko obszar działki (poza białe)."""
    cmap, norm = _mapa_klas()
    fig, osie = plt.subplots(2, 2, figsize=(9.4, 8.6), dpi=150)
    for ax, arr, term in zip(osie.ravel(), obrazy, terminy):
        pokaz = np.where(maska & np.isfinite(arr), arr, np.nan)
        ax.set_facecolor("white")
        ax.imshow(pokaz, extent=extent, origin="upper", cmap=cmap, norm=norm,
                  interpolation="nearest")
        ax.set_xticks([])
        ax.set_yticks([])
        for krawedz in ax.spines.values():
            krawedz.set_visible(False)
        ax.set_title(f"{term['data']}  ·  clean {term['czyste_pct']:.1f}%",
                     fontsize=10.5, fontweight="bold", pad=5)
    uchwyty = [Patch(facecolor=KOLORY_NDVI[0], label=f"very low  < {PROGI_NDVI[0]:.2f}"),
               Patch(facecolor=KOLORY_NDVI[1], label=f"low  {PROGI_NDVI[0]:.2f}–{PROGI_NDVI[1]:.2f}"),
               Patch(facecolor=KOLORY_NDVI[2], label=f"medium  {PROGI_NDVI[1]:.2f}–{PROGI_NDVI[2]:.2f}"),
               Patch(facecolor=KOLORY_NDVI[3], label=f"good  {PROGI_NDVI[2]:.2f}–{PROGI_NDVI[3]:.2f}"),
               Patch(facecolor=KOLORY_NDVI[4], label=f"very good  ≥ {PROGI_NDVI[3]:.2f}")]
    fig.legend(handles=uchwyty, loc="lower center", ncol=5, fontsize=7.4,
               frameon=False, bbox_to_anchor=(0.5, 0.035))
    fig.suptitle(tytul, fontsize=13.0, fontweight="bold", x=0.04, ha="left", y=0.975)
    fig.text(0.04, 0.012,
             "Same thresholds on all four dates · NDVI from Sentinel-2 L2A · "
             "colour only inside the parcel · white = outside parcel or cloud/nodata.",
             fontsize=6.8, color="#444")
    fig.subplots_adjust(left=0.02, right=0.98, top=0.90, bottom=0.13, wspace=0.04, hspace=0.12)
    # Metadane w PNG: ta sama data i ten sam odsetek czystych, co w JSON.
    opis = "dates=" + ";".join(f"{t['data']}:{t['czyste_pct']:.1f}" for t in terminy)
    opis += "|thresholds=" + ",".join(f"{p:.2f}" for p in PROGI_NDVI)
    fig.savefig(sciezka_png, bbox_inches="tight", facecolor="white",
                metadata={"Description": opis})
    plt.close(fig)


def rysuj_krzywa(sciezka_png, punkty, segmenty, terminy, tytul):
    """Średnia NDVI w czasie; przerwy w linii są jawne, bez interpolacji przez lukę."""
    import matplotlib.dates as mdates
    from datetime import date

    fig, ax = plt.subplots(figsize=(9.6, 4.8), dpi=150)
    for odcinek in segmenty:
        if len(odcinek) >= 2:
            ax.plot([p[0] for p in odcinek], [p[1] for p in odcinek],
                    color="#1f5f8b", lw=1.7, zorder=2)
    ax.scatter([p[0] for p in punkty], [p[1] for p in punkty],
               s=18, color="#1f5f8b", zorder=3, label="accepted scene (mean NDVI)")
    if terminy:
        ax.scatter([date.fromisoformat(t["data"]) for t in terminy],
                   [t["ndvi"] for t in terminy], s=120, marker="*",
                   color="#d73027", zorder=5, label="date shown on the season map")
        for t in terminy:
            ax.annotate(t["data"][5:], (date.fromisoformat(t["data"]), t["ndvi"]),
                        textcoords="offset points", xytext=(0, 9), ha="center",
                        fontsize=7.2, color="#8b1a1a")
    ax.set_title(tytul, fontsize=12.5, fontweight="bold", loc="left", pad=8)
    ax.set_xlabel("Date (2026)", fontsize=9)
    ax.set_ylabel("Mean NDVI inside parcel", fontsize=9)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.grid(alpha=0.25)
    ax.legend(fontsize=7.6, loc="best")
    if punkty:
        wartosci = [p[1] for p in punkty]
        ax.set_ylim(max(-0.05, min(wartosci) - 0.08), min(1.0, max(wartosci) + 0.10))
    fig.text(0.01, 0.01,
             "Line breaks at a gap longer than 45 days or a month without an accepted scene — "
             "no interpolation through a gap.",
             fontsize=6.8, color="#444")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    # Metadane w PNG: dokładnie te cztery daty z map, z NDVI z JSON.
    opis = "map_dates=" + ";".join(
        f"{t['data']}:{t['ndvi']:.4f}" for t in terminy if t.get("ndvi") is not None)
    fig.savefig(sciezka_png, bbox_inches="tight", facecolor="white",
                metadata={"Description": opis})
    plt.close(fig)


# ────────────────────────────────────────────────────────────────────────────
# S155b2: kolor, który nie udaje tła, i punkty tylko z rysowanych pikseli
# ────────────────────────────────────────────────────────────────────────────

# `terrain` kończy się bielą. Przy vmin/vmax z danych najwyższy piksel był biały
# jak papier, więc punkt C (najwyższy) wyglądał, jakby leżał poza kolorowym
# obszarem — choć był w masce. Ucinamy górne 14% skali: maksimum jest brunatne,
# a nie białe.
_TERRAIN = plt.get_cmap("terrain")
CMAP_TEREN = LinearSegmentedColormap.from_list(
    "terrain_bez_bieli", _TERRAIN(np.linspace(0.0, 0.86, 256)))


def kolor_terenu(frac: float) -> tuple[int, int, int]:
    """Kolor skali terenu dla ułamka 0..1 (1 = najwyższy piksel). Do testu."""
    r, g, b, _ = CMAP_TEREN(min(1.0, max(0.0, float(frac))))
    return tuple(int(round(255 * c)) for c in (r, g, b))


def sprawdz_punkty_na_mapie(punkty, arr, maska, extent):
    """Każdy punkt A/B/C musi leżeć na pikselu, który mapa NAPRAWDĘ rysuje.

    Rysowany piksel = w masce działki i ze skończoną wysokością. Punkt poza
    maską albo na NaN to błąd, nie cichy znacznik na białym tle. Podnosi
    ValueError z nazwą punktu — build/test ma paść głośno.
    """
    x0, x1, y0, y1 = extent
    wys, szer = maska.shape
    bledy = []
    for klucz, p in punkty.items():
        kol = int((p["x"] - x0) / (x1 - x0) * szer)
        wier = int((y1 - p["y"]) / (y1 - y0) * wys)
        if not (0 <= wier < wys and 0 <= kol < szer):
            bledy.append("%s (%.1f, %.1f) poza kadrem mapy" % (klucz, p["x"], p["y"]))
            continue
        if not bool(maska[wier, kol]):
            bledy.append("%s (%.1f, %.1f) poza maską działki" % (klucz, p["x"], p["y"]))
        elif not np.isfinite(arr[wier, kol]):
            bledy.append("%s (%.1f, %.1f) na pikselu bez wysokości" % (klucz, p["x"], p["y"]))
    if bledy:
        raise ValueError("punkty poza rysowanym obszarem: " + "; ".join(bledy))
    return True


def rysuj_teren(sciezka_png, arr, maska, extent, tytul, punkty, status):
    """Kolor tylko w obrębie działki; BRAK DANYCH nie jest płaskim terenem."""
    fig, ax = plt.subplots(figsize=(7.4, 5.8), dpi=150)
    ax.set_facecolor("white")
    opis_meta = f"status={status}"
    if status != "OK":
        ax.text(0.5, 0.5, "BRAK DANYCH", transform=ax.transAxes, ha="center", va="center",
                fontsize=22, fontweight="bold", color="#b00020")
        ax.text(0.5, 0.40, "GUGiK NMT did not return enough heights — this is not flat terrain.",
                transform=ax.transAxes, ha="center", va="center", fontsize=9, color="#333")
    else:
        pokaz = np.where(maska & np.isfinite(arr), arr, np.nan)
        vmin, vmax = float(np.nanmin(pokaz)), float(np.nanmax(pokaz))
        if not np.isfinite(vmin) or not np.isfinite(vmax):
            return rysuj_teren(sciezka_png, arr, maska, extent, tytul, punkty, "BRAK DANYCH")
        # S155b2: najpierw sprawdź, że A/B/C leżą na pikselach rysowanych
        # (maska + skończona wysokość); punkt na białym tle = błąd, nie ozdoba.
        sprawdz_punkty_na_mapie(punkty, arr, maska, extent)
        im = ax.imshow(pokaz, extent=extent, origin="upper", cmap=CMAP_TEREN,
                       vmin=vmin, vmax=vmax, interpolation="nearest")
        # cienki obrys działki: nawet najwyższy (jasny) piksel nie zlewa się z tłem
        ax.contour(np.linspace(extent[0], extent[1], maska.shape[1]),
                   np.linspace(extent[3], extent[2], maska.shape[0]),
                   maska.astype(float), levels=[0.5], colors=["#40503f"],
                   linewidths=0.9, zorder=4)
        opis_meta += f";h_min={vmin:.2f};h_max={vmax:.2f};roznica={vmax - vmin:.2f}"
        cb = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
        cb.set_label("metres above sea level", fontsize=8.5)
        cb.ax.tick_params(labelsize=7.5)
        for klucz, p in punkty.items():
            ax.plot(p["x"], p["y"], marker="o", markersize=7, color="black",
                    markeredgecolor="white", markeredgewidth=1.4, zorder=6)
            ax.annotate(f"{klucz}  {p['h_m']:.1f} m", (p["x"], p["y"]),
                        textcoords="offset points", xytext=(8, 6), fontsize=8.8,
                        fontweight="bold", color="black", zorder=7,
                        path_effects=[patheffects.withStroke(linewidth=2.8, foreground="white")])
    ax.set_title(tytul, fontsize=12.0, fontweight="bold", loc="left", pad=8)
    ax.set_xticks([])
    ax.set_yticks([])
    for krawedz in ax.spines.values():
        krawedz.set_visible(False)
    ax.set_aspect("equal")
    fig.text(0.01, 0.01,
             "GUGiK NMT GetHByXY (EPSG:2180) · 10 m grid at parcel pixels · "
             "A = lowest, B = nearest parcel centre, C = highest · outside parcel white.",
             fontsize=6.6, color="#444")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(sciezka_png, bbox_inches="tight", facecolor="white",
                metadata={"Description": opis_meta})
    plt.close(fig)
