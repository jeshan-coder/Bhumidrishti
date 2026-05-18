// This file defines frontend helpers for assessments API operations and map GeoJSON conversion.

import type { FeatureCollection, Point } from "geojson"

// This type defines a single assessment row returned by list API for map usage.
export type AssessmentListItem = {
  id: string
  lat: number
  lon: number
  input_type: string
  photo_path: string | null
  severity: number | null
  damage_type: string | null
  structural_risk: string | null
  building_type: string | null
  recommended_action: string | null
  action_priority: number | null
  status: string
  created_at: string
  updated_at: string
}

// This function fetches assessment-matched building polygons as GeoJSON layer.
export async function fetchAssessmentBuildingLayer(limit = 500): Promise<{
  featureCount: number
  geojson: FeatureCollection
}> {
  const response = await fetch(`${API_BASE_URL}/assessments/building-layer?limit=${limit}`)

  if (!response.ok) {
    throw new Error(`Assessment building layer request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<AssessmentBuildingLayerPayload>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Invalid assessment building layer response")
  }

  return {
    featureCount: payload.data.feature_count,
    geojson: payload.data.geojson,
  }
}

// This type defines generic backend envelope structure.
type BackendEnvelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

// This type defines payload returned by assessment building-layer endpoint.
type AssessmentBuildingLayerPayload = {
  feature_count: number
  geojson: FeatureCollection
}

// This variable stores frontend API base URL with environment overrides.
const API_BASE_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

// This function fetches latest assessments list for dashboard/map consumers.
export async function fetchAssessments(limit = 500): Promise<AssessmentListItem[]> {
  const response = await fetch(`${API_BASE_URL}/assessments?limit=${limit}`)

  if (!response.ok) {
    throw new Error(`Assessments request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<AssessmentListItem[]>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Invalid assessments response")
  }

  return payload.data
}

// This function converts assessments list to GeoJSON point collection for MapLibre source.
export function buildAssessmentsGeoJson(
  assessments: AssessmentListItem[]
): FeatureCollection<Point, AssessmentListItem> {
  // This variable stores point features generated from assessment coordinates.
  const features = assessments
    .filter((item) => Number.isFinite(item.lat) && Number.isFinite(item.lon))
    .map((item) => ({
      type: "Feature" as const,
      id: item.id,
      geometry: {
        type: "Point" as const,
        coordinates: [item.lon, item.lat],
      },
      properties: item,
    }))

  return {
    type: "FeatureCollection",
    features,
  }
}
