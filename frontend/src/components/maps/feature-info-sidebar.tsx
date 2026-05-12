"use client"

import { useState } from "react"
import { Maximize2, Minimize2, X } from "lucide-react"
import { toast } from "sonner"
import { AssessmentMediaGallery } from "@/components/maps/assessment-media-gallery"
import type { GisLayerKey } from "@/lib/api/gis-layers"

// This type defines all info needed to render the right-hand feature info panel.
export type SelectedFeatureInfo = {
  properties: Record<string, unknown>
  layerKey: GisLayerKey
  layerLabel: string
  lat: number
  lon: number
}

// This type defines props for the right-hand sidebar that replaces map popups.
type FeatureInfoSidebarProps = {
  info: SelectedFeatureInfo | null
  onClose: () => void
  onAnalyseBuilding: (osmId: number, lat: number, lon: number) => void
}

// This component renders feature details for the currently selected map feature.
export function FeatureInfoSidebar({ info, onClose, onAnalyseBuilding }: FeatureInfoSidebarProps) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "error">("idle")
  const [isExpanded, setIsExpanded] = useState(false)

  if (!info) {
    return null
  }

  const latitudeText = info.lat.toFixed(6)
  const longitudeText = info.lon.toFixed(6)
  const locationText = `${latitudeText}, ${longitudeText}`

  // This function copies the location string to clipboard with a brief state change.
  const handleCopyLocation = async () => {
    try {
      await navigator.clipboard.writeText(locationText)
      setCopyState("copied")
      toast.success("Location copied")
    } catch {
      setCopyState("error")
    } finally {
      window.setTimeout(() => setCopyState("idle"), 1200)
    }
  }

  const copyButtonLabel = copyState === "copied" ? "Copied" : copyState === "error" ? "Error" : "Copy"

  return (
    <aside
      className={`absolute right-0 top-0 z-30 flex h-full w-full flex-col border-l border-[#D3D1C7] bg-[#FAFAF8] shadow-xl transition-[max-width] duration-200 ${
        isExpanded ? "max-w-2xl" : "max-w-sm"
      }`}
    >
      <header className="flex items-center justify-between border-b border-[#D3D1C7] bg-[#0F6E56] px-4 py-3">
        <h2 className="text-sm font-semibold text-white">{info.layerLabel}</h2>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => setIsExpanded((prev) => !prev)}
            aria-label={isExpanded ? "Collapse sidebar" : "Expand sidebar"}
            title={isExpanded ? "Collapse" : "Expand"}
            className="rounded-md p-1 text-white transition-colors hover:bg-white/10"
          >
            {isExpanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
          </button>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close details"
            className="rounded-md p-1 text-white transition-colors hover:bg-white/10"
          >
            <X size={16} />
          </button>
        </div>
      </header>

      <div className="flex-1 overflow-y-auto px-4 py-4">
        {info.layerKey === "assessments" ? (
          <AssessmentInfoBody
            properties={info.properties}
            latitude={latitudeText}
            longitude={longitudeText}
            onCopy={handleCopyLocation}
            copyLabel={copyButtonLabel}
          />
        ) : (
          <GenericInfoBody
            properties={info.properties}
            layerKey={info.layerKey}
            latitude={latitudeText}
            longitude={longitudeText}
            onCopy={handleCopyLocation}
            copyLabel={copyButtonLabel}
            onAnalyseBuilding={onAnalyseBuilding}
          />
        )}
      </div>
    </aside>
  )
}

// ---------------------------------------------------------------------------
// Body — assessment-layer details
// ---------------------------------------------------------------------------

type AssessmentBodyProps = {
  properties: Record<string, unknown>
  latitude: string
  longitude: string
  onCopy: () => void
  copyLabel: string
}

function AssessmentInfoBody({ properties, latitude, longitude, onCopy, copyLabel }: AssessmentBodyProps) {
  const severity = String(properties.severity ?? "-")
  const status = String(properties.status ?? "unknown")
  const damageType = String(properties.damage_type ?? "unknown").replaceAll("_", " ")
  const structuralRisk = String(properties.structural_risk ?? "unknown")
  const recommendation = String(properties.recommended_action ?? "not available").replaceAll("_", " ")
  const inputType = String(properties.input_type ?? "unknown").replaceAll("_", " ")
  const createdAt = String(properties.created_at ?? "").replace("T", " ").replace("Z", "")
  const assessmentId = String(properties.id ?? "-")

  return (
    <div className="space-y-3 text-xs text-[#17352b]">
      <div className="flex items-center justify-between rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2.5 py-2">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-[#6b7280]">Assessment ID</div>
          <div className="mt-0.5 font-semibold">{assessmentId}</div>
        </div>
        <span className="rounded bg-[#0F6E56] px-2 py-0.5 text-[10px] font-semibold uppercase text-white">
          Severity {severity}
        </span>
      </div>

      <AssessmentMediaGallery properties={properties} />

      <div className="grid grid-cols-2 gap-2">
        <InfoBox label="Damage" value={damageType} capitalize />
        <InfoBox label="Risk" value={structuralRisk} capitalize />
      </div>

      <div className="grid grid-cols-2 gap-2">
        <InfoBox label="Status" value={status} capitalize />
        <InfoBox label="Input" value={inputType} capitalize />
      </div>

      <InfoBox label="Recommended Action" value={recommendation} capitalize />

      <LocationCopyRow
        latitude={latitude}
        longitude={longitude}
        onCopy={onCopy}
        copyLabel={copyLabel}
      />

      <div className="text-[10px] text-[#6b7280]">Created: {createdAt || "-"}</div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Body — generic GIS layer details
// ---------------------------------------------------------------------------

type GenericBodyProps = {
  properties: Record<string, unknown>
  layerKey: GisLayerKey
  latitude: string
  longitude: string
  onCopy: () => void
  copyLabel: string
  onAnalyseBuilding: (osmId: number, lat: number, lon: number) => void
}

function GenericInfoBody({
  properties,
  layerKey,
  latitude,
  longitude,
  onCopy,
  copyLabel,
  onAnalyseBuilding,
}: GenericBodyProps) {
  const entries = Object.entries(properties).filter(
    ([key, value]) => value !== null && value !== undefined && value !== "" && key !== "id"
  )

  const osmId = Number(properties.osm_id ?? properties.id ?? 0)
  const isBuilding = layerKey === "turkey_buildings" && Number.isFinite(osmId) && osmId > 0

  return (
    <div className="space-y-2 text-xs text-[#17352b]">
      <LocationCopyRow
        latitude={latitude}
        longitude={longitude}
        onCopy={onCopy}
        copyLabel={copyLabel}
      />

      <div className="divide-y divide-[#ECEAE2] rounded-md border border-[#D3D1C7] bg-white">
        {entries.length > 0 ? (
          entries.map(([key, value]) => (
            <div key={key} className="flex justify-between gap-3 px-2.5 py-1.5">
              <span className="text-[11px] font-medium capitalize text-[#6b7280]">
                {key.replace(/_/g, " ")}
              </span>
              <span className="text-right text-[11px] font-semibold text-[#17352b]">
                {String(value)}
              </span>
            </div>
          ))
        ) : (
          <div className="px-2.5 py-2 text-[11px] text-[#6b7280]">No additional data available.</div>
        )}
      </div>

      {isBuilding && (
        <button
          type="button"
          onClick={() => onAnalyseBuilding(osmId, Number(latitude), Number(longitude))}
          className="mt-2 w-full rounded-md border border-[#0F6E56] bg-[#0F6E56] px-3 py-2 text-xs font-semibold text-white hover:bg-[#0C614D]"
        >
          Analyse Building
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Small reusable bits
// ---------------------------------------------------------------------------

type InfoBoxProps = {
  label: string
  value: string
  capitalize?: boolean
}

function InfoBox({ label, value, capitalize }: InfoBoxProps) {
  return (
    <div className="rounded-md border border-[#D3D1C7] px-2.5 py-2">
      <div className="text-[10px] uppercase text-[#6b7280]">{label}</div>
      <div className={`mt-0.5 font-semibold ${capitalize ? "capitalize" : ""}`}>{value}</div>
    </div>
  )
}

type LocationCopyRowProps = {
  latitude: string
  longitude: string
  onCopy: () => void
  copyLabel: string
}

function LocationCopyRow({ latitude, longitude, onCopy, copyLabel }: LocationCopyRowProps) {
  return (
    <div className="flex items-center justify-between rounded-md border border-[#D3D1C7] bg-[#FAFAF8] px-2.5 py-2">
      <div>
        <div className="text-[10px] uppercase tracking-wide text-[#6b7280]">Location</div>
        <div className="font-semibold">
          {latitude}, {longitude}
        </div>
      </div>
      <button
        type="button"
        onClick={onCopy}
        className="rounded-md border border-[#D3D1C7] bg-white px-2 py-1 text-[10px] font-semibold text-[#0F6E56] hover:bg-[#ECEAE2]"
      >
        {copyLabel}
      </button>
    </div>
  )
}
