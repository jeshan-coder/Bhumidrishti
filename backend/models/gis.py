"""GIS layer data models for PostGIS tables."""

from typing import Literal, Any
from pydantic import BaseModel, Field


GisLayerKey = Literal[
    "turkey_provinces",
    "turkey_points",
    "turkey_lines",
    "turkey_districts_pts",
    "turkey_buildings",
    "flood_zones",
    "destroyed_buildings",
]


class TurkeyProvince(BaseModel):
    """Turkey province polygon feature - complete schema."""

    id: int | None = None
    plate: str | None = None
    name_tr: str | None = None
    name_en: str | None = None
    region: str | None = None
    capital: str | None = None
    country: str | None = None


class TurkeyPoint(BaseModel):
    """Turkey point of interest feature - complete schema."""

    id: int | None = None
    osm_id: int | None = None
    osm_type: str | None = None
    medical_sy: str | None = None
    access_roo: str | None = None
    rooms: str | None = None
    surface: str | None = None
    government: str | None = None
    landuse: str | None = None
    isced_leve: str | None = None
    roof_mater: str | None = None
    staff_coun: str | None = None
    blockage: str | None = None
    man_made: str | None = None
    network: str | None = None
    religion: str | None = None
    boundary: str | None = None
    shop: str | None = None
    natural: str | None = None
    office: str | None = None
    diameter: str | None = None
    name: str | None = None
    health_fac: str | None = None
    communicat: str | None = None
    covered: str | None = None
    historic: str | None = None
    name_en: str | None = None
    addr_house: str | None = None
    status: str | None = None
    fuel: str | None = None
    health_f_1: str | None = None
    tunnel: str | None = None
    is_in: str | None = None
    opening_ho: str | None = None
    operator: str | None = None
    place: str | None = None
    tower: str | None = None
    addr_postc: str | None = None
    aeroway: str | None = None
    highway: str | None = None
    railway: str | None = None
    toilets_di: str | None = None
    layer: str | None = None
    staff_co_1: str | None = None
    barrier: str | None = None
    denominati: str | None = None
    name_fr: str | None = None
    operator_t: str | None = None
    toilets_ha: str | None = None
    building: str | None = None
    emergency: str | None = None
    access: str | None = None
    health_f_2: str | None = None
    beds: str | None = None
    power: str | None = None
    pump: str | None = None
    building_m: str | None = None
    leisure: str | None = None
    name_sw: str | None = None
    tourism: str | None = None
    depth: str | None = None
    smoothness: str | None = None
    waterway: str | None = None
    public_tra: str | None = None
    addr_stree: str | None = None
    communic_1: str | None = None
    healthcare: str | None = None
    water: str | None = None
    backup_gen: str | None = None
    bridge: str | None = None
    parking: str | None = None
    military: str | None = None
    oneway: str | None = None
    population: str | None = None
    admin_leve: str | None = None
    capacity: str | None = None
    amenity: str | None = None
    width: str | None = None
    province: str | None = None


class TurkeyLine(BaseModel):
    """Turkey line feature (roads, waterways) - complete schema."""

    id: int | None = None
    osm_id: int | None = None
    osm_type: str | None = None
    surface: str | None = None
    landuse: str | None = None
    blockage: str | None = None
    man_made: str | None = None
    natural: str | None = None
    diameter: str | None = None
    name: str | None = None
    covered: str | None = None
    name_en: str | None = None
    tunnel: str | None = None
    operator: str | None = None
    highway: str | None = None
    aeroway: str | None = None
    railway: str | None = None
    layer: str | None = None
    barrier: str | None = None
    name_fr: str | None = None
    building: str | None = None
    pump: str | None = None
    name_sw: str | None = None
    depth: str | None = None
    smoothness: str | None = None
    waterway: str | None = None
    public_tra: str | None = None
    water: str | None = None
    bridge: str | None = None
    parking: str | None = None
    oneway: str | None = None
    capacity: str | None = None
    amenity: str | None = None
    width: str | None = None
    province: str | None = None


class TurkeyDistrictPoint(BaseModel):
    """Turkey district point feature - complete schema."""

    id: int | None = None
    province: str | None = None
    plate: str | None = None
    district: str | None = None
    region: str | None = None
    lon: str | None = None
    lat: str | None = None
    country: str | None = None


class TurkeyBuilding(BaseModel):
    """Turkey building polygon feature - complete schema."""

    id: int | None = None
    osm_id: int | None = None
    osm_type: str | None = None
    addr_house: str | None = None
    addr_stree: str | None = None
    access_roo: str | None = None
    roof_mater: str | None = None
    building: str | None = None
    name: str | None = None
    building_m: str | None = None
    province: str | None = None


class FloodZone(BaseModel):
    """Flood zone polygon feature (derived from waterway buffer) - complete schema."""

    osm_id: int | None = None
    waterway_type: str | None = None
    waterway_name: str | None = None
    province: str | None = None


class DestroyedBuilding(BaseModel):
    """Earthquake-destroyed building feature - complete schema."""

    id: int | None = None
    osm_id: int | None = None
    addr_house: str | None = None
    addr_full: str | None = None
    damage_eve: str | None = None
    source: str | None = None
    damage_typ: str | None = None
    damage_dat: str | None = None
    building: str | None = None
    destroyed_: str | None = None
    addr_stree: str | None = None
    name: str | None = None
    addr_city: str | None = None
    province: str | None = None


class GisLayerListResponse(BaseModel):
    """Response payload listing available GIS layers."""

    layers: list[str]


class GisLayerResponse(BaseModel):
    """Response payload for a single GIS layer."""

    layer: str
    table: str
    max_features: int
    feature_count: int
    geojson: dict[str, Any]
