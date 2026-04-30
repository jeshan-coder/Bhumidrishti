"use client"

import { useCallback, useEffect, useRef, useState } from "react"
import maplibregl from "maplibre-gl"
import { PMTiles, Protocol } from "pmtiles"
import { Map, MapPin, Route, MapPinned, Building, Droplets, AlertTriangle, Mountain, SatelliteDish } from "lucide-react"
import { useRouter } from "next/navigation"
import { toast } from "sonner"
import { FieldMapChatSidebar } from "@/components/maps/field-map-chat-sidebar"
import { fetchGisLayer, type GeoJsonFeatureCollection, type GisLayerKey } from "@/lib/api/gis-layers"
import { fetchUnfinishedUploads } from "@/lib/api/uploads"

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
  { key: "destroyed_buildings", label: "Earthquake Damage", color: "#E24B4A", icon: AlertTriangle },
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
        "fill-color": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          "#F59E0B",
          config.color,
        ],
        "fill-opacity": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          0.6,
          0.25,
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
        "line-color": config.color,
        "line-width": 1.2,
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
        "circle-color": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          "#F59E0B",
          config.color,
        ],
        "circle-radius": [
          "case",
          ["boolean", ["feature-state", "selected"], false],
          5,
          3,
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
        layerLabel: string,
        latitude: string,
        longitude: string
      ): string => {
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
        const popupContent = createPopupContent(feature.properties || {}, layerLabel, latitude, longitude)

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
          } else {
            // Handle vector GeoJSON layers
            const layerResult = await fetchGisLayer(config.key)
            if (isUnmounted) {
              continue
            }

            const isVisible = overlayStates[config.key].visible
            addOverlayToMap(map, config, layerResult.geojson, isVisible)
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
            {overlayLayerConfigs.map((config) => {
              const layerState = overlayStates[config.key]
              return (
                <button
                  key={config.key}
                  type="button"
                  onClick={() => handleToggleOverlay(config.key)}
                  className="flex items-center gap-2 rounded-md border border-[#D3D1C7] bg-white px-3 py-1.5 transition-all hover:border-[#0F6E56]"
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
