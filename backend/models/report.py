"""Report request/response models for markdown report generation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


ReportType = Literal["site", "building"]


class ReportGenerateRequest(BaseModel):
    """Request body for generating a report."""

    report_type: ReportType
    site_name: str | None = Field(default=None, max_length=200)
    site_id: int | None = None
    assessment_id: str | None = Field(default=None, max_length=50)
    team_name: str | None = Field(default=None, min_length=1, max_length=100)
    language: str = Field(default="en", min_length=2, max_length=10)
    created_by: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _validate_target(self) -> "ReportGenerateRequest":
        if self.report_type == "site" and not (self.site_name or self.site_id is not None):
            raise ValueError("site_name or site_id is required for site report")
        if self.report_type == "building" and not self.assessment_id:
            raise ValueError("assessment_id is required for building report")
        return self


class ReportListItem(BaseModel):
    """One reports table record returned to frontend."""

    id: str
    report_type: ReportType
    site_id: str | None = None
    assessment_id: str | None = None
    team_name: str | None = None
    language: str | None = None
    file_path: str | None = None
    status: str
    created_by: str | None = None
    created_at: datetime | None = None

