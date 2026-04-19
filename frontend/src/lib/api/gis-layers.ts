// This file defines API utilities for GIS map overlay layers.

import type { FeatureCollection, Geometry, GeoJsonProperties } from "geojson"

export type GisLayerKey =
  | "turkey_provinces"
  | "turkey_points"
  | "turkey_lines"
  | "turkey_districts_pts"
  | "turkey_buildings"
  | "flood_zones"
  | "destroyed_buildings"
  | "dem"

// This type defines the GeoJSON FeatureCollection shape used by map overlays.
export type GeoJsonFeatureCollection = FeatureCollection<Geometry, GeoJsonProperties>

// This type defines backend response envelope.
type BackendEnvelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

// This type defines layer payload returned by backend.
type GisLayerPayload = {
  layer: string
  table: string
  max_features: number
  feature_count: number
  geojson: GeoJsonFeatureCollection
}

// This variable defines API base URL for browser-side requests.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

// This function fetches one GIS layer as GeoJSON from backend.
export async function fetchGisLayer(
  layerName: GisLayerKey,
  maxFeatures = 100000
): Promise<{ featureCount: number; geojson: GeoJsonFeatureCollection }> {
  const response = await fetch(`${API_BASE_URL}/gis/layers/${layerName}?max_features=${maxFeatures}`, {
    method: "GET",
    cache: "no-store",
  })

  if (!response.ok) {
    throw new Error(`GIS layer API failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<GisLayerPayload>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? `Failed to load layer ${layerName}`)
  }

  return {
    featureCount: payload.data.feature_count,
    geojson: payload.data.geojson,
  }
}
