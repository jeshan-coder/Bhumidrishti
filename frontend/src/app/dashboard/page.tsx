"use client"

import { useEffect, useState } from "react"
import { AssessmentCard } from "@/components/triage/assessment-card"
import { Play, Loader2, CheckCircle2, AlertCircle, MapPin, ChevronDown, ChevronRight, Image, Film } from "lucide-react"

type Assessment = {
  id: string
  lat: number
  lon: number
  input_type: string
  photo_path: string | null
  severity: number
  damage_type: string
  structural_risk: string
  building_type: string
  recommended_action: string
  action_priority: number
  status: string
  created_at: string
}

type Upload = {
  id: string
  file_type: string
  original_filename: string
  saved_path: string
  lat: number
  lon: number
  status: string
  is_analyzed: boolean
  uploaded_at: string
  worker_name: string | null
  field_note: string | null
}

type LocationGroup = {
  group_id: string
  center_lat: number
  center_lon: number
  upload_count: number
  location_name: string | null
  uploads: Upload[]
}

function PendingUploadCard({
  upload,
  onAnalysisStarted
}: {
  upload: Upload
  onAnalysisStarted: (id: string) => void
}) {
  const [isTriggering, setIsTriggering] = useState(false)
  const [currentStatus, setCurrentStatus] = useState(upload.status)

  // Poll if processing
  useEffect(() => {
    let interval: NodeJS.Timeout
    if (currentStatus === "processing") {
      interval = setInterval(async () => {
        try {
          const res = await fetch(`http://localhost:8000/uploads/${upload.id}`)
          const json = await res.json()
          if (json.success && json.data.status !== "processing") {
            setCurrentStatus(json.data.status)
            if (json.data.status === "done") {
              onAnalysisStarted(upload.id)
            }
          }
        } catch (err) {
          console.error(err)
        }
      }, 5000)
    }
    return () => clearInterval(interval)
  }, [currentStatus, upload.id, onAnalysisStarted])

  const handleStart = async () => {
    setIsTriggering(true)
    try {
      const res = await fetch(`http://localhost:8000/uploads/${upload.id}/analyze`, {
        method: "POST"
      })
      const json = await res.json()
      if (json.success) {
        setCurrentStatus("processing")
      } else {
        alert(json.error)
      }
    } catch (err) {
      alert("Failed to start analysis")
    } finally {
      setIsTriggering(false)
    }
  }

  const isImage = upload.file_type.includes("photo") || upload.file_type.includes("image")

  return (
    <div className="flex items-center gap-3 rounded-lg border border-[#D3D1C7] bg-[#FAFAF8] p-3">
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-[#0F6E56]/10">
        {isImage ? <Image size={20} className="text-[#0F6E56]" /> : <Film size={20} className="text-[#0F6E56]" />}
      </div>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-[#17352b]">{upload.original_filename}</p>
        <p className="text-xs text-[#6b7280]">
          {upload.worker_name || "Unknown worker"} · {new Date(upload.uploaded_at).toLocaleDateString()}
        </p>
      </div>
      {currentStatus === "uploaded" || currentStatus === "failed" ? (
        <button
          onClick={handleStart}
          disabled={isTriggering}
          className="flex shrink-0 items-center gap-1 rounded-md bg-[#0F6E56] px-3 py-1.5 text-xs font-semibold text-white hover:bg-[#0c5945] disabled:opacity-50"
        >
          {isTriggering ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
          {currentStatus === "failed" ? "Retry" : "Assess"}
        </button>
      ) : currentStatus === "processing" ? (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-yellow-100 px-2 py-1 text-xs font-medium text-yellow-700">
          <Loader2 size={12} className="animate-spin" />
          Processing
        </span>
      ) : currentStatus === "done" ? (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-green-100 px-2 py-1 text-xs font-medium text-green-700">
          <CheckCircle2 size={12} />
          Done
        </span>
      ) : (
        <span className="flex shrink-0 items-center gap-1 rounded-md bg-gray-100 px-2 py-1 text-xs font-medium text-gray-600">
          <AlertCircle size={12} />
          {currentStatus}
        </span>
      )}
    </div>
  )
}

function LocationGroupCard({
  group,
  onAnalysisStarted
}: {
  group: LocationGroup
  onAnalysisStarted: (id: string) => void
}) {
  const [isExpanded, setIsExpanded] = useState(false)
  const hasMultiple = group.upload_count > 1

  return (
    <div className="rounded-xl border border-[#D3D1C7] bg-white shadow-sm overflow-hidden">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex w-full items-center gap-3 p-4 text-left hover:bg-[#FAFAF8] transition-colors"
      >
        <div className="flex h-12 w-12 shrink-0 items-center justify-center rounded-lg bg-[#0F6E56]/10">
          <MapPin size={24} className="text-[#0F6E56]" />
        </div>
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold text-[#17352b]">
            {group.location_name || `Location ${group.center_lat?.toFixed(4)}, ${group.center_lon?.toFixed(4)}`}
          </h3>
          <p className="mt-0.5 text-sm text-[#6b7280]">
            {hasMultiple ? `${group.upload_count} uploads` : "1 upload"} · 
            Lat: {group.center_lat?.toFixed(5)}, Lon: {group.center_lon?.toFixed(5)}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {hasMultiple && (
            <span className="rounded-full bg-blue-100 px-2 py-0.5 text-xs font-bold text-blue-800">
              Batch
            </span>
          )}
          {isExpanded ? <ChevronDown size={20} className="text-[#6b7280]" /> : <ChevronRight size={20} className="text-[#6b7280]" />}
        </div>
      </button>

      {isExpanded && (
        <div className="border-t border-[#F1EFE8] bg-[#FAFAF8] p-3">
          <div className="space-y-2">
            {group.uploads.map((upload) => (
              <PendingUploadCard
                key={upload.id}
                upload={upload}
                onAnalysisStarted={onAnalysisStarted}
              />
            ))}
          </div>
          {hasMultiple && (
            <div className="mt-3 flex justify-end">
              <button
                onClick={async () => {
                  // Start analysis for all uploads in this group
                  for (const upload of group.uploads) {
                    if (upload.status === "uploaded" || upload.status === "failed") {
                      try {
                        await fetch(`http://localhost:8000/uploads/${upload.id}/analyze`, { method: "POST" })
                      } catch (err) {
                        console.error(`Failed to start analysis for ${upload.id}:`, err)
                      }
                    }
                  }
                  onAnalysisStarted(group.group_id)
                }}
                className="flex items-center gap-2 rounded-lg bg-[#0F6E56] px-4 py-2 text-sm font-semibold text-white hover:bg-[#0c5945]"
              >
                <Play size={16} />
                Assess All {group.upload_count} Files
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function TriagePage() {
  const [assessments, setAssessments] = useState<Assessment[]>([])
  const [locationGroups, setLocationGroups] = useState<LocationGroup[]>([])
  const [uploadsWithoutCoords, setUploadsWithoutCoords] = useState<Upload[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Refresh trigger when a new assessment completes
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    async function fetchData() {
      setIsLoading(true)
      try {
        const [assRes, upRes] = await Promise.all([
          fetch("http://localhost:8000/assessments?limit=50"),
          fetch("http://localhost:8000/uploads/by-location?radius_meters=10")
        ])

        const assJson = await assRes.json()
        const upJson = await upRes.json()

        if (assJson.success && upJson.success) {
          setAssessments(assJson.data)
          setLocationGroups(upJson.data.location_groups)
          setUploadsWithoutCoords(upJson.data.uploads_without_coords)
        } else {
          setError(assJson.error || upJson.error)
        }
      } catch (err: unknown) {
        if (err instanceof Error) {
          setError(err.message)
        } else {
          setError("An unknown error occurred")
        }
      } finally {
        setIsLoading(false)
      }
    }

    fetchData()
  }, [refreshKey])

  const totalPendingUploads = locationGroups.reduce((sum, g) => sum + g.upload_count, 0) + uploadsWithoutCoords.length

  return (
    <main className="min-h-[calc(100dvh-49px)] w-full bg-[#FAFAF8] p-6 lg:p-10">
      <div className="mx-auto max-w-7xl">
        <header className="mb-10 border-b border-[#D3D1C7] pb-6">
          <h1 className="text-3xl font-bold tracking-tight text-[#17352b]">
            Triage Dashboard
          </h1>
          <p className="mt-2 text-sm text-[#6b7280]">
            Queue incoming physical intelligence and review structural damage assessments flagged by Gemma-4.
          </p>
        </header>

        {isLoading && !assessments.length && totalPendingUploads === 0 && (
          <div className="flex h-64 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-[#0F6E56] border-t-transparent" />
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-600 mb-8">
            {error}
          </div>
        )}

        {!isLoading && !error && totalPendingUploads > 0 && (
          <section className="mb-12">
            <div className="mb-4 flex items-center gap-2">
              <h2 className="text-xl font-bold text-[#17352b]">Pending Intelligence</h2>
              <span className="flex h-5 items-center justify-center rounded-full bg-blue-100 px-2 text-xs font-bold text-blue-800">
                {totalPendingUploads} Action{totalPendingUploads !== 1 && 's'} Required
              </span>
            </div>
            
            {/* Location Grouped Uploads */}
            {locationGroups.length > 0 && (
              <div className="mb-6">
                <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-[#6b7280]">
                  Grouped by Location ({locationGroups.length} location{locationGroups.length !== 1 ? 's' : ''})
                </h3>
                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  {locationGroups.map((group) => (
                    <LocationGroupCard
                      key={group.group_id}
                      group={group}
                      onAnalysisStarted={() => setRefreshKey(k => k + 1)}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Uploads Without Coordinates */}
            {uploadsWithoutCoords.length > 0 && (
              <div className="rounded-xl border border-yellow-200 bg-yellow-50 p-4">
                <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold text-yellow-800">
                  <AlertCircle size={16} />
                  Uploads Without GPS Coordinates ({uploadsWithoutCoords.length})
                </h3>
                <div className="space-y-2">
                  {uploadsWithoutCoords.map((upload) => (
                    <PendingUploadCard
                      key={upload.id}
                      upload={upload}
                      onAnalysisStarted={() => setRefreshKey(k => k + 1)}
                    />
                  ))}
                </div>
              </div>
            )}
          </section>
        )}

        <section>
          <div className="mb-4 flex items-center gap-2">
            <h2 className="text-xl font-bold text-[#17352b]">Completed Assessments</h2>
            <span className="flex h-5 items-center justify-center rounded-full bg-green-100 px-2 text-xs font-bold text-green-800">
              {assessments.length}
            </span>
          </div>

          {!isLoading && !error && assessments.length === 0 && (
             <div className="flex h-48 flex-col items-center justify-center rounded-xl border border-dashed border-[#D3D1C7] bg-white">
               <p className="font-medium text-[#17352b]">No assessments found</p>
               <p className="mt-1 text-sm text-[#6b7280]">
                 Trigger a pending upload above to generate reports.
               </p>
             </div>
          )}

          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {assessments.map((a) => (
              <AssessmentCard key={a.id} assessment={a} />
            ))}
          </div>
        </section>
      </div>
    </main>
  )
}
