"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import maplibregl from "maplibre-gl"
import { PMTiles, Protocol } from "pmtiles"
import { Map, MapPin, Route, MapPinned, Building, Droplets, AlertTriangle, Mountain, SatelliteDish, ImageIcon, X, Loader2, ChevronRight, LayoutList } from "lucide-react"
import { toast } from "sonner"
import { FieldMapChatSidebar } from "@/components/maps/field-map-chat-sidebar"
import { FeatureInfoSidebar, type SelectedFeatureInfo } from "@/components/maps/feature-info-sidebar"
import { MapDrawControls } from "@/components/maps/map-draw-controls"
import { PendingBatchCards } from "@/components/maps/pending-batch-cards"
import { fetchGisLayer, type GeoJsonFeatureCollection, type GisLayerKey } from "@/lib/api/gis-layers"
import { fetchAssessmentBuildingLayer } from "@/lib/api/assessments"
import { fetchPostEarthquakeLayer } from "@/lib/api/uploads"
import {
  analyzeSingleBuilding,
  analyzeExistingBatch,
  cancelBatch,
  fetchBatchBuildings,
  getBatchStatus,
  fetchPendingBatches,
  fetchBatchSites,
  fetchSitesFull,
  fetchSiteBuildings,
  fetchUnassignedUploads,
  startOrthophotoBatch,
  subscribeBatchStream,
  type PendingBatchRecord,
  type BatchSseEvent,
  type SiteRecord,
  type SiteBuildingsResult,
  type UnassignedUpload,
} from "@/lib/api/batches"

// This variable holds the shared PMTiles protocol instance for MapLibre.
const protocol = new Protocol()

// This variable prevents duplicate protocol registration in the browser runtime.
let protocolAdded = false

// This type defines the PMTiles metadata subset needed for style generation.
type PmtilesMetadata = {
  vector_layers?: Array<{ id?: string }>
}

// This type defines one GIS overlay layer configuration.
type OverlayLayerConfig = {
  key: GisLayerKey
  label: string
  color: string
  icon: React.ComponentType<{ className?: string; size?: number | string; color?: string }>
  isDEM?: boolean
}

// This type defines local state for each GIS overlay.
type OverlayLayerState = {
  visible: boolean
  isLoading: boolean
  featureCount: number
  error: string | null
}

// This function maps severity levels to UI color classes.
function getSeverityClasses(severity: number | undefined): { dot: string; badge: string; text: string } {
  if (severity === 5) {
    return { dot: "bg-[#A32D2D]", badge: "bg-[#FDECEC] border-[#F5C2C2] text-[#7A1F1F]", text: "text-[#7A1F1F]" }
  }
  if (severity === 4) {
    return { dot: "bg-[#E24B4A]", badge: "bg-[#FFF1F1] border-[#F5CACA] text-[#A33635]", text: "text-[#A33635]" }
  }
  if (severity === 3) {
    return { dot: "bg-[#EF9F27]", badge: "bg-[#FFF8EC] border-[#F7DEB3] text-[#B6731C]", text: "text-[#B6731C]" }
  }
  if (severity === 2) {
    return { dot: "bg-[#97C459]", badge: "bg-[#F4FAEC] border-[#CFE6AE] text-[#6E8E3F]", text: "text-[#6E8E3F]" }
  }
  return { dot: "bg-[#639922]", badge: "bg-[#EEF7E6] border-[#C5DFAB] text-[#496F18]", text: "text-[#496F18]" }
}

// This function prefers streamed AI text fields over static stage labels.
function getLiveAiTextFromEvent(ev: BatchSseEvent, fallback: string): string {
  const maybeThinking = typeof ev.thinking_text === "string" ? ev.thinking_text.trim() : ""
  const maybeResponse = typeof ev.response_text === "string" ? ev.response_text.trim() : ""
  const maybeThought = typeof ev.thought === "string" ? ev.thought.trim() : ""
  if (maybeResponse) return maybeResponse.slice(-480)
  if (maybeThinking) return maybeThinking.slice(-480)
  if (maybeThought) return maybeThought
  return fallback
}

// This variable lists GIS overlays requested for field map controls.
const overlayLayerConfigs: OverlayLayerConfig[] = [
  { key: "turkey_provinces", label: "Provinces", color: "#10B981", icon: Map },
  { key: "turkey_points", label: "Facilities & Amenities", color: "#F59E0B", icon: MapPin },
  { key: "turkey_lines", label: "Roads & Waterways", color: "#84CC16", icon: Route },
  { key: "turkey_districts_pts", label: "Districts", color: "#A3E635", icon: MapPinned },
  { key: "turkey_buildings", label: "Buildings", color: "#0F6E56", icon: Building },
  { key: "flood_zones", label: "Flood Zones", color: "#3B82F6", icon: Droplets },
  { key: "assessments", label: "Assessments", color: "#A32D2D", icon: AlertTriangle },
  { key: "post_earthquake_images", label: "Post-Earthquake Images", color: "#F97316", icon: ImageIcon },
  { key: "dem", label: "DEM", color: "#8B4513", icon: Mountain, isDEM: true },
  { key: "satellite_pre", label: "Pre-Earthquake Imagery", color: "#6366F1", icon: SatelliteDish, isDEM: true },
]

// This variable builds default layer state map.
const defaultOverlayLayerState: Record<GisLayerKey, OverlayLayerState> = {
  turkey_provinces: { visible: false, isLoading: false, featureCount: 0, error: null },
  turkey_points: { visible: false, isLoading: false, featureCount: 0, error: null },
  turkey_lines: { visible: false, isLoading: false, featureCount: 0, error: null },
  turkey_districts_pts: { visible: false, isLoading: false, featureCount: 0, error: null },
  turkey_buildings: { visible: true, isLoading: false, featureCount: 0, error: null },
  flood_zones: { visible: false, isLoading: false, featureCount: 0, error: null },
  destroyed_buildings: { visible: false, isLoading: false, featureCount: 0, error: null },
  assessments: { visible: true, isLoading: false, featureCount: 0, error: null },
  post_earthquake_images: { visible: false, isLoading: false, featureCount: 0, error: null },
  dem: { visible: false, isLoading: false, featureCount: 0, error: null },
  satellite_pre: { visible: false, isLoading: false, featureCount: 0, error: null },
}

// This function returns MapLibre source ID for a given overlay key.
function getOverlaySourceId(layerKey: GisLayerKey): string {
  return `overlay-source-${layerKey}`
}

// This function returns MapLibre layer IDs for point, line, and fill visuals.
function getOverlayLayerIds(layerKey: GisLayerKey): { fill: string; line: string; point: string } {
  return {
    fill: `overlay-fill-${layerKey}`,
    line: `overlay-line-${layerKey}`,
    point: `overlay-point-${layerKey}`,
  }
}

// This function returns all existing map layer IDs for one overlay key in bottom-to-top order.
function getExistingMapLayerIdsForOverlay(map: maplibregl.Map, layerKey: GisLayerKey): string[] {
  if (layerKey === "dem" || layerKey === "satellite_pre") {
    const rasterLayerIds = [`${layerKey}-adiyaman-raster`, `${layerKey}-hatay-raster`]
    return rasterLayerIds.filter((layerId) => Boolean(map.getLayer(layerId)))
  }

  if (layerKey === "post_earthquake_images") {
    const styleLayers = map.getStyle()?.layers ?? []
    return styleLayers
      .map((layer) => layer.id)
      .filter((layerId) => layerId.startsWith("post-earthquake-") && layerId.endsWith("-raster"))
  }

  const overlayLayerIds = getOverlayLayerIds(layerKey)
  return [overlayLayerIds.fill, overlayLayerIds.line, overlayLayerIds.point].filter(
    (layerId) => Boolean(map.getLayer(layerId))
  )
}

// This function applies user-defined overlay ranking by moving layer groups to map top in sequence.
function applyOverlayRankingOrder(
  map: maplibregl.Map,
  rankedOverlayKeysTopFirst: GisLayerKey[]
): void {
  const bottomToTopOrder = [...rankedOverlayKeysTopFirst].reverse()
  for (const overlayKey of bottomToTopOrder) {
    const layerIds = getExistingMapLayerIdsForOverlay(map, overlayKey)
    for (const layerId of layerIds) {
      if (map.getLayer(layerId)) {
        map.moveLayer(layerId)
      }
    }
  }
}

// This function applies visibility to all map layers representing one overlay.
function applyOverlayVisibility(map: maplibregl.Map, layerKey: GisLayerKey, visible: boolean): void {
  const visibilityValue = visible ? "visible" : "none"

  // Handle DEM and satellite raster layers
  if (layerKey === "dem" || layerKey === "satellite_pre") {
    const regions = ["adiyaman", "hatay"]
    for (const region of regions) {
      const rasterLayerId = `${layerKey}-${region}-raster`
      if (map.getLayer(rasterLayerId)) {
        map.setLayoutProperty(rasterLayerId, "visibility", visibilityValue)
      }
    }
  } else if (layerKey === "post_earthquake_images") {
    const styleLayers = map.getStyle()?.layers ?? []
    for (const styleLayer of styleLayers) {
      if (styleLayer.id.startsWith("post-earthquake-") && styleLayer.id.endsWith("-raster")) {
        if (map.getLayer(styleLayer.id)) {
          map.setLayoutProperty(styleLayer.id, "visibility", visibilityValue)
        }
      }
    }
  } else {
    // Handle vector layers
    const layerIds = getOverlayLayerIds(layerKey)
    for (const layerId of [layerIds.fill, layerIds.line, layerIds.point]) {
      if (map.getLayer(layerId)) {
        map.setLayoutProperty(layerId, "visibility", visibilityValue)
      }
    }
  }
}

// Pending-site "Show in map" uses polygons from the buildings GeoJSON — line layers skip Polygon features, so this must be a fill layer.
const PENDING_BATCH_HIGHLIGHT_LAYER_ID = "pending-batch-buildings-highlight"

function pendingBatchHighlightInactiveFilter(): maplibregl.FilterSpecification {
  return [
    "all",
    ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
    ["==", 1, 0],
  ] as unknown as maplibregl.FilterSpecification
}

function pendingBatchPolygonOsmHighlightFilter(osmIds: number[]): maplibregl.FilterSpecification {
  return [
    "all",
    ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
    ["in", ["to-number", ["get", "osm_id"]], ["literal", osmIds]],
  ] as unknown as maplibregl.FilterSpecification
}

/** Ensures highlight layer exists and sits above footprint fills (below draw tools). */
function raisePendingBatchHighlightLayer(map: maplibregl.Map): void {
  if (!map.getLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID)) return
  const beforeDraw = map.getLayer("draw-polygon-fill") ? "draw-polygon-fill" : undefined
  if (beforeDraw) {
    map.moveLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID, beforeDraw)
  } else {
    map.moveLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID)
  }
}

function ensurePendingBatchHighlightLayer(map: maplibregl.Map): boolean {
  const sourceId = getOverlaySourceId("turkey_buildings")
  if (!map.getSource(sourceId)) return false

  const styleLayers = map.getStyle()?.layers ?? []
  const desc = styleLayers.find((layer) => layer.id === PENDING_BATCH_HIGHLIGHT_LAYER_ID)
  if (desc && "type" in desc && desc.type === "line") {
    map.removeLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID)
  }

  if (!map.getLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID)) {
    map.addLayer({
      id: PENDING_BATCH_HIGHLIGHT_LAYER_ID,
      type: "fill",
      source: sourceId,
      filter: pendingBatchHighlightInactiveFilter(),
      layout: { visibility: "visible" },
      paint: {
        "fill-color": "#2563EB",
        "fill-opacity": 0.45,
        "fill-outline-color": "#1E40AF",
      },
    })
  }
  return true
}

// This function adds one GeoJSON overlay source and style layers to map.
function addOverlayToMap(
  map: maplibregl.Map,
  config: OverlayLayerConfig,
  geojson: GeoJsonFeatureCollection,
  visible: boolean
): void {
  const sourceId = getOverlaySourceId(config.key)
  const layerIds = getOverlayLayerIds(config.key)
  const visibilityValue = visible ? "visible" : "none"

  if (!map.getSource(sourceId)) {
    map.addSource(sourceId, {
      type: "geojson",
      data: geojson,
      promoteId: "id",
    })
  }

  if (!map.getLayer(layerIds.fill)) {
    map.addLayer({
      id: layerIds.fill,
      type: "fill",
      source: sourceId,
      filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
      layout: { visibility: visibilityValue },
      paint: {
        "fill-color": config.key === "assessments"
          ? [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              "#F59E0B",
              ["==", ["get", "severity"], 5],
              "#A32D2D",
              ["==", ["get", "severity"], 4],
              "#E24B4A",
              ["==", ["get", "severity"], 3],
              "#EF9F27",
              ["==", ["get", "severity"], 2],
              "#97C459",
              "#639922",
            ]
          : [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              "#F59E0B",
              config.color,
            ],
        "fill-opacity": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          0.6,
          config.key === "assessments" ? 0.42 : 0.25,
        ],
      },
    })
  }

  if (!map.getLayer(layerIds.line)) {
    map.addLayer({
      id: layerIds.line,
      type: "line",
      source: sourceId,
      filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]],
      layout: { visibility: visibilityValue },
      paint: {
        "line-color": config.key === "assessments"
          ? [
              "case",
              ["==", ["get", "severity"], 5],
              "#7A1F1F",
              ["==", ["get", "severity"], 4],
              "#A33635",
              ["==", ["get", "severity"], 3],
              "#B6731C",
              ["==", ["get", "severity"], 2],
              "#6E8E3F",
              "#496F18",
            ]
          : config.color,
        "line-width": config.key === "assessments" ? 1.8 : 1.2,
        "line-opacity": 0.9,
      },
    })
  }

  if (!map.getLayer(layerIds.point)) {
    map.addLayer({
      id: layerIds.point,
      type: "circle",
      source: sourceId,
      filter: ["in", ["geometry-type"], ["literal", ["Point", "MultiPoint"]]],
      layout: { visibility: visibilityValue },
      paint: {
        "circle-color": config.key === "assessments"
          ? [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              "#F59E0B",
              ["==", ["get", "severity"], 5],
              "#A32D2D",
              ["==", ["get", "severity"], 4],
              "#E24B4A",
              ["==", ["get", "severity"], 3],
              "#EF9F27",
              ["==", ["get", "severity"], 2],
              "#97C459",
              "#639922",
            ]
          : [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              "#F59E0B",
              config.color,
            ],
        "circle-radius": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          config.key === "assessments" ? 8 : 5,
          config.key === "assessments" ? 5 : 3,
        ],
        "circle-opacity": 0.92,
        "circle-stroke-color": "#0B0F0D",
        "circle-stroke-width": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          2,
          0.5,
        ],
      },
    })
  }
}

function collectLngLatPairsFromGeoJsonGeometry(
  geometry: { type: string; coordinates: unknown } | null | undefined
): Array<[number, number]> {
  if (!geometry) return []
  const coordinates = (geometry as { coordinates?: unknown }).coordinates
  if (!Array.isArray(coordinates)) return []

  const pairs: Array<[number, number]> = []
  const walk = (value: unknown) => {
    if (!Array.isArray(value)) return
    if (value.length >= 2 && typeof value[0] === "number" && typeof value[1] === "number") {
      pairs.push([value[0], value[1]])
      return
    }
    for (const child of value) walk(child)
  }
  walk(coordinates)
  return pairs
}

const AI_CHAT_RESULT_SOURCE_ID = "ai-chat-result-source"
const AI_CHAT_RESULT_FILL_LAYER_ID = "ai-chat-result-fill"
const AI_CHAT_RESULT_LINE_LAYER_ID = "ai-chat-result-line"
const AI_CHAT_RESULT_POINT_LAYER_ID = "ai-chat-result-point"

// [fill, line/point, outline/stroke] — one deep distinct color per tool
const TOOL_OVERLAY_COLORS: Record<string, [string, string, string]> = {
  get_flood_zone:              ["#1E40AF", "#1E40AF", "#1e3a8a"],  // deep blue
  get_nearest_shelter:         ["#7C3AED", "#7C3AED", "#5b21b6"],  // deep purple (route)
  get_building_info:           ["#15803D", "#15803D", "#14532d"],  // deep green
  get_assessments:             ["#DC2626", "#DC2626", "#991b1b"],  // deep red
  get_sites:                   ["#0E7490", "#0E7490", "#164e63"],  // deep cyan
  get_location_info_province:  ["#1D4ED8", "#1D4ED8", "#1e3a8a"],  // blue
  get_location_info_district:  ["#6D28D9", "#6D28D9", "#4c1d95"],  // violet
  get_location_info_point:     ["#0369A1", "#0369A1", "#0c4a6e"],  // sky blue
  get_nearest_road:            ["#D97706", "#D97706", "#92400e"],  // deep amber
  get_centroid:                ["#BE185D", "#BE185D", "#9d174d"],  // deep pink
}
const DEFAULT_OVERLAY_COLORS: [string, string, string] = ["#F59E0B", "#EA580C", "#B45309"]

function createEmptyFeatureCollection(): GeoJsonFeatureCollection {
  return { type: "FeatureCollection", features: [] } as GeoJsonFeatureCollection
}

function isGeometryCandidate(value: unknown): value is { type: string; coordinates: unknown } {
  if (!value || typeof value !== "object") return false
  const geometry = value as { type?: unknown; coordinates?: unknown }
  return typeof geometry.type === "string" && geometry.coordinates !== undefined
}

function ensureAiChatResultLayers(map: maplibregl.Map): void {
  if (!map.getSource(AI_CHAT_RESULT_SOURCE_ID)) {
    map.addSource(AI_CHAT_RESULT_SOURCE_ID, {
      type: "geojson",
      data: createEmptyFeatureCollection(),
    })
  }

  // Data-driven color expressions — each feature carries its own _fill_color / _line_color / _stroke_color
  const fillColorExpr = ["coalesce", ["get", "_fill_color"], DEFAULT_OVERLAY_COLORS[0]] as unknown as string
  const lineColorExpr = ["coalesce", ["get", "_line_color"], DEFAULT_OVERLAY_COLORS[1]] as unknown as string
  const strokeColorExpr = ["coalesce", ["get", "_stroke_color"], DEFAULT_OVERLAY_COLORS[2]] as unknown as string

  if (!map.getLayer(AI_CHAT_RESULT_FILL_LAYER_ID)) {
    map.addLayer({
      id: AI_CHAT_RESULT_FILL_LAYER_ID,
      type: "fill",
      source: AI_CHAT_RESULT_SOURCE_ID,
      filter: ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]],
      paint: {
        "fill-color": fillColorExpr,
        "fill-opacity": 0.22,
        "fill-outline-color": strokeColorExpr,
      },
    })
  } else {
    map.setPaintProperty(AI_CHAT_RESULT_FILL_LAYER_ID, "fill-color", fillColorExpr)
    map.setPaintProperty(AI_CHAT_RESULT_FILL_LAYER_ID, "fill-outline-color", strokeColorExpr)
  }

  if (!map.getLayer(AI_CHAT_RESULT_LINE_LAYER_ID)) {
    map.addLayer({
      id: AI_CHAT_RESULT_LINE_LAYER_ID,
      type: "line",
      source: AI_CHAT_RESULT_SOURCE_ID,
      filter: ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]],
      paint: {
        "line-color": lineColorExpr,
        "line-width": 4,
        "line-opacity": 0.95,
      },
    })
  } else {
    map.setPaintProperty(AI_CHAT_RESULT_LINE_LAYER_ID, "line-color", lineColorExpr)
  }

  if (!map.getLayer(AI_CHAT_RESULT_POINT_LAYER_ID)) {
    map.addLayer({
      id: AI_CHAT_RESULT_POINT_LAYER_ID,
      type: "circle",
      source: AI_CHAT_RESULT_SOURCE_ID,
      filter: ["in", ["geometry-type"], ["literal", ["Point", "MultiPoint"]]],
      paint: {
        "circle-color": fillColorExpr,
        "circle-radius": 6,
        "circle-stroke-color": strokeColorExpr,
        "circle-stroke-width": 2,
      },
    })
  } else {
    map.setPaintProperty(AI_CHAT_RESULT_POINT_LAYER_ID, "circle-color", fillColorExpr)
    map.setPaintProperty(AI_CHAT_RESULT_POINT_LAYER_ID, "circle-stroke-color", strokeColorExpr)
  }
}

type ChatToolGeometryExtraction = {
  overlayName: string
  features: GeoJsonFeatureCollection["features"]
}

function getOverlayNameFromToolResult(toolName: string, result: Record<string, unknown>): string {
  if (toolName === "get_sites" && Array.isArray(result.items)) {
    const first = result.items[0]
    if (first && typeof first === "object") {
      const firstRow = first as Record<string, unknown>
      const firstName = firstRow.name
      if (typeof firstName === "string" && firstName.trim().length > 0) {
        return `Site: ${firstName}`
      }
    }
    return `Sites (${result.items.length})`
  }

  if (toolName === "get_assessments" && Array.isArray(result.items)) {
    const first = result.items[0]
    if (first && typeof first === "object") {
      const firstRow = first as Record<string, unknown>
      const firstId = firstRow.id
      if (typeof firstId === "string" && firstId.trim().length > 0) {
        return `Assessment: ${firstId}`
      }
    }
    return `Assessments (${result.items.length})`
  }

  if (toolName === "get_flood_zone") {
    const floodZoneData = result.flood_zone_data
    if (floodZoneData && typeof floodZoneData === "object") {
      const flood = floodZoneData as Record<string, unknown>
      const waterwayName = flood.waterway_name
      if (typeof waterwayName === "string" && waterwayName.trim().length > 0) {
        return `Flood zone: ${waterwayName}`
      }
    }
    return "Flood zone"
  }

  if (toolName === "get_building_info") {
    const buildingData = result.building_data
    if (buildingData && typeof buildingData === "object") {
      const building = buildingData as Record<string, unknown>
      const osmId = building.osm_id
      if (typeof osmId === "number" || typeof osmId === "string") {
        return `Building: ${String(osmId)}`
      }
    }
    return "Building geometry"
  }

  if (toolName === "get_location_info") {
    const province = result.province
    const district = result.district
    if (typeof province === "string" && province.trim().length > 0) {
      if (typeof district === "string" && district.trim().length > 0) {
        return `Location: ${district}, ${province}`
      }
      return `Location: ${province}`
    }
    return "Location context"
  }

  if (toolName === "get_nearest_shelter") {
    const routeName = result.route_name
    if (typeof routeName === "string" && routeName.trim().length > 0) {
      return routeName
    }
    const shelterName = result.name
    if (typeof shelterName === "string" && shelterName.trim().length > 0) {
      return `Route to shelter: ${shelterName}`
    }
    return "Route to nearest shelter"
  }

  return toolName.replaceAll("_", " ")
}

function extractFeaturesFromChatToolResult(
  toolName: string,
  result: Record<string, unknown>
): ChatToolGeometryExtraction {
  const overlayName = getOverlayNameFromToolResult(toolName, result)
  const features: GeoJsonFeatureCollection["features"] = []

  const pushFeature = (
    geometryCandidate: unknown,
    properties: Record<string, unknown>,
    idSuffix: string,
    colorKey?: string,
  ) => {
    if (!isGeometryCandidate(geometryCandidate)) return
    const [fc, lc, sc] = TOOL_OVERLAY_COLORS[colorKey ?? toolName] ?? DEFAULT_OVERLAY_COLORS
    features.push({
      type: "Feature",
      id: `${toolName}-${idSuffix}`,
      geometry: geometryCandidate as never,
      properties: { tool_name: toolName, _fill_color: fc, _line_color: lc, _stroke_color: sc, ...properties },
    })
  }

  if (toolName === "get_sites" && Array.isArray(result.items)) {
    result.items.forEach((item, index) => {
      if (!item || typeof item !== "object") return
      const row = item as Record<string, unknown>
      pushFeature(row.boundary_geojson, {
        label: row.name ?? "Site boundary",
        site_name: row.name,
        building_count: row.building_count,
        area_m2: row.area_m2,
      }, `site-${index}`)
    })
    return { overlayName, features }
  }

  if (toolName === "get_assessments" && Array.isArray(result.items)) {
    result.items.forEach((item, index) => {
      if (!item || typeof item !== "object") return
      const row = item as Record<string, unknown>
      pushFeature(row.geom_geojson, {
        label: row.id ?? "Assessment",
        assessment_id: row.id,
        severity: row.severity,
        damage_type: row.damage_type,
        status: row.status,
        recommended_action: row.recommended_action,
        structural_risk: row.structural_risk,
      }, `assessment-${index}`)
    })
    return { overlayName, features }
  }

  if (toolName === "get_flood_zone") {
    const floodZoneData = result.flood_zone_data
    if (floodZoneData && typeof floodZoneData === "object") {
      const flood = floodZoneData as Record<string, unknown>
      pushFeature(flood.geom_geojson, {
        label: flood.waterway_name ?? "Flood zone",
        in_flood_zone: result.in_flood_zone,
        waterway_name: flood.waterway_name,
        distance_to_waterway_m: result.distance_to_waterway_m,
        return_period: result.return_period,
      }, "flood-zone")
    }
    return { overlayName, features }
  }

  if (toolName === "get_building_info") {
    const buildingData = result.building_data
    if (buildingData && typeof buildingData === "object") {
      const building = buildingData as Record<string, unknown>
      pushFeature(building.geom_geojson, {
        label: building.osm_id ?? "Building",
        osm_id: building.osm_id,
        building_type: building.building,
        floors: building.building_levels,
        material: building.building_material,
        roof_type: building.roof_shape,
      }, "building")
    }
    return { overlayName, features }
  }

  if (toolName === "get_location_info") {
    const provinceData = result.province_data
    const districtData = result.district_data
    const nearestPointData = result.nearest_point_data
    if (provinceData && typeof provinceData === "object") {
      const province = provinceData as Record<string, unknown>
      pushFeature(province.geom_geojson, {
        label: province.name_en ?? province.name_tr ?? "Province",
        province: province.name_en ?? province.name_tr,
      }, "province", "get_location_info_province")
    }
    if (districtData && typeof districtData === "object") {
      const district = districtData as Record<string, unknown>
      pushFeature(district.geom_geojson, {
        label: district.district ?? "District",
        district: district.district,
      }, "district", "get_location_info_district")
    }
    if (nearestPointData && typeof nearestPointData === "object") {
      const point = nearestPointData as Record<string, unknown>
      pushFeature(point.geom_geojson, {
        label: point.name ?? "Nearest point",
        name: point.name,
        amenity: point.amenity,
      }, "nearest-point", "get_location_info_point")
    }
    return { overlayName, features }
  }

  if (toolName === "get_nearest_shelter") {
    pushFeature(result.route_geometry_geojson, {
      label: result.route_name ?? "Route to shelter",
      shelter_name: result.name,
      shelter_type: result.shelter_type,
      shelter_description: result.shelter_description,
      distance_m: result.distance_m,
      route_distance_m: result.route_distance_m,
      route_duration_s: result.route_duration_s,
      nearest_road: result.nearest_road,
    }, "shelter-route")
    return { overlayName, features }
  }

  return { overlayName, features }
}

// ── AI overlay info panel ──────────────────────────────────────────────────

const AI_OVERLAY_TOOL_ICONS: Record<string, string> = {
  get_flood_zone: "🌊", get_nearest_shelter: "🏥", get_building_info: "🏢",
  get_assessments: "📋", get_sites: "🗺️", get_location_info: "📍",
  get_nearest_road: "🛣️", get_centroid: "📌", get_elevation_slope: "⛰️",
}
const AI_OVERLAY_TOOL_LABELS: Record<string, string> = {
  get_flood_zone: "Flood Zone", get_nearest_shelter: "Shelter Route",
  get_building_info: "Building", get_assessments: "Assessment",
  get_sites: "Site", get_location_info: "Location",
  get_nearest_road: "Nearest Road", get_centroid: "Centroid", get_elevation_slope: "Elevation",
}

function formatAiPropValue(key: string, value: unknown): string {
  if (value == null || value === "") return ""
  if (typeof value === "boolean") return value ? "Yes" : "No"
  if (typeof value === "number") {
    if (key.includes("distance_m") || key === "distance_m") return value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${Math.round(value)} m`
    if (key.includes("route_distance")) return value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${Math.round(value)} m`
    if (key.includes("duration_s") || key.includes("_s")) return value >= 60 ? `${Math.round(value / 60)} min` : `${Math.round(value)} s`
    if (key.includes("area_m2")) return `${(value / 10000).toFixed(2)} ha`
    return String(Math.round(value * 100) / 100)
  }
  return String(value)
}

function AiOverlayInfoPanel({
  info,
  onClose,
}: {
  info: { properties: Record<string, unknown>; lat: number; lon: number } | null
  onClose: () => void
}) {
  if (!info) return null
  const { properties, lat, lon } = info
  const toolName = String(properties.tool_name ?? "")
  const label    = String(properties.label ?? "Feature")
  const color    = String(properties._line_color ?? properties._fill_color ?? "#6b7280")
  const icon     = AI_OVERLAY_TOOL_ICONS[toolName] ?? "🔧"
  const toolLabel = AI_OVERLAY_TOOL_LABELS[toolName] ?? toolName.replace(/_/g, " ")

  const SKIP_KEYS = new Set(["tool_name", "label", "_fill_color", "_line_color", "_stroke_color", "overlay_id", "overlay_name"])
  const displayEntries = Object.entries(properties).filter(
    ([k, v]) => !SKIP_KEYS.has(k) && v != null && v !== "" && String(v).trim() !== ""
  )

  return (
    <div className="absolute right-4 top-16 z-30 w-72 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-xl">
      {/* Header */}
      <div
        className="flex items-center gap-2 px-3 py-2.5"
        style={{ backgroundColor: `${color}18`, borderBottom: `2px solid ${color}` }}
      >
        <span className="shrink-0 text-base">{icon}</span>
        <div className="min-w-0 flex-1">
          <p className="text-xs font-semibold text-gray-900">{toolLabel}</p>
          <p className="truncate text-[10px] text-gray-500">{label}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded p-1 text-gray-400 hover:bg-black/10"
        >
          <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
            <path d="M1 1l10 10M11 1L1 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
          </svg>
        </button>
      </div>

      {/* Properties */}
      <div className="max-h-80 overflow-y-auto divide-y divide-gray-50 px-3 py-2">
        {displayEntries.map(([key, value]) => {
          const formatted = formatAiPropValue(key, value)
          if (!formatted) return null
          return (
            <div key={key} className="flex items-start justify-between gap-2 py-1">
              <span className="shrink-0 capitalize text-[10px] font-medium text-gray-400">
                {key.replace(/_/g, " ")}
              </span>
              <span className="text-right text-[11px] text-gray-800 break-all">{formatted}</span>
            </div>
          )
        })}
        <div className="flex items-start justify-between gap-2 py-1">
          <span className="shrink-0 text-[10px] font-medium text-gray-400">Location</span>
          <span className="text-right font-mono text-[10px] text-gray-600">
            {lat.toFixed(5)}, {lon.toFixed(5)}
          </span>
        </div>
      </div>
    </div>
  )
}

// This component renders the PMTiles basemap with a single-button AI chat sidebar.
export function FieldMapView() {
  type ChatGeometryOverlay = {
    id: string
    name: string
    geojson: GeoJsonFeatureCollection
    visible: boolean
  }

  type SelectedBuildingChatContext = {
    label: string
    geometry: unknown
  }

  // This variable references the map container element for MapLibre mount.
  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapInstanceRef = useRef<maplibregl.Map | null>(null)
  const overlayLoadedRef = useRef<Set<GisLayerKey>>(new Set())
  // This variable caches loaded overlay GeoJSON so feature lookup can power custom map actions.
  const overlayGeoJsonRef = useRef<Partial<Record<GisLayerKey, GeoJsonFeatureCollection>>>({})
  // This variable stores one dynamic map layer driven by AI chat tool-result geometry.
  const aiChatResultGeoJsonRef = useRef<GeoJsonFeatureCollection>(createEmptyFeatureCollection())
  const chatGeometryOverlaysRef = useRef<ChatGeometryOverlay[]>([])
  const [chatGeometryOverlays, setChatGeometryOverlays] = useState<Array<{ id: string; name: string; visible: boolean }>>([])
  // This variable stores a map-bound helper used to focus one assessment and open its popup.
  const showAssessmentOnMapRef = useRef<((assessmentId: string, lat: number, lon: number) => void) | null>(null)

  // This variable tracks overlay visibility/loading/count per GIS layer.
  const [overlayStates, setOverlayStates] = useState<Record<GisLayerKey, OverlayLayerState>>(
    defaultOverlayLayerState
  )
  // This variable tracks overlay ranking order where index 0 is drawn on top.
  const [overlayOrder, setOverlayOrder] = useState<GisLayerKey[]>(
    overlayLayerConfigs.map((config) => config.key)
  )

  // This variable tracks chat sidebar open state for responsive layout.
  const [isChatSidebarOpen, setIsChatSidebarOpen] = useState(false)
  // This variable stores hidden selected-building context for Gemma4 chat.
  const [selectedBuildingChatContext, setSelectedBuildingChatContext] = useState<SelectedBuildingChatContext | null>(null)

  // This variable tracks currently selected feature for highlighting.
  const selectedFeatureRef = useRef<{ source: string; id: string | number } | null>(null)

  // This variable stores the feature info shown in the right-hand details sidebar.
  const [selectedFeatureInfo, setSelectedFeatureInfo] = useState<SelectedFeatureInfo | null>(null)

  // AI overlay click info panel
  const [selectedAiOverlay, setSelectedAiOverlay] = useState<{
    properties: Record<string, unknown>
    lat: number
    lon: number
  } | null>(null)
  const aiClickHandlerAddedRef = useRef(false)

  // ── Draw polygon for batch analysis ──────────────────────────────────────
  const [drawMode, setDrawMode] = useState(false)
  const drawPointsRef = useRef<[number, number][]>([])
  const [drawPoints, setDrawPoints] = useState<[number, number][]>([])
  const drawModeRef = useRef(false)

  // ── Batch modal state ─────────────────────────────────────────────────────
  const [batchModalOpen, setBatchModalOpen] = useState(false)
  const [batchWorkerName, setBatchWorkerName] = useState("")
  const [batchUploadId, setBatchUploadId] = useState("")
  const [batchSubmitting, setBatchSubmitting] = useState(false)

  // ── Site picker (shared between batch + single-building flows) ────────────
  const [existingSites, setExistingSites] = useState<string[]>([])
  const [sitesLoading, setSitesLoading] = useState(false)
  /** The currently selected existing site, or "" meaning "use new site" */
  const [selectedExistingSite, setSelectedExistingSite] = useState<string>("")
  const [newSiteNameInput, setNewSiteNameInput] = useState("")
  const [useNewSite, setUseNewSite] = useState(false)
  /** Site picker shown for single-building flow (separate from batch modal) */
  const [singleBldgPickerOpen, setSingleBldgPickerOpen] = useState(false)
  const pendingBldgRef = useRef<{ osmId: number; lat: number; lon: number } | null>(null)

  // ── Batch progress state ──────────────────────────────────────────────────
  type BuildingEvent = { osm_id: number; status: string; assessment_id?: string; severity?: number; error?: string; chip_path?: string; pre_chip_path?: string }
  type AiStageEvent = { osm_id: number; stage: string; thought: string }
  type BuildingLiveProgress = { osm_id: number; progressPercent: number; stage: string; thought: string; status: string; updatedAt: number; totalTokens?: number; contextWindow?: number; thinkingText?: string; responseText?: string }
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null)
  const [batchTokensUsed, setBatchTokensUsed] = useState<number>(0)
  const [batchProgress, setBatchProgress] = useState<{
    total: number; processed: number; failed: number; skipped: number; events: BuildingEvent[]
  } | null>(null)
  const [batchDone, setBatchDone] = useState(false)
  const [currentAiStage, setCurrentAiStage] = useState<AiStageEvent | null>(null)
  const [batchBuildingProgress, setBatchBuildingProgress] = useState<Record<number, BuildingLiveProgress>>({})
  const [currentThinkingFull, setCurrentThinkingFull] = useState<string>("")
  const [currentResponseFull, setCurrentResponseFull] = useState<string>("")
  const [aiThinkingExpanded, setAiThinkingExpanded] = useState(false)
  const [processingOsmIds, setProcessingOsmIds] = useState<number[]>([])
  const [processingBlinkOn, setProcessingBlinkOn] = useState(false)
  const [batchWasStopped, setBatchWasStopped] = useState(false)
  const [activeBatchSiteName, setActiveBatchSiteName] = useState<string>("")
  const [sitesFullList, setSitesFullList] = useState<SiteRecord[]>([])
  const [siteBuildingsPanel, setSiteBuildingsPanel] = useState<SiteBuildingsResult | null>(null)
  const [siteBuildingsPanelLoading, setSiteBuildingsPanelLoading] = useState(false)
  // Unassigned uploads notification.
  const [unassignedUploads, setUnassignedUploads] = useState<UnassignedUpload[]>([])
  const [unassignedUploadsLoading, setUnassignedUploadsLoading] = useState(false)
  const [showUnassignedOnMap, setShowUnassignedOnMap] = useState(false)
  const [unassignedNotifDismissed, setUnassignedNotifDismissed] = useState(false)
  const unassignedPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const [pendingBatches, setPendingBatches] = useState<PendingBatchRecord[]>([])
  const [dismissedPendingBatchIds, setDismissedPendingBatchIds] = useState<string[]>([])
  const [selectedPendingBatchId, setSelectedPendingBatchId] = useState<string | null>(null)
  const batchStreamCleanupRef = useRef<(() => void) | null>(null)
  const singleBuildingStreamCleanupRef = useRef<(() => void) | null>(null)
  const pendingHighlightTimeoutRef = useRef<number | null>(null)
  const assessmentsRefreshTimeoutRef = useRef<number | null>(null)
  const assessmentsRefreshInFlightRef = useRef(false)

  // ── Single building analysis drawer ──────────────────────────────────────
  const [buildingDrawer, setBuildingDrawer] = useState<{
    osmId: number; lat: number; lon: number; batchId: string | null
    events: BuildingEvent[]; done: boolean; aiThought?: string; aiStage?: string
    progressPercent: number; progressLabel?: string; tokensUsed: number
  } | null>(null)
  const [singleWasStopped, setSingleWasStopped] = useState(false)
  // One card per site name: `/batch/pending` can return several rows per label (retry runs share `site_name`).
  const visiblePendingBatches = (() => {
    const filtered = pendingBatches
      .filter((batch) => !dismissedPendingBatchIds.includes(batch.batch_id))
      .filter((batch) => (batch.status || "").toLowerCase() !== "complete")
      .filter((batch) => {
        const total = Math.max(0, Number(batch.total_buildings) || 0)
        const doneCount = Math.max(0, Number(batch.processed) || 0)
        const skippedCount = Math.max(0, Number(batch.skipped) || 0)
        const remaining = Math.max(
          0,
          batch.remaining_buildings == null
            ? total - doneCount - skippedCount
            : Number(batch.remaining_buildings)
        )
        return remaining > 0
      })
    const siteKey = (b: PendingBatchRecord) => {
      const name = (b.site_name ?? "").trim()
      return name.length > 0 ? name.toLowerCase() : `__id__:${b.batch_id}`
    }
    const ranked = [...filtered].sort((a, b) => {
      const actDiff = Number(!!b.is_active_task) - Number(!!a.is_active_task)
      if (actDiff !== 0) return actDiff
      const ta = a.created_at ? Date.parse(a.created_at) : 0
      const tb = b.created_at ? Date.parse(b.created_at) : 0
      return tb - ta
    })
    const winnerBySite: Record<string, PendingBatchRecord> = {}
    for (const batch of ranked) {
      const key = siteKey(batch)
      if (winnerBySite[key] === undefined) winnerBySite[key] = batch
    }
    const winnerIds = new Set(Object.values(winnerBySite).map((batch) => batch.batch_id))
    return filtered
      .filter((b) => winnerIds.has(b.batch_id))
      .sort((a, b) => {
        const ta = a.created_at ? Date.parse(a.created_at) : 0
        const tb = b.created_at ? Date.parse(b.created_at) : 0
        return tb - ta
      })
  })()

  // This function cancels currently running polygon batch analysis.
  const handleStopBatchAnalysis = useCallback(async () => {
    if (!activeBatchId) return
    try {
      await cancelBatch(activeBatchId)
      toast.success("Stopping batch analysis...")
      setBatchWasStopped(true)
      setCurrentAiStage(null)
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to stop batch analysis"
      toast.error(message)
    }
  }, [activeBatchId])

  const refreshAssessmentsLayer = useCallback(async () => {
    const map = mapInstanceRef.current
    if (!map || assessmentsRefreshInFlightRef.current) return
    assessmentsRefreshInFlightRef.current = true
    try {
      const layerResult = await fetchAssessmentBuildingLayer(2000)
      overlayGeoJsonRef.current.assessments = layerResult.geojson as GeoJsonFeatureCollection
      const source = map.getSource(getOverlaySourceId("assessments")) as maplibregl.GeoJSONSource | undefined
      if (source) {
        source.setData(layerResult.geojson as never)
      }
      applyOverlayVisibility(map, "assessments", true)
      setOverlayStates((currentStates) => ({
        ...currentStates,
        assessments: {
          ...currentStates.assessments,
          visible: true,
          featureCount: layerResult.featureCount,
          error: null,
        },
      }))
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to refresh assessments layer"
      setOverlayStates((currentStates) => ({
        ...currentStates,
        assessments: {
          ...currentStates.assessments,
          error: message,
        },
      }))
    } finally {
      assessmentsRefreshInFlightRef.current = false
    }
  }, [])

  const scheduleAssessmentsLayerRefresh = useCallback(() => {
    if (assessmentsRefreshTimeoutRef.current != null) return
    assessmentsRefreshTimeoutRef.current = window.setTimeout(() => {
      assessmentsRefreshTimeoutRef.current = null
      void refreshAssessmentsLayer()
    }, 450)
  }, [refreshAssessmentsLayer])

  // This function cancels currently running single-building analysis.
  const handleStopSingleBuildingAnalysis = useCallback(async () => {
    const batchId = buildingDrawer?.batchId
    if (!batchId) return
    try {
      await cancelBatch(batchId)
      toast.success("Stopping building analysis...")
      setSingleWasStopped(true)
      setBuildingDrawer((prev) => prev ? { ...prev, progressLabel: "Stopped" } : null)
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to stop building analysis"
      toast.error(message)
    }
  }, [buildingDrawer?.batchId])

  const handleShowPendingBatchInMap = useCallback((batch: PendingBatchRecord) => {
    void (async () => {
      const map = mapInstanceRef.current
      if (!map) return

      try {
        const batchBuildings = await fetchBatchBuildings(batch.batch_id, 5000)
        const osmIds = (batchBuildings.osm_ids ?? []).filter((id) => Number.isFinite(id))
        if (osmIds.length === 0) {
          toast("No buildings found for this site yet")
          return
        }

        setOverlayStates((current) => ({
          ...current,
          turkey_buildings: { ...current.turkey_buildings, visible: true },
        }))
        applyOverlayVisibility(map, "turkey_buildings", true)

        if (!ensurePendingBatchHighlightLayer(map)) {
          toast.error("Building layer not ready yet")
          return
        }

        map.setFilter(PENDING_BATCH_HIGHLIGHT_LAYER_ID, pendingBatchPolygonOsmHighlightFilter(osmIds))
        map.setLayoutProperty(PENDING_BATCH_HIGHLIGHT_LAYER_ID, "visibility", "visible")
        raisePendingBatchHighlightLayer(map)

        if (batchBuildings.bbox) {
          map.fitBounds(
            [
              [batchBuildings.bbox.west, batchBuildings.bbox.south],
              [batchBuildings.bbox.east, batchBuildings.bbox.north],
            ],
            { padding: 60, duration: 900 },
          )
        }

        map.once("idle", () => {
          const currentMap = mapInstanceRef.current
          if (!currentMap?.getLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID)) return
          currentMap.setFilter(PENDING_BATCH_HIGHLIGHT_LAYER_ID, pendingBatchPolygonOsmHighlightFilter(osmIds))
          raisePendingBatchHighlightLayer(currentMap)
        })

        setSelectedPendingBatchId(batch.batch_id)

        if (pendingHighlightTimeoutRef.current) {
          window.clearTimeout(pendingHighlightTimeoutRef.current)
        }
        pendingHighlightTimeoutRef.current = window.setTimeout(() => {
          const currentMap = mapInstanceRef.current
          if (currentMap?.getLayer(PENDING_BATCH_HIGHLIGHT_LAYER_ID)) {
            currentMap.setFilter(PENDING_BATCH_HIGHLIGHT_LAYER_ID, pendingBatchHighlightInactiveFilter())
          }
          setSelectedPendingBatchId((current) => (current === batch.batch_id ? null : current))
        }, 7000)
      } catch (error) {
        const message = error instanceof Error ? error.message : "Failed to show site buildings"
        toast(message)
      }
    })()
  }, [])

  const handleAnalyzePendingBatch = useCallback(async (batch: PendingBatchRecord) => {
    try {
      const result = await analyzeExistingBatch(batch.batch_id)
      toast.success(`Analysis started for ${batch.site_name || batch.batch_id}`)
      setDismissedPendingBatchIds((prev) => [...prev, batch.batch_id])

      const bId = result.batch_id
      if (batchStreamCleanupRef.current) {
        batchStreamCleanupRef.current()
        batchStreamCleanupRef.current = null
      }
      setActiveBatchId(bId)
      setActiveBatchSiteName(batch.site_name || batch.batch_id)
      setIsChatSidebarOpen(true)
      setBatchProgress({ total: 0, processed: 0, failed: 0, skipped: 0, events: [] })
      setBatchDone(false)
      setBatchWasStopped(false)
      setBatchBuildingProgress({})
      setBatchTokensUsed(0)
      setProcessingOsmIds([])
      setCurrentThinkingFull("")
      setCurrentResponseFull("")
      setAiThinkingExpanded(false)

      const cleanup = subscribeBatchStream(
        bId,
        (ev: BatchSseEvent) => {
          if (ev.type === "batch_failed") {
            setCurrentAiStage(null)
            setBatchDone(true)
            setProcessingOsmIds([])
          }
          if (ev.type === "building_started") {
            const osmId = Number(ev.osm_id)
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                osm_id: osmId,
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 10),
                stage: "start",
                thought: prev[osmId]?.thought ?? "Preparing building analysis.",
                status: "processing",
                updatedAt: Date.now(),
              },
            }))
          }
          if (ev.type === "building_clipping") {
            const osmId = Number(ev.osm_id)
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                ...(prev[osmId] ?? {
                  osm_id: osmId,
                  progressPercent: 0,
                  stage: "start",
                  thought: "",
                  status: "processing",
                  updatedAt: Date.now(),
                }),
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 20),
                stage: "clipping",
                thought: prev[osmId]?.thought || "Preparing building chip.",
                status: "processing",
                updatedAt: Date.now(),
              },
            }))
          }
          if (ev.type === "building_analyzing") {
            const osmId = Number(ev.osm_id)
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                ...(prev[osmId] ?? {
                  osm_id: osmId,
                  progressPercent: 0,
                  stage: "start",
                  thought: "",
                  status: "processing",
                  updatedAt: Date.now(),
                }),
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 35),
                stage: "analyzing",
                thought: prev[osmId]?.thought || "AI is analyzing this building.",
                status: "processing",
                updatedAt: Date.now(),
              },
            }))
          }
          if (ev.type === "building_ai_stage") {
            const osmId = Number(ev.osm_id)
            const incomingProgress = Number(ev.progress_percent)
            const normalizedProgress = Number.isFinite(incomingProgress) ? incomingProgress : 45
            const liveText = getLiveAiTextFromEvent(ev, "AI is analyzing this building.")
            const incomingThinking = typeof ev.thinking_text === "string" && ev.thinking_text.trim() ? ev.thinking_text.trim() : undefined
            const incomingResponse = typeof ev.response_text === "string" && ev.response_text.trim() ? ev.response_text.trim() : undefined
            if (ev.stage === "context_window_full") {
              toast.warning(`Building ${osmId}: context window full — model ran out of space. Result may be incomplete.`, { duration: 8000 })
            }
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setCurrentAiStage({
              osm_id: osmId,
              stage: String(ev.stage || "processing"),
              thought: liveText,
            })
            if (incomingThinking) setCurrentThinkingFull(incomingThinking)
            if (incomingResponse) setCurrentResponseFull(incomingResponse)
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                ...(prev[osmId] ?? {
                  osm_id: osmId,
                  progressPercent: 0,
                  stage: "analyzing",
                  thought: "",
                  status: "processing",
                  updatedAt: Date.now(),
                }),
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, Math.min(95, normalizedProgress)),
                stage: String(ev.stage ?? "processing"),
                thought: liveText,
                status: "processing",
                updatedAt: Date.now(),
                thinkingText: incomingThinking ?? prev[osmId]?.thinkingText,
                responseText: incomingResponse ?? prev[osmId]?.responseText,
              },
            }))
          }
          setBatchProgress((prev) => {
            const base = prev ?? { total: 0, processed: 0, failed: 0, skipped: 0, events: [] }
            if (ev.type === "batch_started") {
              return { ...base, total: Number(ev.total_buildings || base.total) }
            }
            if (ev.type === "building_done") {
              scheduleAssessmentsLayerRefresh()
              setCurrentAiStage(null)
              setCurrentThinkingFull("")
              setCurrentResponseFull("")
              if (ev.total_tokens) setBatchTokensUsed((t) => t + Number(ev.total_tokens))
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prevIds) => prevIds.filter((id) => id !== osmId))
              setBatchBuildingProgress((prevProgress) => ({
                ...prevProgress,
                [osmId]: {
                  ...(prevProgress[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "analyzing",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: 100,
                  stage: "completed",
                  thought: "Building analysis complete.",
                  status: "done",
                  updatedAt: Date.now(),
                  totalTokens: ev.total_tokens ? Number(ev.total_tokens) : undefined,
                  contextWindow: ev.context_window ? Number(ev.context_window) : undefined,
                },
              }))
              return {
                ...base,
                processed: base.processed + 1,
                events: [...base.events, {
                  osm_id: osmId,
                  status: "done",
                  assessment_id: ev.assessment_id as string,
                  severity: ev.severity as number,
                  chip_path: ev.chip_path as string | undefined,
                  pre_chip_path: ev.pre_chip_path as string | undefined,
                }],
              }
            }
            if (ev.type === "building_failed") {
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prevIds) => prevIds.filter((id) => id !== osmId))
              setBatchBuildingProgress((prevProgress) => ({
                ...prevProgress,
                [osmId]: {
                  ...(prevProgress[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "analyzing",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: 100,
                  stage: "failed",
                  thought: String(ev.error ?? "Building analysis failed."),
                  status: "failed",
                  updatedAt: Date.now(),
                },
              }))
              return {
                ...base,
                failed: base.failed + 1,
                events: [...base.events, { osm_id: osmId, status: "failed", error: ev.error as string }],
              }
            }
            if (ev.type === "building_skipped") {
              const osmId = Number(ev.osm_id)
              const skipThought = ev.reason === "no_orthophoto_coverage"
                ? "Building is outside the uploaded orthophoto coverage area."
                : "Building skipped (already assessed)."
              setProcessingOsmIds((prevIds) => prevIds.filter((id) => id !== osmId))
              setBatchBuildingProgress((prevProgress) => ({
                ...prevProgress,
                [osmId]: {
                  ...(prevProgress[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "start",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: 100,
                  stage: "skipped",
                  thought: skipThought,
                  status: "skipped",
                  updatedAt: Date.now(),
                },
              }))
              return {
                ...base,
                skipped: base.skipped + 1,
                events: [...base.events, { osm_id: osmId, status: "skipped", error: ev.reason === "no_orthophoto_coverage" ? "no_orthophoto_coverage" : undefined }],
              }
            }
            if (ev.type === "batch_complete") {
              setCurrentAiStage(null)
              setProcessingOsmIds([])
              setBatchDone(true)
              return {
                ...base,
                total: Number(ev.total || base.total),
                processed: Number(ev.processed || base.processed),
                failed: Number(ev.failed || base.failed),
                skipped: Number(ev.skipped || base.skipped),
              }
            }
            return base
          })
        },
        () => {
          setBatchDone(true)
          batchStreamCleanupRef.current = null
        },
        (err) => {
          toast.error(`Batch stream error: ${err.message}`)
        }
      )
      batchStreamCleanupRef.current = cleanup
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to start analysis"
      toast.error(message)
    }
  }, [batchWasStopped])

  useEffect(() => {
    if (processingOsmIds.length === 0) {
      setProcessingBlinkOn(false)
      return
    }
    const timer = window.setInterval(() => {
      setProcessingBlinkOn((prev) => !prev)
    }, 550)
    return () => window.clearInterval(timer)
  }, [processingOsmIds.length])

  useEffect(() => {
    const map = mapInstanceRef.current
    if (!map) return
    if (!map.getLayer("processing-buildings-fill") || !map.getLayer("processing-buildings-outline")) return

    const osmIds = processingOsmIds.map((id) => Number(id)).filter((id) => Number.isFinite(id))
    const filterExpression = ["in", ["get", "osm_id"], ["literal", osmIds]] as unknown as maplibregl.FilterSpecification
    map.setFilter("processing-buildings-fill", filterExpression)
    map.setFilter("processing-buildings-outline", filterExpression)
    map.setPaintProperty("processing-buildings-fill", "fill-opacity", processingBlinkOn ? 0.32 : 0.12)
    map.setPaintProperty("processing-buildings-outline", "line-opacity", processingBlinkOn ? 0.95 : 0.35)
    // Raise highlight layers above imagery rasters so they stay visible when
    // post-earthquake or satellite tiles are turned on (rasters are opaque and
    // would otherwise bury these vector layers).
    if (osmIds.length > 0) {
      map.moveLayer("processing-buildings-fill")
      map.moveLayer("processing-buildings-outline")
    }
  }, [processingOsmIds, processingBlinkOn])

  // This function updates one layer state safely.
  const updateOverlayState = (
    layerKey: GisLayerKey,
    updater: (current: OverlayLayerState) => OverlayLayerState
  ) => {
    setOverlayStates((currentStates) => ({
      ...currentStates,
      [layerKey]: updater(currentStates[layerKey]),
    }))
  }

  // This function toggles overlay visibility and applies it to map if already loaded.
  const handleToggleOverlay = (layerKey: GisLayerKey) => {
    setOverlayStates((currentStates) => {
      const nextVisible = !currentStates[layerKey].visible
      const map = mapInstanceRef.current
      if (map && overlayLoadedRef.current.has(layerKey)) {
        applyOverlayVisibility(map, layerKey, nextVisible)
      }

      return {
        ...currentStates,
        [layerKey]: {
          ...currentStates[layerKey],
          visible: nextVisible,
        },
      }
    })
  }

  // This function moves one overlay up/down in ranking and reapplies map draw order.
  const handleMoveOverlayRank = (layerKey: GisLayerKey, direction: "up" | "down") => {
    setOverlayOrder((currentOrder) => {
      const currentIndex = currentOrder.indexOf(layerKey)
      if (currentIndex < 0) {
        return currentOrder
      }

      const targetIndex = direction === "up" ? currentIndex - 1 : currentIndex + 1
      if (targetIndex < 0 || targetIndex >= currentOrder.length) {
        return currentOrder
      }

      const nextOrder = [...currentOrder]
      const [movedLayer] = nextOrder.splice(currentIndex, 1)
      nextOrder.splice(targetIndex, 0, movedLayer)

      const map = mapInstanceRef.current
      if (map) {
        applyOverlayRankingOrder(map, nextOrder)
      }

      return nextOrder
    })
  }

  // This effect keeps the draw polygon MapLibre source in sync with drawPoints state.
  useEffect(() => {
    const map = mapInstanceRef.current
    if (!map) return
    const src = map.getSource("draw-polygon") as maplibregl.GeoJSONSource | undefined
    if (!src) return

    if (drawPoints.length === 0) {
      src.setData({ type: "FeatureCollection", features: [] })
      return
    }

    const pts = drawPoints
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const features: any[] = pts.map((pt) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: pt },
      properties: {},
    }))

    if (pts.length >= 2) {
      const lineCoords = [...pts, pts[0]]
      features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: lineCoords },
        properties: {},
      })
    }

    if (pts.length >= 3) {
      features.push({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [[...pts, pts[0]]] },
        properties: {},
      })
    }

    src.setData({ type: "FeatureCollection", features })

    // Keep draw layers above overlays so polygon remains visible while drawing.
    const drawLayerIds = ["draw-polygon-fill", "draw-polygon-line", "draw-polygon-points"]
    for (const layerId of drawLayerIds) {
      if (map.getLayer(layerId)) {
        map.moveLayer(layerId)
      }
    }
  }, [drawPoints])

  // This effect registers / removes map click handler for polygon drawing mode.
  useEffect(() => {
    drawModeRef.current = drawMode
    const map = mapInstanceRef.current
    if (!map) return

    if (drawMode) {
      map.getCanvas().style.cursor = "crosshair"
    } else {
      map.getCanvas().style.cursor = ""
    }
  }, [drawMode])

  // This function finalizes the currently drawn polygon and opens batch configuration.
  const finalizeDrawPolygon = async () => {
    const pts = drawPointsRef.current
    if (pts.length < 3) {
      toast.error("Draw at least 3 points to create a polygon")
      return
    }

    setDrawMode(false)
    const map = mapInstanceRef.current
    if (map) {
      map.getCanvas().style.cursor = ""
      map.off("click", handleDrawClick)
      map.off("dblclick", handleDrawDblClick)
      map.off("contextmenu", handleDrawRightClick)
    }

    await loadExistingSites()
    setNewSiteNameInput("")
    setBatchModalOpen(true)
  }

  // This function handles clicking the map to add a draw point or finish polygon.
  const handleDrawClick = useCallback((e: maplibregl.MapMouseEvent) => {
    const map = mapInstanceRef.current
    const pts = drawPointsRef.current

    // Finalize by left-clicking close to the first point once at least 3 points exist.
    if (map && pts.length >= 3) {
      const firstPoint = pts[0]
      const firstScreenPoint = map.project({ lng: firstPoint[0], lat: firstPoint[1] })
      const clickScreenPoint = e.point
      const dx = firstScreenPoint.x - clickScreenPoint.x
      const dy = firstScreenPoint.y - clickScreenPoint.y
      const distancePx = Math.sqrt(dx * dx + dy * dy)
      if (distancePx <= 14) {
        void finalizeDrawPolygon()
        return
      }
    }

    drawPointsRef.current = [...pts, [e.lngLat.lng, e.lngLat.lat]]
    setDrawPoints([...drawPointsRef.current])
  }, [finalizeDrawPolygon])

  // This function loads existing site names from the backend for the picker.
  const loadExistingSites = useCallback(async () => {
    setSitesLoading(true)
    try {
      const sites = await fetchBatchSites()
      setExistingSites(sites)
      if (sites.length > 0) {
        setSelectedExistingSite(sites[0])
        setUseNewSite(false)
      } else {
        setSelectedExistingSite("")
        setUseNewSite(true)
      }
    } catch {
      setExistingSites([])
      setUseNewSite(true)
    } finally {
      setSitesLoading(false)
    }
  }, [])

  // This function fetches uploads with no site and updates the notification.
  const refreshUnassignedUploads = useCallback(async (silent = false) => {
    if (!silent) setUnassignedUploadsLoading(true)
    try {
      const result = await fetchUnassignedUploads()
      setUnassignedUploads(result.uploads)
      // Re-show notification if new unassigned uploads appear.
      if (result.count > 0) setUnassignedNotifDismissed(false)
    } catch {
      // non-critical, keep last state
    } finally {
      if (!silent) setUnassignedUploadsLoading(false)
    }
  }, [])

  // This function handles double-click to close the polygon and open batch modal.
  const handleDrawDblClick = useCallback(
    async (e: maplibregl.MapMouseEvent) => {
      e.preventDefault()
      await finalizeDrawPolygon()
    },
    [finalizeDrawPolygon]
  )

  // This function handles right-click to finalize polygon without adding points.
  const handleDrawRightClick = useCallback(
    async (e: maplibregl.MapMouseEvent) => {
      e.preventDefault()
      e.originalEvent.preventDefault()
      await finalizeDrawPolygon()
    },
    [finalizeDrawPolygon]
  )

  // This function starts draw mode and attaches map listeners.
  const startDrawMode = useCallback(() => {
    const map = mapInstanceRef.current
    if (!map) return
    drawPointsRef.current = []
    setDrawPoints([])
    setDrawMode(true)
    map.on("click", handleDrawClick)
    map.on("dblclick", handleDrawDblClick)
    map.on("contextmenu", handleDrawRightClick)
    toast("Draw your analysis zone — click to add points, right-click or click first point to finish")
  }, [handleDrawClick, handleDrawDblClick, handleDrawRightClick])

  // This function cancels draw mode and clears polygon.
  const cancelDrawMode = useCallback(() => {
    const map = mapInstanceRef.current
    if (map) {
      map.off("click", handleDrawClick)
      map.off("dblclick", handleDrawDblClick)
      map.off("contextmenu", handleDrawRightClick)
      map.getCanvas().style.cursor = ""
    }
    drawPointsRef.current = []
    setDrawPoints([])
    setDrawMode(false)
  }, [handleDrawClick, handleDrawDblClick, handleDrawRightClick])

  // This function submits the batch analysis request.
  const submitBatch = useCallback(async () => {
    const resolvedSiteName = useNewSite ? newSiteNameInput.trim() : selectedExistingSite.trim()
    if (!resolvedSiteName) {
      toast.error("Choose or enter a site name")
      return
    }
    const pts = drawPointsRef.current
    if (pts.length < 3) {
      toast.error("Draw a polygon first")
      return
    }
    setBatchSubmitting(true)
    try {
      const polygon = {
        type: "Polygon" as const,
        coordinates: [[...pts, pts[0]]],
      }
      const result = await startOrthophotoBatch({
        post_ortho_upload_id: batchUploadId.trim() || undefined,
        area_polygon: polygon,
        site_name: resolvedSiteName,
        worker_name: batchWorkerName.trim() || undefined,
        force_reanalyze: false,
      })
      setBatchModalOpen(false)
      // Clear draw polygon
      drawPointsRef.current = []
      setDrawPoints([])

      // Start monitoring the batch.
      const bId = result.batch_id
      if (batchStreamCleanupRef.current) {
        batchStreamCleanupRef.current()
        batchStreamCleanupRef.current = null
      }
      setActiveBatchId(bId)
      setActiveBatchSiteName(resolvedSiteName)
      setSiteBuildingsPanel(null)
      setBatchProgress({ total: 0, processed: 0, failed: 0, skipped: 0, events: [] })
      setBatchDone(false)
      setBatchWasStopped(false)
      setBatchBuildingProgress({})
      setBatchTokensUsed(0)
      setProcessingOsmIds([])
      setCurrentThinkingFull("")
      setCurrentResponseFull("")
      setAiThinkingExpanded(false)

      const cleanup = subscribeBatchStream(
        bId,
        (ev: BatchSseEvent) => {
          if (ev.type === "batch_failed") {
            setCurrentAiStage(null)
            setBatchDone(true)
            setProcessingOsmIds([])
          }
          if (ev.type === "building_started") {
            const osmId = Number(ev.osm_id)
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                osm_id: osmId,
                progressPercent: 10,
                stage: "start",
                thought: "Starting building analysis.",
                status: "processing",
                updatedAt: Date.now(),
              },
            }))
          }
          if (ev.type === "building_clipping") {
            const osmId = Number(ev.osm_id)
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                ...(prev[osmId] ?? {
                  osm_id: osmId,
                  progressPercent: 0,
                  stage: "start",
                  thought: "",
                  status: "processing",
                  updatedAt: Date.now(),
                }),
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 20),
                stage: "clipping",
                thought: "Clipping building from orthophoto.",
                status: "processing",
                updatedAt: Date.now(),
              },
            }))
          }
          if (ev.type === "building_analyzing") {
            const osmId = Number(ev.osm_id)
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                ...(prev[osmId] ?? {
                  osm_id: osmId,
                  progressPercent: 0,
                  stage: "start",
                  thought: "",
                  status: "processing",
                  updatedAt: Date.now(),
                }),
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 35),
                stage: "analyzing",
                thought: "AI is analyzing this building.",
                status: "processing",
                updatedAt: Date.now(),
              },
            }))
          }
          // Live AI stage events — update the "currently processing" indicator.
          if (ev.type === "building_ai_stage") {
            const osmId = Number(ev.osm_id)
            const incomingProgress = Number(ev.progress_percent)
            const normalizedProgress = Number.isFinite(incomingProgress) ? incomingProgress : 45
            const liveText = getLiveAiTextFromEvent(ev, "AI is analyzing this building.")
            const incomingThinking = typeof ev.thinking_text === "string" && ev.thinking_text.trim() ? ev.thinking_text.trim() : undefined
            const incomingResponse = typeof ev.response_text === "string" && ev.response_text.trim() ? ev.response_text.trim() : undefined
            if (ev.stage === "context_window_full") {
              toast.warning(`Building ${osmId}: context window full — model ran out of space. Result may be incomplete.`, { duration: 8000 })
            }
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setCurrentAiStage({
              osm_id: osmId,
              stage: ev.stage as string,
              thought: liveText,
            })
            if (incomingThinking) setCurrentThinkingFull(incomingThinking)
            if (incomingResponse) setCurrentResponseFull(incomingResponse)
            setBatchBuildingProgress((prev) => ({
              ...prev,
              [osmId]: {
                ...(prev[osmId] ?? {
                  osm_id: osmId,
                  progressPercent: 0,
                  stage: "analyzing",
                  thought: "",
                  status: "processing",
                  updatedAt: Date.now(),
                }),
                progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, Math.min(95, normalizedProgress)),
                stage: String(ev.stage ?? "tool_call"),
                thought: liveText,
                status: "processing",
                updatedAt: Date.now(),
                thinkingText: incomingThinking ?? prev[osmId]?.thinkingText,
                responseText: incomingResponse ?? prev[osmId]?.responseText,
              },
            }))
            return
          }
          setBatchProgress((prev) => {
            const base = prev ?? { total: 0, processed: 0, failed: 0, skipped: 0, events: [] }
            if (ev.type === "batch_started") {
              return { ...base, total: (ev.total_buildings as number) ?? 0 }
            }
            if (ev.type === "building_done") {
              scheduleAssessmentsLayerRefresh()
              setCurrentAiStage(null)
              setCurrentThinkingFull("")
              setCurrentResponseFull("")
              if (ev.total_tokens) setBatchTokensUsed((t) => t + Number(ev.total_tokens))
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prev) => prev.filter((id) => id !== osmId))
              setBatchBuildingProgress((prevProgress) => ({
                ...prevProgress,
                [osmId]: {
                  ...(prevProgress[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "analyzing",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: 100,
                  stage: "completed",
                  thought: "Building analysis complete.",
                  status: "done",
                  updatedAt: Date.now(),
                  totalTokens: ev.total_tokens ? Number(ev.total_tokens) : undefined,
                  contextWindow: ev.context_window ? Number(ev.context_window) : undefined,
                },
              }))
              return {
                ...base,
                processed: base.processed + 1,
                events: [...base.events, { osm_id: ev.osm_id as number, status: "done", assessment_id: ev.assessment_id as string, severity: ev.severity as number, chip_path: ev.chip_path as string, pre_chip_path: ev.pre_chip_path as string | undefined }],
              }
            }
            if (ev.type === "building_failed") {
              setCurrentAiStage(null)
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prev) => prev.filter((id) => id !== osmId))
              setBatchBuildingProgress((prevProgress) => ({
                ...prevProgress,
                [osmId]: {
                  ...(prevProgress[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "analyzing",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: 100,
                  stage: "failed",
                  thought: String(ev.error ?? "Building analysis failed."),
                  status: "failed",
                  updatedAt: Date.now(),
                },
              }))
              return {
                ...base,
                failed: base.failed + 1,
                events: [...base.events, { osm_id: ev.osm_id as number, status: "failed", error: ev.error as string }],
              }
            }
            if (ev.type === "building_skipped") {
              const osmId = Number(ev.osm_id)
              const skipThought = ev.reason === "no_orthophoto_coverage"
                ? "Building is outside the uploaded orthophoto coverage area."
                : "Building skipped (already assessed)."
              setProcessingOsmIds((prev) => prev.filter((id) => id !== osmId))
              setBatchBuildingProgress((prevProgress) => ({
                ...prevProgress,
                [osmId]: {
                  ...(prevProgress[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "start",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: 100,
                  stage: "skipped",
                  thought: skipThought,
                  status: "skipped",
                  updatedAt: Date.now(),
                },
              }))
              return { ...base, skipped: base.skipped + 1, events: [...base.events, { osm_id: ev.osm_id as number, status: "skipped", error: ev.reason === "no_orthophoto_coverage" ? "no_orthophoto_coverage" : undefined }] }
            }
            if (ev.type === "batch_complete") {
              setCurrentAiStage(null)
              setProcessingOsmIds([])
              return {
                ...base,
                total: (ev.total as number) ?? base.total,
                processed: (ev.processed as number) ?? base.processed,
                failed: (ev.failed as number) ?? base.failed,
                skipped: (ev.skipped as number) ?? base.skipped,
              }
            }
            return base
          })
        },
        () => {
          setBatchDone(true)
          batchStreamCleanupRef.current = null
        },
        (err) => { toast.error(`Batch stream error: ${err.message}`) }
      )
      batchStreamCleanupRef.current = cleanup

      toast.success(`Batch ${bId} started`)
      return cleanup
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to start batch")
    } finally {
      setBatchSubmitting(false)
    }
  }, [useNewSite, newSiteNameInput, selectedExistingSite, batchUploadId, batchWorkerName])

  // This function closes the feature info sidebar and clears the map highlight.
  const handleCloseFeatureInfoSidebar = useCallback(() => {
    setSelectedFeatureInfo(null)
    const map = mapInstanceRef.current
    if (map && selectedFeatureRef.current) {
      map.setFeatureState(
        { source: selectedFeatureRef.current.source, id: selectedFeatureRef.current.id },
        { selected: false }
      )
      selectedFeatureRef.current = null
    }
  }, [])

  // This function handles the Analyse button click from the feature info sidebar.
  const handleAnalyseBuilding = useCallback(async (osmId: number, lat: number, lon: number) => {
    // Close the info sidebar and clear the map selection so no page refresh is needed.
    setSelectedFeatureInfo(null)
    const map = mapInstanceRef.current
    if (map && selectedFeatureRef.current) {
      map.setFeatureState(
        { source: selectedFeatureRef.current.source, id: selectedFeatureRef.current.id },
        { selected: false }
      )
      selectedFeatureRef.current = null
    }
    pendingBldgRef.current = { osmId, lat, lon }
    setNewSiteNameInput("")
    await loadExistingSites()
    setSingleBldgPickerOpen(true)
  }, [loadExistingSites])

  // This function fetches and opens the Site Buildings panel for the active batch's site.
  const openSiteBuildingsPanel = useCallback(async (siteName: string) => {
    setSiteBuildingsPanelLoading(true)
    setSiteBuildingsPanel(null)
    try {
      const sites = await fetchSitesFull()
      const matched = sites.find((s) => s.name.toLowerCase() === siteName.toLowerCase())
      if (!matched) {
        toast.error(`Site "${siteName}" not found`)
        return
      }
      const result = await fetchSiteBuildings(matched.id)
      if (!result) {
        toast.error("Failed to load site buildings")
        return
      }
      setSiteBuildingsPanel(result)
    } catch (err) {
      toast.error(`Failed to load site buildings: ${err}`)
    } finally {
      setSiteBuildingsPanelLoading(false)
    }
  }, [])

  // This function opens Gemma4 chat with a building-specific tool-use prompt.
  const handleAskAiAboutBuilding = useCallback((
    _properties: Record<string, unknown>,
    _lat: number,
    _lon: number,
    geometry?: unknown
  ) => {
    if (!geometry) {
      toast.error("Selected building geometry is unavailable")
      return
    }
    setSelectedBuildingChatContext({
      label: "Building 1",
      geometry,
    })
    setIsChatSidebarOpen(true)
    toast.success("Building context added to Gemma4 chat")
  }, [])

  // This function runs after site is confirmed in the single-building picker.
  const confirmSingleBldgAnalysis = useCallback(async () => {
    const pending = pendingBldgRef.current
    if (!pending) return
    const resolvedSiteName = useNewSite ? newSiteNameInput.trim() : selectedExistingSite.trim()
    setSingleBldgPickerOpen(false)
    const { osmId, lat, lon } = pending
    pendingBldgRef.current = null
    setBuildingDrawer({
      osmId,
      lat,
      lon,
      batchId: null,
      events: [],
      done: false,
      progressPercent: 5,
      progressLabel: "Queued for analysis",
      tokensUsed: 0,
    })
    setSingleWasStopped(false)
    setIsChatSidebarOpen(true)
    try {
      if (singleBuildingStreamCleanupRef.current) {
        singleBuildingStreamCleanupRef.current()
        singleBuildingStreamCleanupRef.current = null
      }
      const result = await analyzeSingleBuilding(osmId, lat, lon, resolvedSiteName)
      const bId = result.batch_id
      setBuildingDrawer((prev) => prev ? { ...prev, batchId: bId } : null)

      const cleanup = subscribeBatchStream(
        bId,
        (ev: BatchSseEvent) => {
          const normalizeStageLabel = (value: unknown): string =>
            String(value ?? "")
              .replaceAll("_", " ")
              .trim()

          if (ev.type === "batch_failed") {
            setProcessingOsmIds((prev) => prev.filter((id) => id !== Number(ev.osm_id)))
            const errorText = String(ev.error ?? "")
            const wasStopped = errorText.toLowerCase().includes("canceled") || errorText.toLowerCase().includes("cancelled") || singleWasStopped
            setBuildingDrawer((prev) => prev ? {
              ...prev,
              done: true,
              aiThought: undefined,
              aiStage: undefined,
              progressPercent: 100,
              progressLabel: wasStopped ? "Stopped" : "Analysis failed",
            } : null)
            return
          }

          if (ev.type === "building_ai_stage") {
            setProcessingOsmIds((prev) => (prev.includes(Number(ev.osm_id)) ? prev : [...prev, Number(ev.osm_id)]))
            setBuildingDrawer((prev) => {
              if (!prev) return null
              const liveText = getLiveAiTextFromEvent(ev, "AI analyzing building")
              const nextProgressRaw = Number(ev.progress_percent ?? prev.progressPercent)
              const nextProgress = Number.isFinite(nextProgressRaw)
                ? Math.max(prev.progressPercent, Math.min(90, nextProgressRaw))
                : prev.progressPercent
              return {
                ...prev,
                aiThought: liveText,
                aiStage: normalizeStageLabel(ev.stage),
                progressPercent: nextProgress,
                progressLabel: normalizeStageLabel(ev.stage) || "AI analyzing building",
              }
            })
            return
          }
          setBuildingDrawer((prev) => {
            if (!prev) return null
            if (ev.type === "building_started" || ev.type === "building_clipping") {
              setProcessingOsmIds((prevList) => (prevList.includes(Number(ev.osm_id)) ? prevList : [...prevList, Number(ev.osm_id)]))
              return {
                ...prev,
                progressPercent: 20,
                progressLabel: "Clipping building chip",
                aiThought: undefined,
                aiStage: undefined,
              }
            }
            if (ev.type === "building_analyzing") {
              setProcessingOsmIds((prevList) => (prevList.includes(Number(ev.osm_id)) ? prevList : [...prevList, Number(ev.osm_id)]))
              return {
                ...prev,
                progressPercent: Math.max(prev.progressPercent, 35),
                progressLabel: "Running AI analysis",
              }
            }
            if (ev.type === "building_done") {
              scheduleAssessmentsLayerRefresh()
              setProcessingOsmIds((prevList) => prevList.filter((id) => id !== Number(ev.osm_id)))
              return {
                ...prev,
                aiThought: undefined,
                aiStage: undefined,
                progressPercent: 100,
                progressLabel: "Analysis complete",
                tokensUsed: prev.tokensUsed + (ev.total_tokens ? Number(ev.total_tokens) : 0),
                events: [...prev.events, { osm_id: ev.osm_id as number, status: "done", assessment_id: ev.assessment_id as string, severity: ev.severity as number, chip_path: ev.chip_path as string, pre_chip_path: ev.pre_chip_path as string | undefined }],
              }
            }
            if (ev.type === "building_failed") {
              setProcessingOsmIds((prevList) => prevList.filter((id) => id !== Number(ev.osm_id)))
              return {
                ...prev,
                aiThought: undefined,
                aiStage: undefined,
                progressPercent: 100,
                progressLabel: "Analysis failed",
                events: [...prev.events, { osm_id: ev.osm_id as number, status: "failed", error: ev.error as string }],
              }
            }
            return prev
          })
        },
        () => {
          setBuildingDrawer((prev) => prev ? { ...prev, done: true } : null)
          singleBuildingStreamCleanupRef.current = null
        },
        (err) => { toast.error(`Analysis error: ${err.message}`) }
      )
      singleBuildingStreamCleanupRef.current = cleanup
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to start analysis")
      setBuildingDrawer(null)
    }
  }, [useNewSite, newSiteNameInput, selectedExistingSite])

  useEffect(() => {
    return () => {
      if (batchStreamCleanupRef.current) {
        batchStreamCleanupRef.current()
      }
      if (singleBuildingStreamCleanupRef.current) {
        singleBuildingStreamCleanupRef.current()
      }
      if (pendingHighlightTimeoutRef.current) {
        window.clearTimeout(pendingHighlightTimeoutRef.current)
      }
      if (assessmentsRefreshTimeoutRef.current) {
        window.clearTimeout(assessmentsRefreshTimeoutRef.current)
      }
    }
  }, [])

  // This function refreshes the assessments layer and focuses one assessment in map view.
  const handleShowAssessmentOnMap = useCallback(async (assessmentId: string, lat: number, lon: number) => {
    if (!assessmentId) {
      toast.error("Assessment ID is missing")
      return
    }

    const map = mapInstanceRef.current
    if (!map) {
      toast.error("Map is not ready yet")
      return
    }

    try {
      const layerResult = await fetchAssessmentBuildingLayer(2000)
      overlayGeoJsonRef.current.assessments = layerResult.geojson as GeoJsonFeatureCollection

      const source = map.getSource(getOverlaySourceId("assessments")) as maplibregl.GeoJSONSource | undefined
      if (source) {
        source.setData(layerResult.geojson as never)
      }

      applyOverlayVisibility(map, "assessments", true)
      setOverlayStates((currentStates) => ({
        ...currentStates,
        assessments: {
          ...currentStates.assessments,
          visible: true,
          featureCount: layerResult.featureCount,
          error: null,
        },
      }))

      const showInMap = showAssessmentOnMapRef.current
      if (showInMap) {
        showInMap(assessmentId, lat, lon)
      } else {
        toast.error("Assessment popup helper is not ready")
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to focus assessment"
      toast.error(message)
    }
  }, [])

  const syncAiChatGeometrySource = useCallback(() => {
    const map = mapInstanceRef.current
    if (!map || !map.isStyleLoaded()) {
      return
    }
    ensureAiChatResultLayers(map)

    // Register click + cursor handlers for AI overlay layers once
    if (!aiClickHandlerAddedRef.current) {
      aiClickHandlerAddedRef.current = true
      const handleAiClick = (e: maplibregl.MapLayerMouseEvent) => {
        if (!e.features || e.features.length === 0) return
        const props = (e.features[0].properties ?? {}) as Record<string, unknown>
        setSelectedAiOverlay({ properties: props, lat: e.lngLat.lat, lon: e.lngLat.lng })
      }
      const setCursor = (cursor: string) => () => { map.getCanvas().style.cursor = cursor }
      for (const layerId of [AI_CHAT_RESULT_FILL_LAYER_ID, AI_CHAT_RESULT_LINE_LAYER_ID, AI_CHAT_RESULT_POINT_LAYER_ID]) {
        map.on("click", layerId, handleAiClick)
        map.on("mouseenter", layerId, setCursor("pointer"))
        map.on("mouseleave", layerId, setCursor(""))
      }
    }

    const mergedFeatures = chatGeometryOverlaysRef.current.flatMap((overlay) =>
      overlay.visible
        ? (overlay.geojson.features ?? []).map((feature, index) => ({
            ...feature,
            id: feature.id ?? `${overlay.id}-feature-${index}`,
            properties: {
              ...(feature.properties ?? {}),
              overlay_id: overlay.id,
              overlay_name: overlay.name,
            },
          }))
        : []
    )
    const mergedGeoJson = {
      type: "FeatureCollection",
      features: mergedFeatures,
    } as GeoJsonFeatureCollection
    aiChatResultGeoJsonRef.current = mergedGeoJson

    const source = map.getSource(AI_CHAT_RESULT_SOURCE_ID) as maplibregl.GeoJSONSource | undefined
    if (source) {
      source.setData(mergedGeoJson as never)
    }
  }, [])

  const handleRemoveChatGeometryOverlay = useCallback((overlayId: string) => {
    chatGeometryOverlaysRef.current = chatGeometryOverlaysRef.current.filter((overlay) => overlay.id !== overlayId)
    setChatGeometryOverlays(
      chatGeometryOverlaysRef.current.map((overlay) => ({
        id: overlay.id,
        name: overlay.name,
        visible: overlay.visible,
      }))
    )
    syncAiChatGeometrySource()
  }, [syncAiChatGeometrySource])

  const handleClearChatGeometryOverlays = useCallback(() => {
    chatGeometryOverlaysRef.current = []
    setChatGeometryOverlays([])
    syncAiChatGeometrySource()
  }, [syncAiChatGeometrySource])

  const handleToggleChatGeometryOverlay = useCallback((overlayId: string) => {
    chatGeometryOverlaysRef.current = chatGeometryOverlaysRef.current.map((overlay) =>
      overlay.id === overlayId ? { ...overlay, visible: !overlay.visible } : overlay
    )
    setChatGeometryOverlays(
      chatGeometryOverlaysRef.current.map((overlay) => ({
        id: overlay.id,
        name: overlay.name,
        visible: overlay.visible,
      }))
    )
    syncAiChatGeometrySource()
  }, [syncAiChatGeometrySource])

  const handleFlyToOverlay = useCallback((overlayId: string) => {
    const overlay = chatGeometryOverlaysRef.current.find((o) => o.id === overlayId)
    if (!overlay) return
    const map = mapInstanceRef.current
    if (!map || !map.isStyleLoaded()) return
    const allPairs = overlay.geojson.features.flatMap((feature) =>
      collectLngLatPairsFromGeoJsonGeometry(feature.geometry as { type: string; coordinates: unknown })
    )
    if (allPairs.length === 0) return
    const bounds = new maplibregl.LngLatBounds(allPairs[0], allPairs[0])
    for (const [lng, lat] of allPairs.slice(1)) {
      bounds.extend([lng, lat])
    }
    map.fitBounds(bounds, { padding: 70, duration: 900, maxZoom: 16 })
  }, [])

  // This function renders geometry returned by AI tool results onto dedicated map overlays.
  const handleChatToolResult = useCallback((toolName: string, result: Record<string, unknown>) => {
    const extraction = extractFeaturesFromChatToolResult(toolName, result)
    const features = extraction.features
    if (features.length === 0) {
      return
    }

    const nextGeoJson = {
      type: "FeatureCollection",
      features,
    } as GeoJsonFeatureCollection
    const overlayId = `${toolName}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    chatGeometryOverlaysRef.current = [
      ...chatGeometryOverlaysRef.current,
      {
        id: overlayId,
        name: extraction.overlayName,
        geojson: nextGeoJson,
        visible: true,
      },
    ]
    setChatGeometryOverlays(
      chatGeometryOverlaysRef.current.map((overlay) => ({
        id: overlay.id,
        name: overlay.name,
        visible: overlay.visible,
      }))
    )
    syncAiChatGeometrySource()

    const map = mapInstanceRef.current
    if (!map) {
      return
    }

    // Defer fitBounds by one animation frame so the source.setData() call inside
    // syncAiChatGeometrySource has time to process before we move the camera.
    const allPairs = features.flatMap((feature) =>
      collectLngLatPairsFromGeoJsonGeometry(feature.geometry as { type: string; coordinates: unknown })
    )
    if (allPairs.length > 0) {
      requestAnimationFrame(() => {
        const bounds = new maplibregl.LngLatBounds(allPairs[0], allPairs[0])
        for (const [lng, lat] of allPairs.slice(1)) {
          bounds.extend([lng, lat])
        }
        map.fitBounds(bounds, { padding: 70, duration: 900, maxZoom: 16 })
      })
    }
  }, [syncAiChatGeometrySource])

  // Keep pending site batches in sync for right-side cards.
  useEffect(() => {
    let isMounted = true

    const loadPendingBatches = async () => {
      try {
        const pending = await fetchPendingBatches(20, false)
        if (!isMounted) {
          return
        }
        setPendingBatches(pending)
      } catch {
        if (isMounted) {
          setPendingBatches([])
        }
      }
    }

    void loadPendingBatches()
    const intervalId = window.setInterval(() => {
      void loadPendingBatches()
    }, 5000)

    return () => {
      isMounted = false
      window.clearInterval(intervalId)
    }
  }, [])

  // On refresh, auto-resume the latest active batch stream so progress panel persists.
  useEffect(() => {
    let isMounted = true
    let localCleanup: (() => void) | null = null

    const resumeLatestActiveBatch = async () => {
      try {
        // If another flow already attached a stream, do not override it.
        if (batchStreamCleanupRef.current) return

        const activeItems = await fetchPendingBatches(20, true)
        if (!isMounted || activeItems.length === 0) return

        const latestActive = [...activeItems].sort((a, b) => {
          const ta = a.created_at ? Date.parse(a.created_at) : 0
          const tb = b.created_at ? Date.parse(b.created_at) : 0
          return tb - ta
        })[0]

        const resumedBatchId = latestActive.batch_id
        const status = await getBatchStatus(resumedBatchId)
        if (!isMounted) return

        setActiveBatchId(resumedBatchId)
        setActiveBatchSiteName(latestActive.site_name || "")
        setSiteBuildingsPanel(null)
        setBatchDone(false)
        setBatchWasStopped(false)
        setBatchTokensUsed(0)
        setBatchProgress((prev) => ({
          ...(prev ?? { events: [] }),
          total:     Math.max(prev?.total     ?? 0, Math.max(0, Number(status.total_buildings) || 0)),
          processed: Math.max(prev?.processed ?? 0, Math.max(0, Number(status.processed) || 0)),
          failed:    Math.max(prev?.failed    ?? 0, Math.max(0, Number(status.failed) || 0)),
          skipped:   Math.max(prev?.skipped   ?? 0, Math.max(0, Number(status.skipped) || 0)),
        }))
        setCurrentAiStage({
          osm_id: 0,
          stage: "reconnected",
          thought: "Reconnected to active analysis after page refresh.",
        })
        setBatchBuildingProgress({})
        setProcessingOsmIds([])

        const cleanup = subscribeBatchStream(
          resumedBatchId,
          (ev: BatchSseEvent) => {
            if (ev.type === "batch_failed") {
              setCurrentAiStage(null)
              setBatchDone(true)
              setProcessingOsmIds([])
            }
            if (ev.type === "building_started") {
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
              setBatchBuildingProgress((prev) => ({
                ...prev,
                [osmId]: {
                  osm_id: osmId,
                  progressPercent: 10,
                  stage: "start",
                  thought: "Starting building analysis.",
                  status: "processing",
                  updatedAt: Date.now(),
                },
              }))
            }
            if (ev.type === "building_clipping") {
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
              setBatchBuildingProgress((prev) => ({
                ...prev,
                [osmId]: {
                  ...(prev[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "start",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 20),
                  stage: "clipping",
                  thought: "Clipping building from orthophoto.",
                  status: "processing",
                  updatedAt: Date.now(),
                },
              }))
            }
            if (ev.type === "building_analyzing") {
              const osmId = Number(ev.osm_id)
              setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
              setBatchBuildingProgress((prev) => ({
                ...prev,
                [osmId]: {
                  ...(prev[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "start",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, 35),
                  stage: "analyzing",
                  thought: "AI is analyzing this building.",
                  status: "processing",
                  updatedAt: Date.now(),
                },
              }))
            }
            if (ev.type === "building_ai_stage") {
              const osmId = Number(ev.osm_id)
              const incomingProgress = Number(ev.progress_percent)
              const normalizedProgress = Number.isFinite(incomingProgress) ? incomingProgress : 45
              const liveText = getLiveAiTextFromEvent(ev, "AI is analyzing this building.")
              setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
              setCurrentAiStage({
                osm_id: osmId,
                stage: String(ev.stage || "processing"),
                thought: liveText,
              })
              setBatchBuildingProgress((prev) => ({
                ...prev,
                [osmId]: {
                  ...(prev[osmId] ?? {
                    osm_id: osmId,
                    progressPercent: 0,
                    stage: "analyzing",
                    thought: "",
                    status: "processing",
                    updatedAt: Date.now(),
                  }),
                  progressPercent: Math.max(prev[osmId]?.progressPercent ?? 0, Math.min(95, normalizedProgress)),
                  stage: String(ev.stage ?? "processing"),
                  thought: liveText,
                  status: "processing",
                  updatedAt: Date.now(),
                },
              }))
              return
            }
            setBatchProgress((prev) => {
              const base = prev ?? { total: 0, processed: 0, failed: 0, skipped: 0, events: [] }
              if (ev.type === "batch_started") {
                return { ...base, total: Number(ev.total_buildings || base.total) }
              }
              if (ev.type === "building_done") {
                scheduleAssessmentsLayerRefresh()
                setCurrentAiStage(null)
                const osmId = Number(ev.osm_id)
                setProcessingOsmIds((prevIds) => prevIds.filter((id) => id !== osmId))
                setBatchBuildingProgress((prevProgress) => ({
                  ...prevProgress,
                  [osmId]: {
                    ...(prevProgress[osmId] ?? {
                      osm_id: osmId,
                      progressPercent: 0,
                      stage: "analyzing",
                      thought: "",
                      status: "processing",
                      updatedAt: Date.now(),
                    }),
                    progressPercent: 100,
                    stage: "completed",
                    thought: "Building analysis complete.",
                    status: "done",
                    updatedAt: Date.now(),
                  },
                }))
                return {
                  ...base,
                  processed: base.processed + 1,
                  events: [...base.events, { osm_id: ev.osm_id as number, status: "done", assessment_id: ev.assessment_id as string, severity: ev.severity as number, chip_path: ev.chip_path as string, pre_chip_path: ev.pre_chip_path as string | undefined }],
                }
              }
              if (ev.type === "building_failed") {
                setCurrentAiStage(null)
                const osmId = Number(ev.osm_id)
                setProcessingOsmIds((prevIds) => prevIds.filter((id) => id !== osmId))
                setBatchBuildingProgress((prevProgress) => ({
                  ...prevProgress,
                  [osmId]: {
                    ...(prevProgress[osmId] ?? {
                      osm_id: osmId,
                      progressPercent: 0,
                      stage: "analyzing",
                      thought: "",
                      status: "processing",
                      updatedAt: Date.now(),
                    }),
                    progressPercent: 100,
                    stage: "failed",
                    thought: String(ev.error ?? "Building analysis failed."),
                    status: "failed",
                    updatedAt: Date.now(),
                  },
                }))
                return {
                  ...base,
                  failed: base.failed + 1,
                  events: [...base.events, { osm_id: ev.osm_id as number, status: "failed", error: ev.error as string }],
                }
              }
              if (ev.type === "building_skipped") {
                const osmId = Number(ev.osm_id)
                const skipThought = ev.reason === "no_orthophoto_coverage"
                  ? "Building is outside the uploaded orthophoto coverage area."
                  : "Building skipped (already assessed)."
                const skipError = ev.reason === "no_orthophoto_coverage"
                  ? "no_orthophoto_coverage"
                  : undefined
                setProcessingOsmIds((prevIds) => prevIds.filter((id) => id !== osmId))
                setBatchBuildingProgress((prevProgress) => ({
                  ...prevProgress,
                  [osmId]: {
                    ...(prevProgress[osmId] ?? {
                      osm_id: osmId,
                      progressPercent: 0,
                      stage: "start",
                      thought: "",
                      status: "processing",
                      updatedAt: Date.now(),
                    }),
                    progressPercent: 100,
                    stage: "skipped",
                    thought: skipThought,
                    status: "skipped",
                    updatedAt: Date.now(),
                  },
                }))
                return { ...base, skipped: base.skipped + 1, events: [...base.events, { osm_id: ev.osm_id as number, status: "skipped", error: skipError }] }
              }
              if (ev.type === "batch_complete") {
                setCurrentAiStage(null)
                setProcessingOsmIds([])
                setBatchDone(true)
                return {
                  ...base,
                  total: Number(ev.total || base.total),
                  processed: Number(ev.processed || base.processed),
                  failed: Number(ev.failed || base.failed),
                  skipped: Number(ev.skipped || base.skipped),
                }
              }
              return base
            })
          },
          () => {
            setBatchDone(true)
            batchStreamCleanupRef.current = null
          },
          () => {
            // Keep silent here; polling effect below still updates state.
          },
        )

        localCleanup = cleanup
        batchStreamCleanupRef.current = cleanup
      } catch {
        // Non-blocking: no active batches or stream unavailable.
      }
    }

    void resumeLatestActiveBatch()

    return () => {
      isMounted = false
      if (localCleanup) localCleanup()
    }
  }, [])

  // Poll DB status for active batch so progress survives missed SSE events after refresh.
  useEffect(() => {
    if (!activeBatchId || batchDone) return
    let isMounted = true
    const syncStatus = async () => {
      try {
        const status = await getBatchStatus(activeBatchId)
        if (!isMounted) return
        setBatchProgress((prev) => {
          const base = prev ?? { total: 0, processed: 0, failed: 0, skipped: 0, events: [] }
          const dbTotal     = Math.max(0, Number(status.total_buildings) || 0)
          const dbProcessed = Math.max(0, Number(status.processed) || 0)
          const dbFailed    = Math.max(0, Number(status.failed) || 0)
          const dbSkipped   = Math.max(0, Number(status.skipped) || 0)
          // Use Math.max so a stale or early DB read never rolls back values
          // that the SSE stream has already advanced forward.
          return {
            ...base,
            total:     Math.max(base.total,     dbTotal),
            processed: Math.max(base.processed, dbProcessed),
            failed:    Math.max(base.failed,     dbFailed),
            skipped:   Math.max(base.skipped,    dbSkipped),
          }
        })
        if (String(status.status || "").toLowerCase() === "complete") {
          setBatchDone(true)
          setCurrentAiStage(null)
          setProcessingOsmIds([])
        }
      } catch {
        // Silent fallback; SSE may still be working.
      }
    }
    // Delay the first poll by 1 s so the SSE stream can establish its baseline
    // before we risk overwriting it with a stale DB snapshot.
    const firstPoll = window.setTimeout(() => { void syncStatus() }, 1000)
    const timer     = window.setInterval(() => { void syncStatus() }, 3000)
    return () => {
      isMounted = false
      window.clearTimeout(firstPoll)
      window.clearInterval(timer)
    }
  }, [activeBatchId, batchDone])

  // This effect polls for unassigned uploads on mount and every 30 seconds.
  useEffect(() => {
    void refreshUnassignedUploads()
    const timer = window.setInterval(() => { void refreshUnassignedUploads(true) }, 30_000)
    return () => {
      window.clearInterval(timer)
    }
  }, [refreshUnassignedUploads])

  // Refresh unassigned uploads after batch completes — the new site may now cover them.
  useEffect(() => {
    if (batchDone) void refreshUnassignedUploads(true)
  }, [batchDone, refreshUnassignedUploads])

  // This effect keeps unassigned-upload building highlights in sync with state.
  // Uses the turkey_buildings polygon source (same as processing highlights) so the
  // actual footprint is shown, not a GPS point.  moveLayer() is called every time to
  // keep the layers on top of any raster imagery overlays added later.
  useEffect(() => {
    const map = mapInstanceRef.current
    if (!map) return

    const active = showUnassignedOnMap && unassignedUploads.length > 0

    // ── Building polygon highlight (for uploads with a matched osm_id) ────────
    const osmIds = active
      ? unassignedUploads
          .map((u) => u.nearby_osm_id)
          .filter((id): id is number => id !== null)
      : []
    const filterExpr = ["in", ["get", "osm_id"], ["literal", osmIds]] as unknown as maplibregl.FilterSpecification

    if (map.getLayer("unassigned-buildings-fill")) {
      map.setFilter("unassigned-buildings-fill", filterExpr)
      map.moveLayer("unassigned-buildings-fill")
    }
    if (map.getLayer("unassigned-buildings-outline")) {
      map.setFilter("unassigned-buildings-outline", filterExpr)
      map.moveLayer("unassigned-buildings-outline")
    }

    // ── Fallback point for uploads with no nearby building ────────────────────
    const orphanedPoints = active
      ? unassignedUploads.filter((u) => u.nearby_osm_id === null)
      : []
    const ptsSrc = map.getSource("unassigned-uploads-pts") as maplibregl.GeoJSONSource | undefined
    if (ptsSrc) {
      ptsSrc.setData({
        type: "FeatureCollection",
        features: orphanedPoints.map((u) => ({
          type: "Feature" as const,
          geometry: { type: "Point" as const, coordinates: [u.lon, u.lat] },
          properties: { id: u.id, file_type: u.file_type },
        })),
      })
      if (map.getLayer("unassigned-uploads-dot")) map.moveLayer("unassigned-uploads-dot")
    }
  }, [showUnassignedOnMap, unassignedUploads])

  // This function initializes the map once and tears it down on unmount.
  useEffect(() => {
    let isUnmounted = false

    // This function builds and mounts the MapLibre map with PMTiles source.
    const initializeMap = async () => {
      if (!mapContainerRef.current) {
        return
      }

      if (!protocolAdded) {
        maplibregl.addProtocol("pmtiles", protocol.tile)
        protocolAdded = true
      }

      const pmtilesHttpUrl = `${window.location.origin}/api/tiles/turkey.pmtiles`
      const pmtilesSourceUrl = `pmtiles://${pmtilesHttpUrl}`
      const pmtiles = new PMTiles(pmtilesHttpUrl)
      protocol.add(pmtiles)

      const rawMetadata = await pmtiles.getMetadata()
      if (isUnmounted) {
        return
      }

      const metadata: PmtilesMetadata =
        rawMetadata !== null && typeof rawMetadata === "object"
          ? (rawMetadata as PmtilesMetadata)
          : {}

      const vectorLayers = Array.isArray(metadata.vector_layers) ? metadata.vector_layers : []

      const styleLayers: maplibregl.LayerSpecification[] = []
      for (const layer of vectorLayers) {
        const sourceLayer = typeof layer?.id === "string" ? layer.id : ""
        if (!sourceLayer) {
          continue
        }

        // Only render polygon fills from basemap - lines and points are controlled by overlay layers
        styleLayers.push({
          id: `${sourceLayer}-fill`,
          type: "fill",
          source: "turkey",
          "source-layer": sourceLayer,
          filter: ["==", ["geometry-type"], "Polygon"],
          paint: {
            "fill-color": "#DAD7CD",
            "fill-opacity": 0.6,
          },
        })
      }

      const map = new maplibregl.Map({
        container: mapContainerRef.current,
        style: {
          version: 8,
          sources: {
            turkey: {
              type: "vector",
              url: pmtilesSourceUrl,
            },
          },
          layers: [
            {
              id: "background",
              type: "background",
              paint: {
                "background-color": "#F1EFE8",
              },
            },
            ...styleLayers,
          ],
        },
        center: [35.32, 38.99],
        zoom: 6,
        attributionControl: false,
      })

      map.addControl(new maplibregl.NavigationControl(), "top-right")
      map.addControl(new maplibregl.ScaleControl(), "bottom-left")

      // Fly to requested province coordinates
      map.on("load", () => {
        syncAiChatGeometrySource()
        setTimeout(() => {
          map.flyTo({
            center: [38.268983, 37.763058],
            zoom: 13,
            duration: 2500,
            essential: true,
          })
        }, 500)
      })

      // This function focuses one assessment by ID and opens the same popup template used by map clicks.
      const showAssessmentOnMap = (assessmentId: string, fallbackLat: number, fallbackLon: number) => {
        const features = overlayGeoJsonRef.current.assessments?.features ?? []
        const matchedFeature = features.find((feature) => {
          const properties = (feature.properties ?? {}) as Record<string, unknown>
          const featureId = feature.id
          return String(properties.id ?? featureId ?? "") === assessmentId
        })

        if (!matchedFeature) {
          toast.error("Assessment not found in map layer")
          return
        }

        const properties = (matchedFeature.properties ?? {}) as Record<string, unknown>
        const focusLat = Number(properties.lat ?? fallbackLat)
        const focusLon = Number(properties.lon ?? fallbackLon)

        if (selectedFeatureRef.current && mapInstanceRef.current) {
          mapInstanceRef.current.setFeatureState(
            { source: selectedFeatureRef.current.source, id: selectedFeatureRef.current.id },
            { selected: false }
          )
          selectedFeatureRef.current = null
        }

        if (matchedFeature.id !== undefined) {
          map.setFeatureState(
            { source: getOverlaySourceId("assessments"), id: matchedFeature.id },
            { selected: true }
          )
          selectedFeatureRef.current = { source: getOverlaySourceId("assessments"), id: matchedFeature.id }
        }

        map.flyTo({
          center: [focusLon, focusLat],
          zoom: Math.max(map.getZoom(), 17),
          essential: true,
        })

        setSelectedFeatureInfo({
          properties,
          layerKey: "assessments",
          layerLabel: "Assessments",
          lat: focusLat,
          lon: focusLon,
        })
      }

      showAssessmentOnMapRef.current = showAssessmentOnMap

      // This function handles feature click and shows popup with highlighting.
      const handleFeatureClick = (e: maplibregl.MapLayerMouseEvent) => {
        if (!e.features || e.features.length === 0) return

        const feature = e.features[0]
        const sourceId = feature.source
        const featureId = feature.id

        // Reset previous selection
        if (selectedFeatureRef.current && mapInstanceRef.current) {
          mapInstanceRef.current.setFeatureState(
            { source: selectedFeatureRef.current.source, id: selectedFeatureRef.current.id },
            { selected: false }
          )
        }

        // Set new selection
        if (featureId !== undefined) {
          map.setFeatureState({ source: sourceId, id: featureId }, { selected: true })
          selectedFeatureRef.current = { source: sourceId, id: featureId }
        }

        // Find layer config
        const layerKey = sourceId.replace("overlay-source-", "") as GisLayerKey
        const config = overlayLayerConfigs.find((c) => c.key === layerKey)
        const layerLabel = config?.label || "Feature"

        // Open the right-hand info sidebar instead of a popup.
        const properties = (feature.properties ?? {}) as Record<string, unknown>
        // Prefer the feature's own coordinates when present so the sidebar pins the actual feature, not the click point.
        const latNumber = Number(properties.lat)
        const lonNumber = Number(properties.lon)
        const focusLat = Number.isFinite(latNumber) ? latNumber : e.lngLat.lat
        const focusLon = Number.isFinite(lonNumber) ? lonNumber : e.lngLat.lng

        setSelectedFeatureInfo({
          properties,
          layerKey,
          layerLabel,
          lat: focusLat,
          lon: focusLon,
          geometry: feature.geometry,
        })
      }

      for (const config of overlayLayerConfigs) {
        updateOverlayState(config.key, (currentState) => ({
          ...currentState,
          isLoading: true,
          error: null,
        }))

        try {
          // Handle DEM raster layers differently from vector layers
          if (config.isDEM) {
            const isVisible = overlayStates[config.key].visible
            const regions = ["adiyaman", "hatay"]

            // Determine which backend endpoint to use
            const tileEndpoint = config.key === "satellite_pre"
              ? `http://localhost:8000/satellite/tiles`
              : `http://localhost:8000/dem/tiles`

            // Raster opacity — satellite imagery should be fully opaque
            const rasterOpacity = config.key === "satellite_pre" ? 1.0 : 0.7

            // Add both Adiyaman and Hatay sources and layers
            for (const region of regions) {
              const sourceId = `${config.key}-${region}-source`
              const layerId = `${config.key}-${region}-raster`

              if (!map.getSource(sourceId)) {
                map.addSource(sourceId, {
                  type: "raster",
                  tiles: [`${tileEndpoint}/${region}/{z}/{x}/{y}.png`],
                  tileSize: 256,
                  minzoom: 0,
                  maxzoom: 20,
                })
              }

              if (!map.getLayer(layerId)) {
                map.addLayer({
                  id: layerId,
                  type: "raster",
                  source: sourceId,
                  layout: {
                    visibility: isVisible ? "visible" : "none",
                  },
                  paint: {
                    "raster-opacity": rasterOpacity,
                  },
                })
              }
            }

            overlayLoadedRef.current.add(config.key)
            updateOverlayState(config.key, (currentState) => ({
              ...currentState,
              isLoading: false,
              featureCount: 0,
              error: null,
            }))
          } else if (config.key === "post_earthquake_images") {
            const isVisible = overlayStates[config.key].visible
            const postLayerResult = await fetchPostEarthquakeLayer(5000)
            if (isUnmounted) {
              continue
            }

            const features = Array.isArray(postLayerResult.geojson.features)
              ? postLayerResult.geojson.features
              : []
            const uploadIds = features
              .map((feature) => {
                const properties = feature.properties as Record<string, unknown> | undefined
                const idValue = properties?.id
                return typeof idValue === "string" && idValue ? idValue : null
              })
              .filter((idValue): idValue is string => Boolean(idValue))

            for (const uploadId of uploadIds) {
              const sourceId = `post-earthquake-${uploadId}-source`
              const layerId = `post-earthquake-${uploadId}-raster`

              if (!map.getSource(sourceId)) {
                map.addSource(sourceId, {
                  type: "raster",
                  tiles: [`http://localhost:8000/uploads/post-earthquake-tiles/${uploadId}/{z}/{x}/{y}.png`],
                  tileSize: 256,
                  minzoom: 0,
                  maxzoom: 20,
                })
              }

              if (!map.getLayer(layerId)) {
                map.addLayer({
                  id: layerId,
                  type: "raster",
                  source: sourceId,
                  layout: {
                    visibility: isVisible ? "visible" : "none",
                  },
                  paint: {
                    "raster-opacity": 1.0,
                  },
                })
              }
            }

            overlayLoadedRef.current.add(config.key)
            updateOverlayState(config.key, (currentState) => ({
              ...currentState,
              isLoading: false,
              featureCount: uploadIds.length,
              error: null,
            }))
          } else {
            // Handle vector GeoJSON layers
            const layerResult = config.key === "assessments"
              ? await (async () => {
                  return await fetchAssessmentBuildingLayer(500)
                })()
              : await fetchGisLayer(config.key)
            if (isUnmounted) {
              continue
            }

            const isVisible = overlayStates[config.key].visible
            addOverlayToMap(map, config, layerResult.geojson as GeoJsonFeatureCollection, isVisible)
            overlayGeoJsonRef.current[config.key] = layerResult.geojson as GeoJsonFeatureCollection
            overlayLoadedRef.current.add(config.key)

            // Add click handlers for interactive layers
            const layerIds = getOverlayLayerIds(config.key)
            map.on("click", layerIds.fill, handleFeatureClick)
            map.on("click", layerIds.point, handleFeatureClick)

            // Change cursor on hover
            map.on("mouseenter", layerIds.fill, () => {
              map.getCanvas().style.cursor = drawModeRef.current ? "crosshair" : "pointer"
            })
            map.on("mouseleave", layerIds.fill, () => {
              map.getCanvas().style.cursor = drawModeRef.current ? "crosshair" : ""
            })
            map.on("mouseenter", layerIds.point, () => {
              map.getCanvas().style.cursor = drawModeRef.current ? "crosshair" : "pointer"
            })
            map.on("mouseleave", layerIds.point, () => {
              map.getCanvas().style.cursor = drawModeRef.current ? "crosshair" : ""
            })

            updateOverlayState(config.key, (currentState) => ({
              ...currentState,
              isLoading: false,
              featureCount: layerResult.featureCount,
              error: null,
            }))
          }
        } catch (error) {
          const errorMessage = error instanceof Error ? error.message : "Layer load failed"
          updateOverlayState(config.key, (currentState) => ({
            ...currentState,
            isLoading: false,
            error: errorMessage,
          }))
        }
      }

      applyOverlayRankingOrder(map, overlayLayerConfigs.map((config) => config.key))

      // ── Processing building highlight layers (blink while AI runs) ────────
      const buildingSourceId = getOverlaySourceId("turkey_buildings")
      if (map.getSource(buildingSourceId)) {
        if (!map.getLayer("processing-buildings-fill")) {
          map.addLayer({
            id: "processing-buildings-fill",
            type: "fill",
            source: buildingSourceId,
            filter: ["in", ["get", "osm_id"], ["literal", []]],
            paint: {
              "fill-color": "#06B6D4",
              "fill-opacity": 0.05,
            },
          })
        }
        if (!map.getLayer("processing-buildings-outline")) {
          map.addLayer({
            id: "processing-buildings-outline",
            type: "line",
            source: buildingSourceId,
            filter: ["in", ["get", "osm_id"], ["literal", []]],
            paint: {
              "line-color": "#06B6D4",
              "line-width": 3,
              "line-opacity": 0.35,
            },
          })
        }
      }

      // ── Draw polygon layer (for batch zone selection) ─────────────────────
      map.addSource("draw-polygon", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      })
      map.addLayer({
        id: "draw-polygon-fill",
        type: "fill",
        source: "draw-polygon",
        filter: ["==", ["geometry-type"], "Polygon"],
        paint: { "fill-color": "#14B8A6", "fill-opacity": 0.3 },
      })
      map.addLayer({
        id: "draw-polygon-line",
        type: "line",
        source: "draw-polygon",
        filter: ["==", ["geometry-type"], "LineString"],
        paint: { "line-color": "#0B7A69", "line-width": 3, "line-dasharray": [2, 1] },
      })
      map.addLayer({
        id: "draw-polygon-points",
        type: "circle",
        source: "draw-polygon",
        filter: ["==", ["geometry-type"], "Point"],
        paint: { "circle-radius": 6, "circle-color": "#0B7A69", "circle-stroke-width": 2, "circle-stroke-color": "#ffffff" },
      })

      const pendingBatchBuildingSourceId = getOverlaySourceId("turkey_buildings")
      if (map.getSource(pendingBatchBuildingSourceId)) {
        ensurePendingBatchHighlightLayer(map)
      }

      // ── Unassigned-uploads: building polygon highlight (orange) ─────────────
      // Uses the same turkey_buildings vector source as the processing layers so the
      // actual building footprint is highlighted, not just a GPS point.
      const unassignedBldgSourceId = getOverlaySourceId("turkey_buildings")
      if (map.getSource(unassignedBldgSourceId)) {
        if (!map.getLayer("unassigned-buildings-fill")) {
          map.addLayer({
            id: "unassigned-buildings-fill",
            type: "fill",
            source: unassignedBldgSourceId,
            filter: ["in", ["get", "osm_id"], ["literal", []]],
            paint: {
              "fill-color": "#F97316",
              "fill-opacity": 0.28,
            },
          })
        }
        if (!map.getLayer("unassigned-buildings-outline")) {
          map.addLayer({
            id: "unassigned-buildings-outline",
            type: "line",
            source: unassignedBldgSourceId,
            filter: ["in", ["get", "osm_id"], ["literal", []]],
            paint: {
              "line-color": "#F97316",
              "line-width": 3,
              "line-opacity": 0.95,
            },
          })
        }
      }
      // Fallback: small orange dot for uploads that have no nearby building match.
      map.addSource("unassigned-uploads-pts", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      })
      map.addLayer({
        id: "unassigned-uploads-dot",
        type: "circle",
        source: "unassigned-uploads-pts",
        paint: {
          "circle-radius": 8,
          "circle-color": "#F97316",
          "circle-opacity": 0.9,
          "circle-stroke-width": 2,
          "circle-stroke-color": "#ffffff",
        },
      })

      mapInstanceRef.current = map

      return map
    }

    let mapInstance: maplibregl.Map | undefined
    initializeMap().then((createdMap) => {
      mapInstance = createdMap
    })

    return () => {
      isUnmounted = true
      mapInstanceRef.current = null
      overlayLoadedRef.current.clear()
      chatGeometryOverlaysRef.current = []
      showAssessmentOnMapRef.current = null
      if (mapInstance) {
        mapInstance.remove()
      }
    }
  }, [])

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <div ref={mapContainerRef} className="h-full w-full" />

      <MapDrawControls
        drawMode={drawMode}
        drawPointsCount={drawPoints.length}
        onStartDraw={startDrawMode}
        onCancelDraw={cancelDrawMode}
      />

      <PendingBatchCards
        batches={visiblePendingBatches}
        selectedBatchId={selectedPendingBatchId}
        onDismiss={(batchId) => setDismissedPendingBatchIds((prev) => [...prev, batchId])}
        onShowInMap={handleShowPendingBatchInMap}
        onAnalyze={(batch) => void handleAnalyzePendingBatch(batch)}
      />

      {chatGeometryOverlays.length > 0 && (
        <div className="absolute right-16 top-4 z-30 w-72 rounded-lg border border-[#D3D1C7] bg-white shadow-xl">
          <div className="flex items-center justify-between rounded-t-lg bg-[#9A3412] px-3 py-2">
            <span className="text-sm font-bold text-white">AI Geometry Overlays</span>
            <button
              type="button"
              onClick={handleClearChatGeometryOverlays}
              className="rounded border border-white/35 bg-white/10 px-2 py-0.5 text-[10px] font-semibold text-white hover:bg-white/20"
            >
              Clear all
            </button>
          </div>
          <div className="max-h-44 space-y-1 overflow-y-auto px-2 py-2">
            {chatGeometryOverlays.map((overlay) => (
              <div
                key={overlay.id}
                className={`flex items-center justify-between gap-2 rounded border px-2 py-1.5 transition-colors ${
                  overlay.visible
                    ? "border-[#E6E3D8] bg-[#FAFAF8]"
                    : "border-[#E6E3D8] bg-[#F0EFEB] opacity-50"
                }`}
              >
                <button
                  type="button"
                  onClick={() => handleFlyToOverlay(overlay.id)}
                  className="min-w-0 flex-1 truncate text-left text-xs font-medium text-[#17352b] hover:text-[#9A3412] hover:underline"
                  title="Fly to this overlay"
                >
                  {overlay.name}
                </button>
                <button
                  type="button"
                  onClick={() => handleToggleChatGeometryOverlay(overlay.id)}
                  className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold transition-colors ${
                    overlay.visible
                      ? "border-[#0F6E56] bg-[#E1F5EE] text-[#0F6E56] hover:bg-[#c8ede0]"
                      : "border-[#D3D1C7] bg-white text-[#8a9490] hover:bg-[#F3F1E9]"
                  }`}
                  title={overlay.visible ? "Hide overlay" : "Show overlay"}
                  aria-label={overlay.visible ? `Hide ${overlay.name}` : `Show ${overlay.name}`}
                >
                  {overlay.visible ? "On" : "Off"}
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Unassigned uploads notification */}
      {unassignedUploads.length > 0 && !unassignedNotifDismissed && (
        <div className="absolute bottom-16 right-4 z-30 w-72 rounded-lg border border-orange-200 bg-white shadow-xl">
          {/* Header */}
          <div className="flex items-center justify-between rounded-t-lg bg-orange-500 px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-white text-[11px] font-bold text-orange-600">
                {unassignedUploads.length}
              </span>
              <span className="text-sm font-bold text-white">Uploads need a site</span>
            </div>
            <button
              type="button"
              onClick={() => { setUnassignedNotifDismissed(true); setShowUnassignedOnMap(false) }}
              className="text-white/70 hover:text-white"
            >
              <X size={15} />
            </button>
          </div>

          {/* Body */}
          <div className="px-3 py-2.5 space-y-2">
            <p className="text-[11px] text-[#6b7280] leading-relaxed">
              {unassignedUploads.length === 1
                ? "1 ground photo/video has no site assigned."
                : `${unassignedUploads.length} ground photos/videos have no site assigned.`}{" "}
              Draw a site boundary that covers them so the batch pipeline can process them.
            </p>

            {/* Upload list */}
            <div className="max-h-32 space-y-1 overflow-y-auto rounded-md border border-[#F3F1E9] bg-[#FAFAF8] px-2 py-1.5">
              {unassignedUploads.map((u) => (
                <div key={u.id} className="flex items-center gap-2 text-[10px]">
                  <span className={`shrink-0 rounded px-1 py-0.5 font-semibold ${
                    u.file_type === "video"
                      ? "bg-purple-100 text-purple-700"
                      : "bg-orange-100 text-orange-700"
                  }`}>
                    {u.file_type === "video" ? "VID" : "IMG"}
                  </span>
                  <span className="min-w-0 truncate text-[#17352b]" title={u.filename}>
                    {u.filename || u.id}
                  </span>
                  <button
                    type="button"
                    className="shrink-0 text-[#0F6E56] underline"
                    title="Fly to building"
                    onClick={() => {
                      setShowUnassignedOnMap(true)
                      const map = mapInstanceRef.current
                      if (map) map.flyTo({ center: [u.lon, u.lat], zoom: 19, duration: 800 })
                    }}
                  >
                    ↗
                  </button>
                </div>
              ))}
            </div>

            {/* Actions */}
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => {
                  setShowUnassignedOnMap((v) => !v)
                  if (!showUnassignedOnMap && unassignedUploads.length > 0) {
                    // Fly to first upload location.
                    const first = unassignedUploads[0]
                    const map = mapInstanceRef.current
                    if (map) {
                      map.flyTo({
                        center: [first.lon, first.lat],
                        zoom: 16,
                        duration: 900,
                      })
                    }
                  }
                }}
                className={`flex-1 rounded-md border py-1.5 text-[11px] font-semibold transition-colors ${
                  showUnassignedOnMap
                    ? "border-orange-400 bg-orange-100 text-orange-700 hover:bg-orange-200"
                    : "border-[#D3D1C7] bg-[#FAFAF8] text-[#17352b] hover:bg-[#ECEAE2]"
                }`}
              >
                {showUnassignedOnMap ? "Hide on Map" : "Show on Map"}
              </button>
              <button
                type="button"
                onClick={() => void refreshUnassignedUploads()}
                disabled={unassignedUploadsLoading}
                className="rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-3 py-1.5 text-[11px] font-semibold text-[#6b7280] hover:bg-[#ECEAE2] disabled:opacity-50"
              >
                {unassignedUploadsLoading ? <Loader2 size={11} className="animate-spin" /> : "↻"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Batch progress panel */}
      {activeBatchId && batchProgress && (
        <div className="absolute right-4 top-4 z-30 w-72 rounded-lg border border-[#D3D1C7] bg-white shadow-xl">
          <div className="flex items-center justify-between rounded-t-lg bg-[#0F6E56] px-3 py-2">
            <span className="text-sm font-bold text-white">
              {batchDone ? (batchWasStopped ? "Batch Stopped" : "Batch Complete") : "Batch Running…"}
            </span>
            <div className="flex items-center gap-2">
              {activeBatchSiteName && (
                <button
                  type="button"
                  onClick={() => void openSiteBuildingsPanel(activeBatchSiteName)}
                  disabled={siteBuildingsPanelLoading}
                  title="View all buildings in this site"
                  className="inline-flex items-center gap-1 rounded border border-white/30 bg-white/15 px-2 py-0.5 text-[10px] font-semibold text-white hover:bg-white/25 disabled:opacity-50"
                >
                  {siteBuildingsPanelLoading ? <Loader2 size={10} className="animate-spin" /> : <LayoutList size={10} />}
                  Site
                </button>
              )}
              {!batchDone && (
                <button
                  type="button"
                  onClick={() => void handleStopBatchAnalysis()}
                  className="rounded border border-red-200 bg-white/15 px-2 py-0.5 text-[10px] font-semibold text-white hover:bg-white/25"
                >
                  Stop
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  if (batchStreamCleanupRef.current) {
                    batchStreamCleanupRef.current()
                    batchStreamCleanupRef.current = null
                  }
                  setActiveBatchId(null)
                  setActiveBatchSiteName("")
                  setSiteBuildingsPanel(null)
                  setBatchProgress(null)
                  setBatchDone(false)
                  setBatchWasStopped(false)
                  setCurrentAiStage(null)
                  setCurrentThinkingFull("")
                  setCurrentResponseFull("")
                  setAiThinkingExpanded(false)
                  setBatchBuildingProgress({})
                  setBatchTokensUsed(0)
                  setProcessingOsmIds([])
                }}
                className="text-white/70 hover:text-white"
              >
                <X size={16} />
              </button>
            </div>
          </div>
          <div className="px-3 py-2">
            <div className="mb-1 h-2 w-full rounded-full bg-[#ECEAE2]">
              <div
                className="h-2 rounded-full bg-[#0F6E56] transition-all"
                style={{
                  width: batchProgress.total > 0
                    ? `${Math.round(((batchProgress.processed + batchProgress.failed + batchProgress.skipped) / batchProgress.total) * 100)}%`
                    : "0%",
                }}
              />
            </div>
            <div className="mt-1 flex justify-between text-[10px] text-[#6b7280]">
              <span>{batchProgress.processed} done</span>
              <span>{batchProgress.skipped} skipped</span>
              <span>{batchProgress.failed} failed</span>
              <span>of {batchProgress.total}</span>
            </div>
            {batchTokensUsed > 0 && (
              <div className="mt-1 text-right text-[10px] text-[#6b7280]">
                {batchTokensUsed >= 1000000
                  ? `${(batchTokensUsed / 1000000).toFixed(1)}M`
                  : batchTokensUsed >= 1000
                  ? `${Math.round(batchTokensUsed / 1000)}k`
                  : batchTokensUsed} tokens used
              </div>
            )}
            {/* Live AI stage indicator — expandable */}
            {!batchDone && currentAiStage && (
              <div className="mt-2 rounded-md border border-[#c4e8d8] bg-[#f0faf6]">
                <button
                  type="button"
                  onClick={() => setAiThinkingExpanded((v) => !v)}
                  className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left"
                >
                  <Loader2 size={11} className="shrink-0 animate-spin text-[#0F6E56]" />
                  <span className="text-[10px] font-semibold uppercase text-[#0F6E56]">AI Thinking</span>
                  <span className="ml-auto text-[10px] text-[#0F6E56]">{aiThinkingExpanded ? "▾" : "▸"}</span>
                </button>
                {!aiThinkingExpanded && (
                  <div className="truncate px-2 pb-1.5 text-[10px] leading-tight text-[#17352b]">{currentAiStage.thought}</div>
                )}
                {aiThinkingExpanded && (currentThinkingFull || currentResponseFull) && (
                  <div className="max-h-64 overflow-y-auto border-t border-[#c4e8d8] px-2 py-1.5">
                    {currentThinkingFull && (
                      <pre className="whitespace-pre-wrap break-words text-[9px] leading-relaxed text-[#17352b]">{currentThinkingFull}</pre>
                    )}
                    {currentThinkingFull && currentResponseFull && (
                      <div className="my-1 border-t border-[#c4e8d8]" />
                    )}
                    {currentResponseFull && (
                      <pre className="whitespace-pre-wrap break-words text-[9px] leading-relaxed text-[#0F6E56]">{currentResponseFull}</pre>
                    )}
                  </div>
                )}
                {aiThinkingExpanded && !currentThinkingFull && !currentResponseFull && (
                  <div className="px-2 pb-1.5 text-[10px] leading-tight text-[#17352b]">{currentAiStage.thought}</div>
                )}
              </div>
            )}
            {Object.keys(batchBuildingProgress).length > 0 && (
              <div className="mt-2 space-y-1.5 rounded-md border border-[#D3D1C7] bg-[#FAFAF8] p-2">
                <div className="text-[10px] font-semibold uppercase text-[#17352b]">Individual Building Progress</div>
                <div className="max-h-32 space-y-1.5 overflow-y-auto pr-0.5">
                  {Object.values(batchBuildingProgress)
                    .sort((a, b) => b.updatedAt - a.updatedAt)
                    .slice(0, 12)
                    .map((item) => (
                      <div key={item.osm_id} className="rounded border border-[#E6E3D8] bg-white px-2 py-1.5">
                        <div className="flex items-center justify-between text-[10px]">
                          <span className="font-semibold text-[#17352b]">OSM:{item.osm_id}</span>
                          <span className="text-[#6b7280]">{Math.round(item.progressPercent)}%</span>
                        </div>
                        <div className="mt-1 h-1.5 w-full rounded-full bg-[#ECEAE2]">
                          <div
                            className={`h-1.5 rounded-full transition-all ${
                              item.status === "failed" ? "bg-red-500" : item.status === "done" ? "bg-green-600" : item.status === "skipped" ? "bg-yellow-500" : "bg-[#0F6E56]"
                            }`}
                            style={{ width: `${Math.max(0, Math.min(100, item.progressPercent))}%` }}
                          />
                        </div>
                        <div className="mt-1 text-[10px] text-[#0F6E56] uppercase">{item.stage.replaceAll("_", " ")}</div>
                        <div className="text-[10px] text-[#17352b] leading-tight">{item.thought}</div>
                        {item.totalTokens != null && item.contextWindow != null && item.contextWindow > 0 && (
                          <div className="mt-1.5">
                            <div className="mb-0.5 flex justify-between text-[9px] text-[#6b7280]">
                              <span>ctx usage</span>
                              <span>{Math.round(item.totalTokens / 1000)}k / {Math.round(item.contextWindow / 1024)}k ({Math.min(100, Math.round((item.totalTokens / item.contextWindow) * 100))}%)</span>
                            </div>
                            <div className="h-1 w-full rounded-full bg-[#ECEAE2]">
                              <div
                                className={`h-1 rounded-full transition-all ${
                                  item.totalTokens / item.contextWindow > 0.85 ? "bg-red-500" :
                                  item.totalTokens / item.contextWindow > 0.6 ? "bg-yellow-500" : "bg-[#0F6E56]"
                                }`}
                                style={{ width: `${Math.min(100, Math.round((item.totalTokens / item.contextWindow) * 100))}%` }}
                              />
                            </div>
                          </div>
                        )}
                      </div>
                    ))}
                </div>
              </div>
            )}
            <div className="mt-2 max-h-48 overflow-y-auto space-y-0.5">
              {batchProgress.events.slice(-30).map((ev, i) => (
                <div key={i} className="flex items-center gap-1.5 text-[11px] text-[#17352b]">
                  {(() => {
                    const severityClasses = getSeverityClasses(ev.severity)
                    return (
                  <span
                        className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
                          ev.status === "done"
                            ? severityClasses.dot
                            : ev.status === "failed"
                              ? "bg-red-500"
                              : "bg-yellow-400"
                        }`}
                  />
                    )
                  })()}
                  <span className="shrink-0">OSM:{ev.osm_id}</span>
                  {ev.severity != null && (
                    <span
                      className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${getSeverityClasses(ev.severity).badge}`}
                    >
                      S{ev.severity}
                    </span>
                  )}
                  {ev.chip_path && (
                    <a href={`http://localhost:8000/media/${ev.chip_path}`} target="_blank" rel="noopener noreferrer" className="text-[#0F6E56] underline" title="Post-earthquake chip">post ↗</a>
                  )}
                  {ev.pre_chip_path && (
                    <a href={`http://localhost:8000/media/${ev.pre_chip_path}`} target="_blank" rel="noopener noreferrer" className="text-blue-600 underline" title="Pre-earthquake chip">pre ↗</a>
                  )}
                  {ev.error && <span className="text-red-500 truncate" title={ev.error}>{ev.error}</span>}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Site Buildings Panel — shown when user clicks "Site" in batch panel */}
      {siteBuildingsPanel && (
        <div
          className="absolute z-30 flex flex-col rounded-lg border border-[#D3D1C7] bg-white shadow-xl"
          style={{
            top: "1rem",
            right: activeBatchId && batchProgress ? "20rem" : "1rem",
            width: "22rem",
            maxHeight: "calc(100vh - 2rem)",
          }}
        >
          {/* Header */}
          <div className="flex shrink-0 items-center justify-between rounded-t-lg bg-[#0F6E56] px-3 py-2">
            <div className="min-w-0">
              <div className="truncate text-sm font-bold text-white">{siteBuildingsPanel.site_name}</div>
              <div className="text-[10px] text-white/70">
                {siteBuildingsPanel.assessed}/{siteBuildingsPanel.total} buildings assessed
              </div>
            </div>
            <button
              type="button"
              onClick={() => setSiteBuildingsPanel(null)}
              className="ml-2 shrink-0 text-white/70 hover:text-white"
            >
              <X size={16} />
            </button>
          </div>

          {/* Progress bar */}
          <div className="shrink-0 border-b border-[#D3D1C7] px-3 py-2">
            <div className="mb-1 h-1.5 w-full rounded-full bg-[#ECEAE2]">
              <div
                className="h-1.5 rounded-full bg-[#0F6E56] transition-all"
                style={{
                  width: siteBuildingsPanel.total > 0
                    ? `${Math.round((siteBuildingsPanel.assessed / siteBuildingsPanel.total) * 100)}%`
                    : "0%",
                }}
              />
            </div>
            <div className="flex justify-between text-[10px] text-[#6b7280]">
              <span>{siteBuildingsPanel.assessed} assessed</span>
              <span>{siteBuildingsPanel.total - siteBuildingsPanel.assessed} pending</span>
              <span>{siteBuildingsPanel.total} total</span>
            </div>
          </div>

          {/* Building list */}
          <div className="flex-1 overflow-y-auto">
            {siteBuildingsPanel.buildings.length === 0 ? (
              <div className="px-3 py-4 text-center text-xs text-[#6b7280]">No buildings found in site boundary.</div>
            ) : (
              <div className="divide-y divide-[#F3F1E9]">
                {siteBuildingsPanel.buildings.map((bldg) => {
                  const sev = bldg.severity
                  const sevClasses = getSeverityClasses(sev ?? 0)
                  const assessed = bldg.assessment_id !== null
                  return (
                    <button
                      key={bldg.osm_id}
                      type="button"
                      className="flex w-full items-start gap-2 px-3 py-2 text-left hover:bg-[#F9F9F7]"
                      onClick={() => {
                        const map = mapInstanceRef.current
                        if (map) map.flyTo({ center: [bldg.centroid_lon, bldg.centroid_lat], zoom: 18, duration: 800 })
                      }}
                      title={`OSM:${bldg.osm_id} — click to fly to`}
                    >
                      {/* severity dot */}
                      <span
                        className={`mt-0.5 inline-block h-2.5 w-2.5 shrink-0 rounded-full ${assessed ? sevClasses.dot : "bg-[#D3D1C7]"}`}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-1">
                          <span className="text-xs font-semibold text-[#17352b]">OSM:{bldg.osm_id}</span>
                          {assessed && sev != null && (
                            <span className={`shrink-0 rounded border px-1.5 py-0.5 text-[10px] font-semibold ${sevClasses.badge}`}>
                              S{sev}
                            </span>
                          )}
                          {!assessed && (
                            <span className="shrink-0 rounded border border-[#D3D1C7] px-1.5 py-0.5 text-[10px] text-[#6b7280]">
                              pending
                            </span>
                          )}
                        </div>
                        {bldg.damage_type && (
                          <div className="text-[10px] capitalize text-[#6b7280]">{bldg.damage_type.replaceAll("_", " ")}</div>
                        )}
                        {bldg.assessment_id && (
                          <div className="text-[10px] text-[#9a9a8e]">{bldg.assessment_id}</div>
                        )}
                      </div>
                    </button>
                  )
                })}
              </div>
            )}
          </div>

          {/* Footer: refresh */}
          <div className="shrink-0 border-t border-[#D3D1C7] px-3 py-2">
            <button
              type="button"
              onClick={() => void openSiteBuildingsPanel(siteBuildingsPanel.site_name)}
              disabled={siteBuildingsPanelLoading}
              className="flex w-full items-center justify-center gap-1.5 rounded-md border border-[#D3D1C7] bg-[#FAFAF8] py-1.5 text-[11px] font-semibold text-[#0F6E56] hover:bg-[#E1F5EE] disabled:opacity-50"
            >
              {siteBuildingsPanelLoading ? <Loader2 size={11} className="animate-spin" /> : null}
              Refresh
            </button>
          </div>
        </div>
      )}

      {/* Single building analysis drawer */}
      {buildingDrawer && (
        <div className="absolute right-4 top-4 z-30 w-72 rounded-lg border border-[#D3D1C7] bg-white shadow-xl">
          <div className="flex items-center justify-between rounded-t-lg bg-[#0F6E56] px-3 py-2">
            <span className="text-sm font-bold text-white">
              Analysing OSM:{buildingDrawer.osmId}
            </span>
            <div className="flex items-center gap-2">
              {!buildingDrawer.done && buildingDrawer.batchId && (
                <button
                  type="button"
                  onClick={() => void handleStopSingleBuildingAnalysis()}
                  className="rounded border border-red-200 bg-white/15 px-2 py-0.5 text-[10px] font-semibold text-white hover:bg-white/25"
                >
                  Stop
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  if (singleBuildingStreamCleanupRef.current) {
                    singleBuildingStreamCleanupRef.current()
                    singleBuildingStreamCleanupRef.current = null
                  }
                  setSingleWasStopped(false)
                  setProcessingOsmIds((prev) => prev.filter((id) => id !== buildingDrawer.osmId))
                  setBuildingDrawer(null)
                }}
                className="text-white/70 hover:text-white"
              >
                <X size={16} />
              </button>
            </div>
          </div>
          <div className="px-3 py-3">
            <div className="mb-2">
              <div className="mb-1 h-2 w-full rounded-full bg-[#ECEAE2]">
                <div
                  className="h-2 rounded-full bg-[#0F6E56] transition-all"
                  style={{ width: `${Math.max(0, Math.min(100, buildingDrawer.progressPercent))}%` }}
                />
              </div>
              <div className="flex items-center justify-between text-[10px] text-[#6b7280]">
                <span>{buildingDrawer.progressLabel ?? "Processing"}</span>
                <span>{Math.round(buildingDrawer.progressPercent)}%</span>
              </div>
            </div>
            {!buildingDrawer.done && !buildingDrawer.batchId && (
              <div className="flex items-center gap-2 text-xs text-[#17352b]">
                <Loader2 size={12} className="animate-spin" />
                Starting analysis…
              </div>
            )}
            {buildingDrawer.batchId && !buildingDrawer.done && !buildingDrawer.aiThought && (
              <div className="flex items-center gap-2 text-xs text-[#17352b]">
                <Loader2 size={12} className="animate-spin" />
                Clipping chip and running Gemma…
              </div>
            )}
            {buildingDrawer.batchId && !buildingDrawer.done && buildingDrawer.aiThought && (
              <div className="flex items-start gap-1.5 rounded-md bg-[#f0faf6] border border-[#c4e8d8] px-2 py-1.5 text-[11px]">
                <Loader2 size={11} className="mt-0.5 shrink-0 animate-spin text-[#0F6E56]" />
                <div className="min-w-0">
                  <div className="text-[10px] font-semibold uppercase text-[#0F6E56]">AI Thinking</div>
                  <span className="text-[#17352b] leading-tight">{buildingDrawer.aiThought}</span>
                </div>
              </div>
            )}
            {buildingDrawer.events.map((ev, i) => (
              <div key={i} className="mt-2 rounded-md border border-[#D3D1C7] px-2.5 py-2 text-xs text-[#17352b]">
                {ev.status === "done" ? (
                  <>
                    <div className="font-semibold text-green-700">Assessment complete</div>
                    {ev.assessment_id && <div className="mt-0.5 text-[#6b7280]">ID: {ev.assessment_id}</div>}
                    {ev.severity != null && (
                      <div className="mt-0.5">
                        Severity:{" "}
                        <span className={`font-bold ${getSeverityClasses(ev.severity).text}`}>
                          {ev.severity}
                        </span>
                      </div>
                    )}
                    <div className="mt-1.5 flex flex-col gap-1">
                      {ev.chip_path && (
                        <a
                          href={`http://localhost:8000/media/${ev.chip_path}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-[#0F6E56] underline"
                        >
                          📸 Post-earthquake chip ↗
                        </a>
                      )}
                      {ev.pre_chip_path && (
                        <a
                          href={`http://localhost:8000/media/${ev.pre_chip_path}`}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-[#0F6E56] underline"
                        >
                          🕐 Pre-earthquake chip ↗
                        </a>
                      )}
                    </div>
                    {ev.assessment_id && (
                      <button
                        type="button"
                        onClick={() => void handleShowAssessmentOnMap(ev.assessment_id as string, buildingDrawer.lat, buildingDrawer.lon)}
                        className="mt-2 w-full rounded-md border border-[#0F6E56] bg-[#0F6E56] px-2.5 py-1.5 text-xs font-semibold text-white hover:bg-[#0C614D]"
                      >
                        Show in Map
                      </button>
                    )}
                  </>
                ) : (
                  <div className="text-red-600">{ev.error ?? "Failed"}</div>
                )}
              </div>
            ))}
            {buildingDrawer.done && buildingDrawer.events.length === 0 && (
              <div className="text-xs text-[#6b7280]">No buildings found at this location in the orthophoto.</div>
            )}
          </div>
        </div>
      )}

      {/* Batch configuration modal */}
      {batchModalOpen && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/40">
          <div className="w-96 rounded-xl border border-[#D3D1C7] bg-white shadow-2xl">
            <div className="flex items-center justify-between rounded-t-xl bg-[#0F6E56] px-4 py-3">
              <span className="text-sm font-bold text-white">Start Batch Analysis</span>
              <button
                type="button"
                onClick={() => { setBatchModalOpen(false); cancelDrawMode() }}
                className="text-white/70 hover:text-white"
              >
                <X size={18} />
              </button>
            </div>
            <div className="space-y-3 px-4 py-4">
              <div>
                <label className="mb-1 block text-xs font-semibold text-[#17352b]">
                  Post-earthquake Upload ID <span className="font-normal text-zinc-400">(optional)</span>
                </label>
                <input
                  type="text"
                  value={batchUploadId}
                  onChange={(e) => setBatchUploadId(e.target.value)}
                  placeholder="Auto-detect by location if empty"
                  className="w-full rounded-md border border-[#D3D1C7] px-3 py-2 text-sm text-[#17352b] focus:outline-none focus:ring-2 focus:ring-[#0F6E56]"
                />
                <p className="mt-1 text-[10px] text-zinc-400">
                  Leave empty to auto-detect. If all buildings are skipped with &quot;no coverage&quot;, paste the upload ID from the Data panel here.
                </p>
              </div>

              {/* Site picker */}
              <div>
                <label className="mb-1.5 block text-xs font-semibold text-[#17352b]">
                  Site Name <span className="text-red-500">*</span>
                </label>
                {sitesLoading ? (
                  <div className="flex items-center gap-2 py-1 text-xs text-[#6b7280]">
                    <Loader2 size={12} className="animate-spin" /> Loading existing sites…
                  </div>
                ) : (
                  <div className="space-y-1 rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2 py-2 max-h-32 overflow-y-auto">
                    {existingSites.map((site) => (
                      <label key={site} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 hover:bg-[#ECEAE2]">
                        <input
                          type="radio"
                          name="batch-site"
                          value={site}
                          checked={!useNewSite && selectedExistingSite === site}
                          onChange={() => { setSelectedExistingSite(site); setUseNewSite(false) }}
                          className="accent-[#0F6E56]"
                        />
                        <span className="text-xs text-[#17352b]">{site}</span>
                      </label>
                    ))}
                    <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 hover:bg-[#ECEAE2]">
                      <input
                        type="radio"
                        name="batch-site"
                        value="__new__"
                        checked={useNewSite}
                        onChange={() => { setUseNewSite(true); setSelectedExistingSite("") }}
                        className="accent-[#0F6E56]"
                      />
                      <span className="text-xs font-semibold text-[#0F6E56]">+ New site…</span>
                    </label>
                  </div>
                )}
                {useNewSite && (
                  <input
                    type="text"
                    value={newSiteNameInput}
                    onChange={(e) => setNewSiteNameInput(e.target.value)}
                    placeholder="e.g. Antakya Ward 3"
                    autoFocus
                    className="mt-1.5 w-full rounded-md border border-[#0F6E56] px-3 py-2 text-sm text-[#17352b] focus:outline-none focus:ring-2 focus:ring-[#0F6E56]"
                  />
                )}
              </div>

              <div>
                <label className="mb-1 block text-xs font-semibold text-[#17352b]">Team Name (optional)</label>
                <input
                  type="text"
                  value={batchWorkerName}
                  onChange={(e) => setBatchWorkerName(e.target.value)}
                  placeholder="e.g. Team Alpha"
                  className="w-full rounded-md border border-[#D3D1C7] px-3 py-2 text-sm text-[#17352b] focus:outline-none focus:ring-2 focus:ring-[#0F6E56]"
                />
              </div>
              <div className="rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-3 py-2 text-xs text-[#6b7280]">
                {drawPoints.length} polygon points drawn
              </div>
              <button
                type="button"
                disabled={batchSubmitting}
                onClick={() => void submitBatch()}
                className="flex w-full items-center justify-center gap-2 rounded-md bg-[#0F6E56] px-4 py-2.5 text-sm font-semibold text-white hover:bg-[#0C614D] disabled:opacity-60"
              >
                {batchSubmitting ? <Loader2 size={14} className="animate-spin" /> : <ChevronRight size={14} />}
                {batchSubmitting ? "Starting…" : "Start Batch Analysis"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Single-building site picker modal */}
      {singleBldgPickerOpen && (
        <div className="absolute inset-0 z-40 flex items-center justify-center bg-black/40">
          <div className="w-80 rounded-xl border border-[#D3D1C7] bg-white shadow-2xl">
            <div className="flex items-center justify-between rounded-t-xl bg-[#0F6E56] px-4 py-3">
              <span className="text-sm font-bold text-white">
                Analyse OSM:{pendingBldgRef.current?.osmId}
              </span>
              <button
                type="button"
                onClick={() => { setSingleBldgPickerOpen(false); pendingBldgRef.current = null }}
                className="text-white/70 hover:text-white"
              >
                <X size={18} />
              </button>
            </div>
            <div className="space-y-3 px-4 py-4">
              <p className="text-xs text-[#6b7280]">
                Choose a site to file this assessment under, or create a new one.
              </p>

              {/* Site picker */}
              <div>
                <label className="mb-1.5 block text-xs font-semibold text-[#17352b]">
                  Site Name <span className="text-red-500">*</span>
                </label>
                {sitesLoading ? (
                  <div className="flex items-center gap-2 py-1 text-xs text-[#6b7280]">
                    <Loader2 size={12} className="animate-spin" /> Loading existing sites…
                  </div>
                ) : (
                  <div className="space-y-1 rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2 py-2 max-h-40 overflow-y-auto">
                    {existingSites.map((site) => (
                      <label key={site} className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 hover:bg-[#ECEAE2]">
                        <input
                          type="radio"
                          name="single-site"
                          value={site}
                          checked={!useNewSite && selectedExistingSite === site}
                          onChange={() => { setSelectedExistingSite(site); setUseNewSite(false) }}
                          className="accent-[#0F6E56]"
                        />
                        <span className="text-xs text-[#17352b]">{site}</span>
                      </label>
                    ))}
                    <label className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 hover:bg-[#ECEAE2]">
                      <input
                        type="radio"
                        name="single-site"
                        value="__new__"
                        checked={useNewSite}
                        onChange={() => { setUseNewSite(true); setSelectedExistingSite("") }}
                        className="accent-[#0F6E56]"
                      />
                      <span className="text-xs font-semibold text-[#0F6E56]">+ New site…</span>
                    </label>
                  </div>
                )}
                {useNewSite && (
                  <input
                    type="text"
                    value={newSiteNameInput}
                    onChange={(e) => setNewSiteNameInput(e.target.value)}
                    placeholder="e.g. Antakya Ward 3"
                    autoFocus
                    className="mt-1.5 w-full rounded-md border border-[#0F6E56] px-3 py-2 text-sm text-[#17352b] focus:outline-none focus:ring-2 focus:ring-[#0F6E56]"
                  />
                )}
              </div>

              <button
                type="button"
                onClick={() => void confirmSingleBldgAnalysis()}
                className="flex w-full items-center justify-center gap-2 rounded-md bg-[#0F6E56] px-4 py-2.5 text-sm font-semibold text-white hover:bg-[#0C614D]"
              >
                <ChevronRight size={14} />
                Analyse Building
              </button>
            </div>
          </div>
        </div>
      )}

      <aside
        className="absolute bottom-0 left-0 right-0 z-20 border-t border-[#D3D1C7] bg-[#FAFAF8]/95 px-4 py-3 shadow-lg backdrop-blur-sm transition-all duration-300"
        style={{
          marginLeft: isChatSidebarOpen ? "24rem" : "0",
        }}
      >
        <div className="flex items-center justify-center">
          <div className="flex flex-wrap items-center justify-center gap-3">
            {overlayOrder.map((layerKey, orderIndex) => {
              const config = overlayLayerConfigs.find((item) => item.key === layerKey)
              if (!config) {
                return null
              }
              const layerState = overlayStates[config.key]
              return (
                <div
                  key={config.key}
                  className="flex items-center gap-2 rounded-md border border-[#D3D1C7] bg-white px-2 py-1.5"
                >
                  <div className="flex items-center gap-0.5">
                    <button
                      type="button"
                      aria-label={`Move ${config.label} left`}
                      disabled={orderIndex === 0}
                      onClick={() => handleMoveOverlayRank(config.key, "up")}
                      className="rounded border border-[#D3D1C7] px-1 text-[9px] leading-3 text-[#17352b] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      ◀
                    </button>
                    <button
                      type="button"
                      aria-label={`Move ${config.label} right`}
                      disabled={orderIndex === overlayOrder.length - 1}
                      onClick={() => handleMoveOverlayRank(config.key, "down")}
                      className="rounded border border-[#D3D1C7] px-1 text-[9px] leading-3 text-[#17352b] disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      ▶
                    </button>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleToggleOverlay(config.key)}
                    className="flex items-center gap-2 rounded-md bg-transparent px-1 py-0.5 transition-all hover:text-[#0F6E56]"
                  >
                  <div
                    className="flex h-4 w-7 items-center rounded-full border border-[#D3D1C7] transition-colors"
                    style={{
                      backgroundColor: layerState.visible ? config.color : "#E5E7EB",
                    }}
                  >
                    <div
                      className="h-3 w-3 rounded-full bg-white shadow-sm transition-transform"
                      style={{
                        transform: layerState.visible ? "translateX(0.75rem)" : "translateX(0.125rem)",
                      }}
                    />
                  </div>
                  <span className="flex items-center gap-1.5 text-xs font-medium text-[#17352b]">
                    <config.icon size={14} color={config.color} />
                    {config.label}
                    <span className="text-[10px] text-[#6b7280]">
                      {layerState.isLoading ? (
                        <span className="inline-flex items-center" aria-label="Loading layer data">
                          <span className="h-2.5 w-2.5 animate-spin rounded-full border border-[#6b7280] border-t-transparent" />
                        </span>
                      ) : layerState.error ? (
                        "!"
                      ) : (
                        `(${layerState.featureCount})`
                      )}
                    </span>
                  </span>
                  </button>
                </div>
              )
            })}

          </div>
        </div>
      </aside>

      <FieldMapChatSidebar
        isOpen={isChatSidebarOpen}
        onOpenChange={setIsChatSidebarOpen}
        onToolResult={handleChatToolResult}
        selectedBuildingContext={selectedBuildingChatContext}
        onClearSelectedBuildingContext={() => setSelectedBuildingChatContext(null)}
        activeBatch={
          // Individual building analysis takes priority over site batch
          buildingDrawer
            ? {
                batchId: buildingDrawer.batchId ?? "",
                siteName: `OSM:${buildingDrawer.osmId}`,
                total: 1,
                processed: buildingDrawer.events.filter((e) => e.status === "done").length,
                failed: buildingDrawer.events.filter((e) => e.status === "failed").length,
                skipped: buildingDrawer.events.filter((e) => e.status === "skipped").length,
                done: buildingDrawer.done,
                stopped: singleWasStopped,
                tokensUsed: buildingDrawer.tokensUsed,
                events: buildingDrawer.events.map((e) => ({
                  osm_id: e.osm_id,
                  status: e.status as "done" | "skipped" | "failed",
                  severity: e.severity,
                  error: e.error,
                })),
                currentOsmId: buildingDrawer.done ? null : buildingDrawer.osmId,
                currentStage: buildingDrawer.aiStage ?? "",
                currentThought: buildingDrawer.aiThought ?? "",
              }
            : activeBatchId && batchProgress
            ? {
                batchId: activeBatchId,
                siteName: activeBatchSiteName,
                total: batchProgress.total,
                processed: batchProgress.processed,
                failed: batchProgress.failed,
                skipped: batchProgress.skipped,
                done: batchDone,
                stopped: batchWasStopped,
                tokensUsed: batchTokensUsed,
                events: batchProgress.events.map((e) => ({
                  osm_id: e.osm_id,
                  status: e.status as "done" | "skipped" | "failed",
                  severity: e.severity,
                  error: e.error,
                })),
                currentOsmId: currentAiStage?.osm_id ?? null,
                currentStage: currentAiStage?.stage ?? "",
                currentThought: currentThinkingFull || currentAiStage?.thought || "",
              }
            : null
        }
      />

      <FeatureInfoSidebar
        info={selectedFeatureInfo}
        onClose={handleCloseFeatureInfoSidebar}
        onAnalyseBuilding={handleAnalyseBuilding}
        onAskAiAboutBuilding={handleAskAiAboutBuilding}
      />

      <AiOverlayInfoPanel
        info={selectedAiOverlay}
        onClose={() => setSelectedAiOverlay(null)}
      />
    </div>
  )
}
