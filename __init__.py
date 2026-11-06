"""Hermes entrypoint for Field from Above."""

from pathlib import Path

from .field_from_above import field_from_above_parcel, field_from_above_point


PARCEL_SCHEMA = {
    "type": "object",
    "properties": {
        "parcel_id": {"type": "string", "description": "Full Polish TERYT parcel identifier."},
        "season": {"type": "integer", "minimum": 2015, "maximum": 2100},
    },
    "required": ["parcel_id"],
    "additionalProperties": False,
}

POINT_SCHEMA = {
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
