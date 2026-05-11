"""Assessment data model for disaster damage assessments."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


AssessmentStatus = Literal["pending", "in_review", "responded", "closed", "false_positive"]
InputType = Literal["ground_photo", "drone_images", "orthophoto", "satellite", "video"]
StructuralRisk = Literal["high", "moderate", "low", "unknown"]
OccupantStatus = Literal["trapped", "evacuated", "unknown", "none_present"]
RoadAccess = Literal["passable", "blocked", "unknown"]
SlopeRisk = Literal["high", "moderate", "low"]


class Assessment(BaseModel):
    """Complete assessment record with all fields from assessments table."""

    # Identity
    id: str | None = None
    
    # Location
    lat: float
    lon: float
    
    # Administrative location
    province: str | None = None
    district: str | None = None
    address_note: str | None = None
    
    # Input type
    input_type: InputType = "ground_photo"
    
    # File paths
    photo_path: str | None = None
    video_path: str | None = None
    ortho_path: str | None = None
    chip_path: str | None = None
    pre_chip_path: str | None = None
    drone_frames: list[str] | None = None
    
    # Damage assessment from Gemma 4
    severity: int | None = Field(None, ge=1, le=5)
    damage_type: str | None = None
    damage_description: str | None = None
    structural_risk: StructuralRisk | None = None
    
    # Building information
    building_type: str | None = None
    building_floors: str | None = None
    building_material: str | None = None
    osm_building_id: int | None = None
    
    # Occupant estimate
    estimated_occupants: str | None = None
    occupant_status: OccupantStatus | None = None
    
    # Recommended action
    recommended_action: str | None = None
    action_priority: int | None = Field(None, ge=1, le=5)
    
    # Spatial context from PostGIS queries
    flood_zone: bool = False
    flood_return_period: str | None = None
    elevation_m: float | None = None
    slope_degrees: float | None = None
    slope_risk: SlopeRisk | None = None
    nearest_shelter: str | None = None
    shelter_distance_m: float | None = None
    shelter_type: str | None = None
    road_access: RoadAccess | None = None
    nearest_road: str | None = None
    road_distance_m: float | None = None
    
    # AI reasoning and output
    reasoning: str | None = None
    warnings: list[str] | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)
    turkish_summary: str | None = None
    model_used: str = "gemma4:26b"
    inference_seconds: float | None = None
    
    # Field worker info
    worker_name: str | None = None
    worker_device: str | None = None
    field_note: str | None = None
    
    # Orthophoto batch info
    site_id: int | None = None
    site_name: str | None = None
    batch_id: str | None = None
    batch_building_count: int | None = None
    
    # Status and workflow
    status: AssessmentStatus = "pending"
    verified_by_ground: bool = False
    response_team: str | None = None
    response_notes: str | None = None
    
    # Timestamps
    created_at: datetime | None = None
    updated_at: datetime | None = None
    responded_at: datetime | None = None


class AssessmentCreate(BaseModel):
    """Request payload for creating new assessment."""

    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    input_type: InputType = "ground_photo"
    photo_path: str | None = None
    worker_name: str | None = None
    field_note: str | None = None


class AssessmentUpdate(BaseModel):
    """Request payload for updating existing assessment."""

    severity: int | None = Field(None, ge=1, le=5)
    damage_type: str | None = None
    damage_description: str | None = None
    status: AssessmentStatus | None = None
    response_team: str | None = None
    response_notes: str | None = None


class AssessmentListResponse(BaseModel):
    """Response payload for assessment list queries."""

    assessments: list[Assessment]
    total_count: int
    page: int
    page_size: int
