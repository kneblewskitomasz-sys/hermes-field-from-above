"""Hermes entrypoint for Field from Above."""

from pathlib import Path

from .field_from_above import field_from_above_parcel, field_from_above_point


PARCEL_PARAMETERS = {
    "type": "object",
    "properties": {
        "parcel_id": {"type": "string", "description": "Full Polish TERYT parcel identifier."},
        "season": {"type": "integer", "minimum": 2015, "maximum": 2100},
    },
    "required": ["parcel_id"],
    "additionalProperties": False,
}

POINT_PARAMETERS = {
    "type": "object",
    "properties": {
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "radius_m": {"type": "number", "minimum": 10, "maximum": 5000},
        "season": {"type": "integer", "minimum": 2015, "maximum": 2100},
    },
    "required": ["latitude", "longitude"],
    "additionalProperties": False,
}

# Hermes sends `schema` to the model as the function definition itself,
# so name and description must live next to the parameters.
PARCEL_SCHEMA = {
    "name": "field_from_above_parcel",
    "description": (
        "Measure a Polish agricultural parcel from above: looks up the parcel by its full "
        "TERYT identifier in GUGiK ULDK, takes the clearest Sentinel-2 L2A scene of the season, "
        "returns dated NDRE/NDVI statistics, terrain context and a PNG map. Not a soil test."
    ),
    "parameters": PARCEL_PARAMETERS,
}

POINT_SCHEMA = {
    "name": "field_from_above_point",
    "description": (
        "Measure an area around a latitude/longitude point (radius in metres) from Sentinel-2 L2A: "
        "returns dated NDRE/NDVI statistics, terrain context (Poland only) and a PNG map. "
        "Not a soil test or fertiliser recommendation."
    ),
    "parameters": POINT_PARAMETERS,
}


def register(ctx):
    """Register exactly the two public tools and the bundled skill."""
    ctx.register_tool(
        name="field_from_above_parcel",
        toolset="field_from_above",
        schema=PARCEL_SCHEMA,
        handler=field_from_above_parcel,
    )
    ctx.register_tool(
        name="field_from_above_point",
        toolset="field_from_above",
        schema=POINT_SCHEMA,
        handler=field_from_above_point,
    )
    ctx.register_skill("field-from-above", str(Path(__file__).with_name("SKILL.md")))
