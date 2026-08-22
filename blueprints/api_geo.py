"""
Geospatial REST API -- the only way the browser gets coordinates.

    GET /api/geo/config      map centre, zoom, tile URL, zone colour groups
    GET /api/geo/barangays   FeatureCollection of the 31 barangay zones
    GET /api/geo/mrfs        one MRF location per barangay
    GET /api/geo/hotspots    ?barangay=&from=&to=&severity=
    GET /api/geo/status      which geo files are real and which are stubs

Every response carries a `meta` block describing what was actually loaded, so
the client can render an honest empty state ("No hotspot data loaded yet")
instead of a blank map with no explanation. These endpoints are public: the
data is boundaries and facility locations, which the public viewer's map needs
without a login. Nothing here exposes a person, a household, or a live vehicle
position -- those arrive over authenticated socket rooms in Phase 7.
"""

from flask import Blueprint, jsonify, request

from config import Config
from services import geo_service

api_geo_bp = Blueprint("api_geo", __name__)


@api_geo_bp.get("/config")
def map_config():
    """Everything the client needs to build a Leaflet map, minus the data."""
    return jsonify({
        "center": list(Config.MAP_DEFAULT_CENTER),
        "zoom": Config.MAP_DEFAULT_ZOOM,
        "tile_url": Config.MAP_TILE_URL,
        "attribution": Config.MAP_TILE_ATTRIBUTION,
        "zone_groups": geo_service.zone_groups(),
        "hotspot_layer_enabled": Config.HOTSPOT_LAYER_ENABLED,
        "hotspot_source": Config.HOTSPOT_SOURCE,
    })


@api_geo_bp.get("/barangays")
def barangays():
    return jsonify(geo_service.barangay_zones())


@api_geo_bp.get("/mrfs")
def mrfs():
    return jsonify(geo_service.mrf_locations())


@api_geo_bp.get("/hotspots")
def hotspots():
    return jsonify(geo_service.hotspots(
        barangay=request.args.get("barangay") or None,
        date_from=request.args.get("from") or None,
        date_to=request.args.get("to") or None,
        severity=request.args.get("severity") or None,
    ))


@api_geo_bp.get("/status")
def status():
    return jsonify(geo_service.status())
