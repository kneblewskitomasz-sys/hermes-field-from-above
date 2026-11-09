# Field from Above — Hermes plugin

Ask Hermes about a farm field using either a Polish parcel identifier or a
latitude/longitude point. The plugin resolves Polish parcel boundaries through
GUGiK ULDK, searches free Sentinel-2 L2A imagery through Earth Search STAC,
computes NDRE on the clear pixels, queries GUGiK NMT terrain data where
available, and returns a short JSON result with a PNG map path.

The two tools are:

* `field_from_above_parcel` — `parcel_id`, optional `season`;
* `field_from_above_point` — `latitude`, `longitude`, optional `radius_m` and
  `season`, worldwide for Sentinel-2, with terrain explicitly unavailable
  outside the Poland NMT coverage.

## Install and validate

```bash
hermes plugins doctor . --ci
hermes plugins validate . --install-deps
```

The plugin has no API keys, does not read `.env` and does not spawn shell or
background processes or local servers. It stores downloaded raster cache and
generated maps under `$HERMES_HOME/plugin-data/field-from-above/` (cache capped at
2 GB, least recently used files evicted; one band file at most 400 MB). Band files
are downloaded only over https from `sentinel-cogs.s3.us-west-2.amazonaws.com`. Network calls are made only to:

* `uldk.gugik.gov.pl` — Polish parcel boundaries;
* `earth-search.aws.element84.com` — Sentinel-2 STAC and public COG assets;
* `services.gugik.gov.pl` — Poland NMT heights.

The bundled `sentinel.py`, `teren.py` and `mapy.py` are included in this
repository so the installed plugin has no dependency on another checkout.

This is a dated satellite measurement and terrain context. It is not a soil
test, fertiliser recommendation, plant-protection recommendation or agronomic
diagnosis.
