"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import maplibregl from "maplibre-gl"
import { PMTiles, Protocol } from "pmtiles"
import { Map, MapPin, Route, MapPinned, Building, Droplets, AlertTriangle, Mountain, SatelliteDish, ImageIcon, X, Loader2, ChevronRight } from "lucide-react"
import { toast } from "sonner"
import { FieldMapChatSidebar } from "@/components/maps/field-map-chat-sidebar"
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
  startOrthophotoBatch,
  subscribeBatchStream,
  type PendingBatchRecord,
  type BatchSseEvent,
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

// This component renders the PMTiles basemap with a single-button AI chat sidebar.
export function FieldMapView() {
  // This variable references the map container element for MapLibre mount.
  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapInstanceRef = useRef<maplibregl.Map | null>(null)
  const overlayLoadedRef = useRef<Set<GisLayerKey>>(new Set())
  // This variable caches loaded overlay GeoJSON so feature lookup can power custom map actions.
  const overlayGeoJsonRef = useRef<Partial<Record<GisLayerKey, GeoJsonFeatureCollection>>>({})
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

  // This variable tracks currently selected feature for highlighting.
  const selectedFeatureRef = useRef<{ source: string; id: string | number } | null>(null)
  const popupRef = useRef<maplibregl.Popup | null>(null)

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
  type BuildingLiveProgress = { osm_id: number; progressPercent: number; stage: string; thought: string; status: string; updatedAt: number }
  const [activeBatchId, setActiveBatchId] = useState<string | null>(null)
  const [batchProgress, setBatchProgress] = useState<{
    total: number; processed: number; failed: number; skipped: number; events: BuildingEvent[]
  } | null>(null)
  const [batchDone, setBatchDone] = useState(false)
  const [currentAiStage, setCurrentAiStage] = useState<AiStageEvent | null>(null)
  const [batchBuildingProgress, setBatchBuildingProgress] = useState<Record<number, BuildingLiveProgress>>({})
  const [processingOsmIds, setProcessingOsmIds] = useState<number[]>([])
  const [processingBlinkOn, setProcessingBlinkOn] = useState(false)
  const [batchWasStopped, setBatchWasStopped] = useState(false)
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
    events: BuildingEvent[]; done: boolean; aiThought?: string; aiStage?: string; progressPercent: number; progressLabel?: string
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
      setBatchProgress({ total: 0, processed: 0, failed: 0, skipped: 0, events: [] })
      setBatchDone(false)
      setBatchWasStopped(false)
      setBatchBuildingProgress({})
      setProcessingOsmIds([])

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
                  thought: "Building skipped (already assessed).",
                  status: "skipped",
                  updatedAt: Date.now(),
                },
              }))
              return {
                ...base,
                skipped: base.skipped + 1,
                events: [...base.events, { osm_id: osmId, status: "skipped" }],
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
      setBatchProgress({ total: 0, processed: 0, failed: 0, skipped: 0, events: [] })
      setBatchDone(false)
      setBatchWasStopped(false)
      setBatchBuildingProgress({})
      setProcessingOsmIds([])

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
            setProcessingOsmIds((prev) => (prev.includes(osmId) ? prev : [...prev, osmId]))
            setCurrentAiStage({
              osm_id: osmId,
              stage: ev.stage as string,
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
                stage: String(ev.stage ?? "tool_call"),
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
              return { ...base, total: (ev.total_buildings as number) ?? 0 }
            }
            if (ev.type === "building_done") {
              scheduleAssessmentsLayerRefresh()
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
                  thought: "Building skipped (already assessed).",
                  status: "skipped",
                  updatedAt: Date.now(),
                },
              }))
              return { ...base, skipped: base.skipped + 1, events: [...base.events, { osm_id: ev.osm_id as number, status: "skipped" }] }
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

  // This function handles the Analyse button click from a building popup — shows site picker first.
  const handleAnalyseBuilding = useCallback(async (osmId: number, lat: number, lon: number) => {
    // Close popup and clear the map selection so no page refresh is needed.
    if (popupRef.current) {
      popupRef.current.remove()
      popupRef.current = null
    }
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

  // This function runs after site is confirmed in the single-building picker.
  const confirmSingleBldgAnalysis = useCallback(async () => {
    const pending = pendingBldgRef.current
    if (!pending) return
    const resolvedSiteName = useNewSite ? newSiteNameInput.trim() : selectedExistingSite.trim()
    if (!resolvedSiteName) {
      toast.error("Choose or enter a site name")
      return
    }
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
    })
    setSingleWasStopped(false)
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
        setBatchDone(false)
        setBatchWasStopped(false)
        setBatchProgress({
          total: Math.max(0, Number(status.total_buildings) || 0),
          processed: Math.max(0, Number(status.processed) || 0),
          failed: Math.max(0, Number(status.failed) || 0),
          skipped: Math.max(0, Number(status.skipped) || 0),
          events: [],
        })
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
                    thought: "Building skipped (already assessed).",
                    status: "skipped",
                    updatedAt: Date.now(),
                  },
                }))
                return { ...base, skipped: base.skipped + 1, events: [...base.events, { osm_id: ev.osm_id as number, status: "skipped" }] }
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
          return {
            ...base,
            total: Math.max(0, Number(status.total_buildings) || 0),
            processed: Math.max(0, Number(status.processed) || 0),
            failed: Math.max(0, Number(status.failed) || 0),
            skipped: Math.max(0, Number(status.skipped) || 0),
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
    void syncStatus()
    const timer = window.setInterval(() => { void syncStatus() }, 3000)
    return () => {
      isMounted = false
      window.clearInterval(timer)
    }
  }, [activeBatchId, batchDone])

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
        setTimeout(() => {
          map.flyTo({
            center: [38.268983, 37.763058],
            zoom: 13,
            duration: 2500,
            essential: true,
          })
        }, 500)
      })

      // This function creates HTML for popup card from feature properties.
      const createPopupContent = (
        properties: Record<string, unknown>,
        layerKey: GisLayerKey,
        layerLabel: string,
        latitude: string,
        longitude: string
      ): string => {
        if (layerKey === "assessments") {
          // This variable stores selected assessment severity for popup highlighting.
          const severity = String(properties.severity ?? "-")
          // This variable stores selected assessment status text.
          const status = String(properties.status ?? "unknown")
          // This variable stores selected assessment damage type text.
          const damageType = String(properties.damage_type ?? "unknown").replaceAll("_", " ")
          // This variable stores selected assessment structural risk text.
          const structuralRisk = String(properties.structural_risk ?? "unknown")
          // This variable stores selected assessment action guidance text.
          const recommendation = String(properties.recommended_action ?? "not available").replaceAll("_", " ")
          // This variable stores selected assessment source/input type text.
          const inputType = String(properties.input_type ?? "unknown").replaceAll("_", " ")
          // This variable stores selected assessment timestamp text.
          const createdAt = String(properties.created_at ?? "").replace("T", " ").replace("Z", "")
          // This variable stores selected assessment id text.
          const assessmentId = String(properties.id ?? "-")
          const postChipPath = String(properties.chip_path ?? properties.photo_path ?? "").trim()
          const preChipPath = String(properties.pre_chip_path ?? "").trim()
          const mediaLinks = `
            <div class="rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2.5 py-2">
              <div class="text-[10px] uppercase tracking-wide text-[#6b7280]">Orthophoto Images</div>
              <div class="mt-1 flex items-center gap-3">
                ${
                  postChipPath
                    ? `<a href="http://localhost:8000/media/${postChipPath}" target="_blank" rel="noopener noreferrer" class="text-[11px] font-semibold text-[#0F6E56] underline">post ↗</a>`
                    : `<span class="text-[11px] text-[#9ca3af]">post -</span>`
                }
                ${
                  preChipPath
                    ? `<a href="http://localhost:8000/media/${preChipPath}" target="_blank" rel="noopener noreferrer" class="text-[11px] font-semibold text-[#2563eb] underline">pre ↗</a>`
                    : `<span class="text-[11px] text-[#9ca3af]">pre -</span>`
                }
              </div>
            </div>
          `

          return `
            <div class="min-w-[260px] max-w-[360px] overflow-hidden rounded-lg border border-[#D3D1C7] bg-white">
              <div class="flex items-center justify-between bg-[#0F6E56] px-3 py-2">
                <h3 class="text-sm font-bold text-white">${layerLabel}</h3>
                <span class="rounded bg-white/20 px-2 py-0.5 text-[10px] font-semibold uppercase text-white">Severity ${severity}</span>
              </div>
              <div class="space-y-2 px-3 py-3 text-xs text-[#17352b]">
                <div class="rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2.5 py-2">
                  <div class="text-[10px] uppercase tracking-wide text-[#6b7280]">Assessment ID</div>
                  <div class="mt-0.5 font-semibold">${assessmentId}</div>
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="rounded-md border border-[#D3D1C7] px-2.5 py-2"><div class="text-[10px] uppercase text-[#6b7280]">Damage</div><div class="mt-0.5 font-semibold capitalize">${damageType}</div></div>
                  <div class="rounded-md border border-[#D3D1C7] px-2.5 py-2"><div class="text-[10px] uppercase text-[#6b7280]">Risk</div><div class="mt-0.5 font-semibold capitalize">${structuralRisk}</div></div>
                </div>
                <div class="grid grid-cols-2 gap-2">
                  <div class="rounded-md border border-[#D3D1C7] px-2.5 py-2"><div class="text-[10px] uppercase text-[#6b7280]">Status</div><div class="mt-0.5 font-semibold capitalize">${status}</div></div>
                  <div class="rounded-md border border-[#D3D1C7] px-2.5 py-2"><div class="text-[10px] uppercase text-[#6b7280]">Input</div><div class="mt-0.5 font-semibold capitalize">${inputType}</div></div>
                </div>
                <div class="rounded-md border border-[#D3D1C7] px-2.5 py-2">
                  <div class="text-[10px] uppercase text-[#6b7280]">Recommended Action</div>
                  <div class="mt-0.5 font-semibold capitalize">${recommendation}</div>
                </div>
                ${mediaLinks}
                <div class="flex items-center justify-between rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2.5 py-2">
                  <div>
                    <div class="text-[10px] uppercase tracking-wide text-[#6b7280]">Location</div>
                    <div class="font-semibold">${latitude}, ${longitude}</div>
                  </div>
                  <button
                    type="button"
                    data-copy-location="${latitude}, ${longitude}"
                    class="rounded-md border border-[#D3D1C7] bg-white px-2 py-1 text-[10px] font-semibold text-[#0F6E56] hover:bg-[#ECEAE2]"
                  >
                    Copy
                  </button>
                </div>
                <div class="text-[10px] text-[#6b7280]">Created: ${createdAt || "-"}</div>
              </div>
            </div>
          `
        }

        const entries = Object.entries(properties).filter(
          ([key, value]) => value !== null && value !== undefined && value !== "" && key !== "id"
        )

        const rows = entries
          .map(
            ([key, value]) => `
            <div class="flex justify-between gap-4 py-1.5 border-b border-gray-100 last:border-0">
              <span class="text-xs font-medium text-gray-600 capitalize">${key.replace(/_/g, " ")}</span>
              <span class="text-xs text-gray-900 font-semibold">${String(value)}</span>
            </div>
          `
          )
          .join("")

        const detailsSection =
          rows.length > 0
            ? rows
            : `<div class="py-2 text-xs text-gray-500">No additional data available.</div>`

        // Show Analyse button for turkey_buildings layer.
        const osmId = properties.osm_id ?? properties.id ?? ""
        const analyseButton =
          layerKey === "turkey_buildings"
            ? `<button
                type="button"
                data-analyse-building="${osmId}"
                data-lat="${latitude}"
                data-lon="${longitude}"
                class="mt-2 w-full rounded-md border border-[#0F6E56] bg-[#0F6E56] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#0C614D]"
              >
                Analyse Building
              </button>`
            : ""

        return `
          <div class="min-w-[250px] max-w-[350px]">
            <div class="bg-[#0F6E56] px-3 py-2 rounded-t-lg">
              <h3 class="text-sm font-bold text-white">${layerLabel}</h3>
            </div>
            <div class="bg-white px-3 py-2 max-h-[300px] overflow-y-auto">
              <div class="flex items-center justify-between gap-2 py-2 border-b border-gray-200">
                <div class="flex flex-col">
                  <span class="text-[10px] font-medium uppercase tracking-wide text-gray-500">Location</span>
                  <span class="text-xs font-semibold text-gray-900">${latitude}, ${longitude}</span>
                </div>
                <button
                  type="button"
                  data-copy-location="${latitude}, ${longitude}"
                  class="rounded-md border border-[#D3D1C7] bg-[#F7F6F2] px-2 py-1 text-[10px] font-semibold text-[#0F6E56] hover:bg-[#ECEAE2]"
                >
                  Copy
                </button>
              </div>
              ${detailsSection}
              ${analyseButton}
            </div>
          </div>
        `
      }

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
        const popupLat = Number(properties.lat ?? fallbackLat)
        const popupLon = Number(properties.lon ?? fallbackLon)
        const latitude = popupLat.toFixed(6)
        const longitude = popupLon.toFixed(6)
        const locationText = `${latitude}, ${longitude}`
        const popupContent = createPopupContent(properties, "assessments", "Assessments", latitude, longitude)

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
          center: [popupLon, popupLat],
          zoom: Math.max(map.getZoom(), 17),
          essential: true,
        })

        if (popupRef.current) {
          popupRef.current.remove()
        }

        const popup = new maplibregl.Popup({
          closeButton: true,
          closeOnClick: false,
          maxWidth: "400px",
          className: "custom-popup",
        })
          .setLngLat([popupLon, popupLat])
          .setHTML(popupContent)
          .addTo(map)

        const copyButton = popup.getElement().querySelector<HTMLButtonElement>("[data-copy-location]")
        if (copyButton) {
          copyButton.addEventListener("click", async (event) => {
            event.preventDefault()
            event.stopPropagation()
            try {
              await navigator.clipboard.writeText(locationText)
              copyButton.textContent = "Copied"
            } catch {
              copyButton.textContent = "Error"
            }

            window.setTimeout(() => {
              copyButton.textContent = "Copy"
            }, 1200)
          })
        }

        popup.on("close", () => {
          if (selectedFeatureRef.current && mapInstanceRef.current) {
            mapInstanceRef.current.setFeatureState(
              { source: selectedFeatureRef.current.source, id: selectedFeatureRef.current.id },
              { selected: false }
            )
            selectedFeatureRef.current = null
          }
        })

        popupRef.current = popup
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

        // Create and show popup
        const latitude = e.lngLat.lat.toFixed(6)
        const longitude = e.lngLat.lng.toFixed(6)
        const locationText = `${latitude}, ${longitude}`
        const popupContent = createPopupContent(feature.properties || {}, layerKey, layerLabel, latitude, longitude)

        // Remove existing popup
        if (popupRef.current) {
          popupRef.current.remove()
        }

        const popup = new maplibregl.Popup({
          closeButton: true,
          closeOnClick: false,
          maxWidth: "400px",
          className: "custom-popup",
        })
          .setLngLat(e.lngLat)
          .setHTML(popupContent)
          .addTo(map)

        const copyButton = popup.getElement().querySelector<HTMLButtonElement>("[data-copy-location]")
        if (copyButton) {
          copyButton.addEventListener("click", async (event) => {
            event.preventDefault()
            event.stopPropagation()
            try {
              await navigator.clipboard.writeText(locationText)
              copyButton.textContent = "Copied"
            } catch {
              copyButton.textContent = "Error"
            }

            window.setTimeout(() => {
              copyButton.textContent = "Copy"
            }, 1200)
          })
        }

        const analyseBtn = popup.getElement().querySelector<HTMLButtonElement>("[data-analyse-building]")
        if (analyseBtn) {
          analyseBtn.addEventListener("click", (event) => {
            event.preventDefault()
            event.stopPropagation()
            const osmId = Number(analyseBtn.dataset.analyseBuilding)
            const lat = parseFloat(analyseBtn.dataset.lat ?? "0")
            const lon = parseFloat(analyseBtn.dataset.lon ?? "0")
            void handleAnalyseBuilding(osmId, lat, lon)
          })
        }

        popup.on("close", () => {
          if (selectedFeatureRef.current && mapInstanceRef.current) {
            mapInstanceRef.current.setFeatureState(
              { source: selectedFeatureRef.current.source, id: selectedFeatureRef.current.id },
              { selected: false }
            )
            selectedFeatureRef.current = null
          }
        })

        popupRef.current = popup
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
      showAssessmentOnMapRef.current = null
      if (mapInstance) {
        mapInstance.remove()
      }
    }
  }, [])

  return (
    <div className="relative flex h-full w-full flex-col overflow-hidden">
      <style jsx global>{`
        .custom-popup .maplibregl-popup-close-button {
          width: 32px !important;
          height: 32px !important;
          font-size: 24px !important;
          line-height: 32px !important;
          padding: 0 !important;
          border-radius: 4px;
          right: 4px;
          top: 4px;
        }
        .custom-popup .maplibregl-popup-close-button:hover {
          background-color: rgba(0, 0, 0, 0.1);
        }
      `}</style>
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

      {/* Batch progress panel */}
      {activeBatchId && batchProgress && (
        <div className="absolute right-4 top-4 z-30 w-72 rounded-lg border border-[#D3D1C7] bg-white shadow-xl">
          <div className="flex items-center justify-between rounded-t-lg bg-[#0F6E56] px-3 py-2">
            <span className="text-sm font-bold text-white">
              {batchDone ? (batchWasStopped ? "Batch Stopped" : "Batch Complete") : "Batch Running…"}
            </span>
            <div className="flex items-center gap-2">
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
                  setBatchProgress(null)
                  setBatchDone(false)
                  setBatchWasStopped(false)
                  setCurrentAiStage(null)
                  setBatchBuildingProgress({})
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
            {/* Live AI stage indicator */}
            {!batchDone && currentAiStage && (
              <div className="mt-2 flex items-start gap-1.5 rounded-md bg-[#f0faf6] border border-[#c4e8d8] px-2 py-1.5">
                <Loader2 size={11} className="mt-0.5 shrink-0 animate-spin text-[#0F6E56]" />
                <div className="min-w-0">
                  <div className="text-[10px] font-semibold uppercase text-[#0F6E56]">AI Thinking</div>
                  <div className="text-[10px] text-[#17352b] leading-tight">{currentAiStage.thought}</div>
                </div>
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
                  Post-earthquake Upload ID (optional)
                </label>
                <input
                  type="text"
                  value={batchUploadId}
                  onChange={(e) => setBatchUploadId(e.target.value)}
                  placeholder="Auto-detect if left empty"
                  className="w-full rounded-md border border-[#D3D1C7] px-3 py-2 text-sm text-[#17352b] focus:outline-none focus:ring-2 focus:ring-[#0F6E56]"
                />
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
                <label className="mb-1 block text-xs font-semibold text-[#17352b]">Worker Name</label>
                <input
                  type="text"
                  value={batchWorkerName}
                  onChange={(e) => setBatchWorkerName(e.target.value)}
                  placeholder="Optional"
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

      <FieldMapChatSidebar isOpen={isChatSidebarOpen} onOpenChange={setIsChatSidebarOpen} />
    </div>
  )
}
