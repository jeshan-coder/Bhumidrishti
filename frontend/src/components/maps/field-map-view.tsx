"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import maplibregl from "maplibre-gl"
import { PMTiles, Protocol } from "pmtiles"
import { Map, MapPin, Route, MapPinned, Building, Droplets, AlertTriangle, Mountain, SatelliteDish, ImageIcon } from "lucide-react"
import { useRouter } from "next/navigation"
import { toast } from "sonner"
import { FieldMapChatSidebar } from "@/components/maps/field-map-chat-sidebar"
import { fetchGisLayer, type GeoJsonFeatureCollection, type GisLayerKey } from "@/lib/api/gis-layers"
import { fetchAssessmentBuildingLayer } from "@/lib/api/assessments"
import { fetchPostEarthquakeLayer, fetchUnfinishedUploads } from "@/lib/api/uploads"

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
      layout: { visibility: config.key === "assessments" ? "none" : visibilityValue },
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

// This component renders the PMTiles basemap with a single-button AI chat sidebar.
export function FieldMapView() {
  // This variable handles route navigation from map actions.
  const router = useRouter()

  // This variable references the map container element for MapLibre mount.
  const mapContainerRef = useRef<HTMLDivElement | null>(null)
  const mapInstanceRef = useRef<maplibregl.Map | null>(null)
  const overlayLoadedRef = useRef<Set<GisLayerKey>>(new Set())
  const hasShownUnfinishedToastRef = useRef(false)

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
  const [unfinishedCount, setUnfinishedCount] = useState(0)

  // This variable tracks currently selected feature for highlighting.
  const selectedFeatureRef = useRef<{ source: string; id: string | number } | null>(null)
  const popupRef = useRef<maplibregl.Popup | null>(null)

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

  // This function routes to assessment page and requests opening unfinished sheet.
  const goToUnfinishedAssessments = useCallback(() => {
    router.push("/assessment?openUnfinished=1")
  }, [router])

  // This effect checks unfinished uploads and shows Sonner in map screen as well.
  useEffect(() => {
    let isMounted = true

    const loadUnfinishedUploads = async () => {
      try {
        const uploads = await fetchUnfinishedUploads(100)
        if (!isMounted) {
          return
        }

        setUnfinishedCount(uploads.length)

        if (uploads.length > 0 && !hasShownUnfinishedToastRef.current) {
          toast("You have unfinished assessments", {
            action: {
              label: "View",
              onClick: goToUnfinishedAssessments,
            },
          })
          hasShownUnfinishedToastRef.current = true
        }

        if (uploads.length === 0) {
          hasShownUnfinishedToastRef.current = false
        }
      } catch {
        if (isMounted) {
          setUnfinishedCount(0)
        }
      }
    }

    void loadUnfinishedUploads()

    return () => {
      isMounted = false
    }
  }, [goToUnfinishedAssessments])

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
            </div>
          </div>
        `
      }

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
            overlayLoadedRef.current.add(config.key)

            // Add click handlers for interactive layers
            const layerIds = getOverlayLayerIds(config.key)
            map.on("click", layerIds.fill, handleFeatureClick)
            map.on("click", layerIds.point, handleFeatureClick)

            // Change cursor on hover
            map.on("mouseenter", layerIds.fill, () => { map.getCanvas().style.cursor = "pointer" })
            map.on("mouseleave", layerIds.fill, () => { map.getCanvas().style.cursor = "" })
            map.on("mouseenter", layerIds.point, () => { map.getCanvas().style.cursor = "pointer" })
            map.on("mouseleave", layerIds.point, () => { map.getCanvas().style.cursor = "" })

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

            {unfinishedCount > 0 && (
              <button
                type="button"
                onClick={goToUnfinishedAssessments}
                className="flex items-center gap-2 rounded-md border border-[#0F6E56] bg-[#0F6E56] px-3 py-1.5 text-xs font-semibold text-white transition-all hover:bg-[#0C614D]"
              >
                View unfinished ({unfinishedCount})
              </button>
            )}
          </div>
        </div>
      </aside>

      <FieldMapChatSidebar isOpen={isChatSidebarOpen} onOpenChange={setIsChatSidebarOpen} />
    </div>
  )
}
