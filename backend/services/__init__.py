# This module exports service utilities for BhumiDrishti backend.

from services.gis import fetch_layer_geojson, get_geometry_column_name

__all__ = ["fetch_layer_geojson", "get_geometry_column_name"]
