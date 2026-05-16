"use client"

import { useEffect, useRef, useState } from "react"
import {
  AlertCircle,
  CheckCircle2,
  CircleDot,
  Film,
  ImageIcon,
  Loader2,
  MapPin,
  Plus,
  Upload,
  X,
} from "lucide-react"
import { toast } from "sonner"
import {
} from "@/lib/api/uploads"
import { fetchPendingBatches, type PendingBatchRecord } from "@/lib/api/batches"

// ── Types ──────────────────────────────────────────────────────────────────────

type InputMode = "ground_photo" | "orthophoto" | "video"

type FileStatus = "pending" | "uploading" | "done" | "error"

type FileCoord = {
  lat: string
  lon: string
  locked: boolean
  source: "manual" | "gps"
  accuracy?: number
  gpsLoading: boolean
}

type ManagedFile = {
  id: string
  file: File
  name: string
  sizeLabel: string
  status: FileStatus
  uploadId?: string
  errorMsg?: string
  coord: FileCoord
}

type SharedBatchCoord = {
  lat: string
  lon: string
  source: "manual" | "gps"
  accuracy?: number
}

// ── Constants ──────────────────────────────────────────────────────────────────

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000"

const UPLOAD_ENDPOINT: Record<InputMode, string> = {
  ground_photo: `${BACKEND}/uploads/ground-photo`,
  orthophoto:   `${BACKEND}/uploads/orthophoto`,
  video:        `${BACKEND}/uploads/video`,
}

const ALLOWED: Record<InputMode, string> = {
  ground_photo: ".jpg,.jpeg,.png",
  orthophoto:   ".tif,.tiff,.geotiff,.jpg,.jpeg,.png",
  video:        ".mp4,.mov",
}

const MODE_INFO = [
  { key: "ground_photo" as InputMode, label: "Ground Photo",      sub: "JPEG · PNG",            icon: ImageIcon },
  { key: "orthophoto"   as InputMode, label: "Orthophoto / Drone", sub: "GeoTIFF · JPEG · PNG",  icon: MapPin    },
  { key: "video"        as InputMode, label: "Video",              sub: "MP4 · MOV",              icon: Film      },
]

// Modes where each file NEEDS a coordinate before upload
const NEEDS_COORD: InputMode[] = ["ground_photo", "video"]

// ── Helpers ────────────────────────────────────────────────────────────────────

const fmt = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1, minimumFractionDigits: 1 })

function sizeLabel(b: number) {
  if (b >= 1_073_741_824) return `${fmt.format(b / 1_073_741_824)} GB`
  if (b >= 1_048_576)     return `${fmt.format(b / 1_048_576)} MB`
  return `${fmt.format(b / 1_024)} KB`
}

function validLat(v: string) { const n = parseFloat(v); return !isNaN(n) && n >= -90  && n <= 90  }
function validLon(v: string) { const n = parseFloat(v); return !isNaN(n) && n >= -180 && n <= 180 }

function emptyCoord(): FileCoord {
  return { lat: "", lon: "", locked: false, source: "manual", gpsLoading: false }
}

// ── Sub-component: per-file coordinate row ─────────────────────────────────────

function CoordRow({
  fileId,
  coord,
  disabled,
  onChange,
  onLock,
  onUnlock,
  onGps,
}: {
  fileId: string
  coord: FileCoord
  disabled: boolean
  onChange: (id: string, field: "lat" | "lon", val: string) => void
  onLock: (id: string) => void
  onUnlock: (id: string) => void
  onGps: (id: string) => void
}) {
  const latOk = validLat(coord.lat)
  const lonOk = validLon(coord.lon)
  const canAdd = latOk && lonOk

  if (coord.locked) {
    return (
      <div className="mt-2.5 flex items-center gap-2 rounded-lg border border-[#A7D4C5] bg-[#EBF6F2] px-3 py-2">
        <MapPin className="h-3.5 w-3.5 flex-shrink-0 text-[#0F6E56]" />
        <span className="flex-1 text-xs font-semibold text-[#0E5B47]">
          {parseFloat(coord.lat).toFixed(5)}, {parseFloat(coord.lon).toFixed(5)}
        </span>
        <span className="text-xs text-[#6B7280]">
          {coord.source === "gps" ? `GPS ±${coord.accuracy ?? "?"}m` : "manual"}
        </span>
        {!disabled && (
          <button
            type="button"
            onClick={() => onUnlock(fileId)}
            className="ml-1 rounded px-1.5 py-0.5 text-xs font-medium text-[#0F6E56] hover:bg-[#D7ECE4] transition-colors"
          >
            Edit
          </button>
        )}
      </div>
    )
  }

  return (
    <div className="mt-2.5 space-y-1.5">
      <div className="flex gap-1.5">
        {/* Lat */}
        <div className="relative flex-1">
          <input
            type="text"
            value={coord.lat}
            onChange={(e) => onChange(fileId, "lat", e.target.value)}
            placeholder="Latitude"
            disabled={disabled}
            className={`h-8 w-full rounded-lg border px-2.5 text-xs font-medium outline-none ring-[#0F6E56]/20 focus:ring-2 disabled:opacity-50 ${
              coord.lat && !latOk
                ? "border-red-400 bg-red-50 text-red-700"
                : "border-[#CFCBBF] bg-[#F4F2EC] text-[#0E5B47]"
            }`}
          />
          {coord.lat && latOk && (
            <CheckCircle2 className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[#0F6E56]" />
          )}
        </div>

        {/* Lon */}
        <div className="relative flex-1">
          <input
            type="text"
            value={coord.lon}
            onChange={(e) => onChange(fileId, "lon", e.target.value)}
            placeholder="Longitude"
            disabled={disabled}
            className={`h-8 w-full rounded-lg border px-2.5 text-xs font-medium outline-none ring-[#0F6E56]/20 focus:ring-2 disabled:opacity-50 ${
              coord.lon && !lonOk
                ? "border-red-400 bg-red-50 text-red-700"
                : "border-[#CFCBBF] bg-[#F4F2EC] text-[#0E5B47]"
            }`}
          />
          {coord.lon && lonOk && (
            <CheckCircle2 className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-[#0F6E56]" />
          )}
        </div>

        {/* Add */}
        <button
          type="button"
          onClick={() => onLock(fileId)}
          disabled={!canAdd || disabled}
          title={!canAdd ? "Enter valid lat/lon first" : "Confirm coordinates"}
          className={`inline-flex h-8 items-center gap-1 rounded-lg px-3 text-xs font-semibold transition-all ${
            canAdd && !disabled
              ? "bg-[#0F6E56] text-white hover:bg-[#0C614D] active:scale-95"
              : "cursor-not-allowed bg-[#E5E7EB] text-[#9CA3AF]"
          }`}
        >
          <Plus className="h-3 w-3" />
          Add
        </button>

        {/* GPS */}
        <button
          type="button"
          onClick={() => onGps(fileId)}
          disabled={coord.gpsLoading || disabled}
          className="inline-flex h-8 items-center gap-1 rounded-lg border border-[#0F6E56] bg-white px-3 text-xs font-semibold text-[#0F6E56] transition-all hover:bg-[#E6F5F0] active:scale-95 disabled:opacity-50"
        >
          {coord.gpsLoading
            ? <Loader2 className="h-3 w-3 animate-spin" />
            : <CircleDot className="h-3 w-3" />
          }
          GPS
        </button>
      </div>

      {/* Inline validation */}
      {((coord.lat && !latOk) || (coord.lon && !lonOk)) && (
        <p className="text-[11px] text-red-500">
          {coord.lat && !latOk ? "Lat must be –90 to 90. " : ""}
          {coord.lon && !lonOk ? "Lon must be –180 to 180." : ""}
        </p>
      )}
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function AssessmentPage() {
  const [mode, setMode]           = useState<InputMode>("ground_photo")
  const [files, setFiles]         = useState<ManagedFile[]>([])
  const [isDragOver, setIsDragOver] = useState(false)
  const [workerName, setWorkerName] = useState("")
  const [fieldNote, setFieldNote]   = useState("")
  // This variable toggles whether one coordinate should be reused for all files in current batch.
  const [useSameLocationForBatch, setUseSameLocationForBatch] = useState(true)
  // This variable stores the most recent confirmed batch coordinate for auto-fill.
  const [sharedBatchCoord, setSharedBatchCoord] = useState<SharedBatchCoord | null>(null)
  const [isPendingBatchesLoading, setIsPendingBatchesLoading] = useState(false)
  // This variable stores a page-level orthophoto upload error message for clear user feedback.
  const [orthophotoUploadError, setOrthophotoUploadError] = useState<string | null>(null)
  const [pendingBatches, setPendingBatches] = useState<PendingBatchRecord[]>([])
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const needsCoord = NEEDS_COORD.includes(mode)

  // Site-based pending status refresh (replaces upload-based unfinished workflow).
  async function refreshPendingBatches(showErrorToast = false, silent = false) {
    if (!silent) setIsPendingBatchesLoading(true)
    try {
      const pendingBatchItems = await fetchPendingBatches(20, false)
      setPendingBatches(pendingBatchItems)
    } catch {
      setPendingBatches([])
      if (showErrorToast) {
        toast("Failed to load site assessment status")
      }
    } finally {
      if (!silent) setIsPendingBatchesLoading(false)
    }
  }

  useEffect(() => {
    void refreshPendingBatches(false)
    const intervalId = window.setInterval(() => {
      void refreshPendingBatches(false, true)
    }, 5000)
    return () => window.clearInterval(intervalId)
  }, [])

  // ── File management ──────────────────────────────────────────────────────

  // This function applies one shared coordinate to all editable files in the batch.
  function applySharedCoordToFiles(coord: SharedBatchCoord, excludeId?: string) {
    setFiles((prev) =>
      prev.map((fileItem) => {
        if (fileItem.id === excludeId) {
          return fileItem
        }
        if (fileItem.status === "uploading" || fileItem.status === "done") {
          return fileItem
        }
        return {
          ...fileItem,
          coord: {
            ...fileItem.coord,
            lat: coord.lat,
            lon: coord.lon,
            locked: true,
            source: coord.source,
            accuracy: coord.accuracy,
            gpsLoading: false,
          },
        }
      })
    )
  }

  function addFiles(fileList: FileList | File[]) {
    setOrthophotoUploadError(null)
    const templateCoord = useSameLocationForBatch ? sharedBatchCoord : null
    const incoming: ManagedFile[] = Array.from(fileList).map((file, i) => ({
      id: `${file.name}-${file.lastModified}-${i}-${Math.random().toString(36).slice(2)}`,
      file,
      name: file.name,
      sizeLabel: sizeLabel(file.size),
      status: "pending",
      coord: templateCoord
        ? {
            lat: templateCoord.lat,
            lon: templateCoord.lon,
            locked: true,
            source: templateCoord.source,
            accuracy: templateCoord.accuracy,
            gpsLoading: false,
          }
        : emptyCoord(),
    }))
    setFiles((prev) => [...prev, ...incoming])
  }

  function removeFile(id: string) {
    setFiles((prev) => prev.filter((f) => f.id !== id))
  }

  function updateFileCoord(id: string, update: Partial<FileCoord>) {
    setFiles((prev) =>
      prev.map((f) => f.id === id ? { ...f, coord: { ...f.coord, ...update } } : f)
    )
  }

  // ── Coordinate handlers (per-file) ───────────────────────────────────────

  function handleCoordChange(id: string, field: "lat" | "lon", val: string) {
    updateFileCoord(id, { [field]: val })
  }

  function handleLock(id: string) {
    const f = files.find((x) => x.id === id)
    if (!f) return
    if (!validLat(f.coord.lat) || !validLon(f.coord.lon)) return
    updateFileCoord(id, { locked: true, source: "manual" })

    const coordForBatch: SharedBatchCoord = {
      lat: f.coord.lat.trim(),
      lon: f.coord.lon.trim(),
      source: "manual",
    }
    setSharedBatchCoord(coordForBatch)
    if (useSameLocationForBatch) {
      applySharedCoordToFiles(coordForBatch, id)
    }
  }

  function handleUnlock(id: string) {
    updateFileCoord(id, { locked: false, accuracy: undefined })
  }

  function handleGps(id: string) {
    if (!("geolocation" in navigator)) return
    updateFileCoord(id, { gpsLoading: true })
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const coordForBatch: SharedBatchCoord = {
          lat: pos.coords.latitude.toFixed(6),
          lon: pos.coords.longitude.toFixed(6),
          source: "gps",
          accuracy: Math.round(pos.coords.accuracy),
        }

        updateFileCoord(id, {
          lat: coordForBatch.lat,
          lon: coordForBatch.lon,
          locked: true,
          source: "gps",
          accuracy: coordForBatch.accuracy,
          gpsLoading: false,
        })

        setSharedBatchCoord(coordForBatch)
        if (useSameLocationForBatch) {
          applySharedCoordToFiles(coordForBatch, id)
        }
      },
      () => {
        updateFileCoord(id, { gpsLoading: false })
      },
      { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
    )
  }

  // ── Upload logic ─────────────────────────────────────────────────────────

  async function uploadOne(f: ManagedFile) {
    setFiles((prev) => prev.map((x) => x.id === f.id ? { ...x, status: "uploading" } : x))

    try {
      const form = new FormData()
      form.append("file", f.file)
      if (needsCoord) {
        form.append("lat", f.coord.lat.trim())
        form.append("lon", f.coord.lon.trim())
      }
      if (workerName.trim()) form.append("worker_name", workerName.trim())
      if (fieldNote.trim())  form.append("field_note",  fieldNote.trim())

      const res  = await fetch(UPLOAD_ENDPOINT[mode], { method: "POST", body: form })
      const json = await res.json()
      if (!res.ok || !json.success) throw new Error(json.error ?? `HTTP ${res.status}`)

      setFiles((prev) =>
        prev.map((x) =>
          x.id === f.id ? { ...x, status: "done", uploadId: json.data?.upload_id } : x
        )
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Upload failed"
      const pageErrorMessage = `${f.name}: ${msg}`
      if (mode === "orthophoto") {
        setOrthophotoUploadError(pageErrorMessage)
      }
      // Show a toast for every upload failure so errors are never silent.
      toast.error(pageErrorMessage)
      setFiles((prev) =>
        prev.map((x) => x.id === f.id ? { ...x, status: "error", errorMsg: msg } : x)
      )
    }
  }

  async function handleUploadAll() {
    setOrthophotoUploadError(null)
    const pending = files.filter((f) => f.status === "pending" || f.status === "error")
    for (const f of pending) await uploadOne(f)
  }

  // ── Derived state ────────────────────────────────────────────────────────

  const anyUploading  = files.some((f) => f.status === "uploading")
  // allDone is true only when every file succeeded — not when some errored.
  const allDone       = files.length > 0 && files.every((f) => f.status === "done")
  // allFinished is true when every file is in a terminal state (done or error).
  const anyErrors     = files.some((f) => f.status === "error")
  const allFinished   = files.length > 0 && files.every((f) => f.status === "done" || f.status === "error")
  const missingCoords = needsCoord ? files.filter((f) => f.status === "pending" && !f.coord.locked) : []
  const canUpload     = files.length > 0 && missingCoords.length === 0 && !anyUploading
  const visiblePendingBatches = pendingBatches.filter((batch) => {
    const total = Math.max(0, Number(batch.total_buildings) || 0)
    const doneCount = Math.max(0, Number(batch.processed) || 0)
    const skippedCount = Math.max(0, Number(batch.skipped) || 0)
    const remaining = Math.max(
      0,
      batch.remaining_buildings == null
        ? total - doneCount - skippedCount
        : Number(batch.remaining_buildings),
    )
    return remaining > 0
  })

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-[calc(100vh-3rem)] bg-[radial-gradient(circle_at_20%_0%,#eaf7f1_0%,#f5f3ee_42%,#f5f3ee_100%)] px-4 py-6 md:px-8 md:py-10">
      <section className="mx-auto w-full max-w-3xl">

        {/* Header */}
        <div className="mb-5">
          <h1 className="text-xl font-bold tracking-tight text-[#0E5B47]">New Assessment Upload</h1>
          <p className="mt-0.5 text-xs text-[#6B7280]">
            One location can be reused across many photos. Upload is blocked until all files have coordinates.
          </p>
        </div>

        {/* Site-based assessment status */}
        {visiblePendingBatches.length > 0 && (
          <div className="mb-4 rounded-2xl border border-amber-200 bg-amber-50 p-3.5">
            <div className="mb-2 flex items-center justify-between gap-2">
              <p className="text-sm font-bold text-amber-900">Pending Site Assessments</p>
              <button
                type="button"
                onClick={() => void refreshPendingBatches(true)}
                disabled={isPendingBatchesLoading}
                className="inline-flex h-7 items-center rounded-lg border border-amber-300 bg-white px-2.5 text-[11px] font-semibold text-amber-900 hover:bg-amber-100 disabled:opacity-60"
              >
                {isPendingBatchesLoading ? "Refreshing..." : "Refresh"}
              </button>
            </div>
            <div className="space-y-1.5">
              {visiblePendingBatches.slice(0, 6).map((batch) => {
                const total = Math.max(0, Number(batch.total_buildings) || 0)
                const doneCount = Math.max(0, Number(batch.processed) || 0)
                const failedCount = Math.max(0, Number(batch.failed) || 0)
                const skippedCount = Math.max(0, Number(batch.skipped) || 0)
                const remaining = Math.max(
                  0,
                  batch.remaining_buildings == null
                    ? total - doneCount - skippedCount
                    : Number(batch.remaining_buildings),
                )
                return (
                  <div key={batch.batch_id} className="rounded-lg border border-amber-200 bg-white px-2.5 py-2 text-[11px]">
                    <div className="font-semibold text-[#17352b]">{batch.site_name || batch.batch_id}</div>
                    <div className="text-[#6B7280]">
                      {doneCount} done · {skippedCount} skipped · {failedCount} failed · of {total}
                    </div>
                    <div className="mt-0.5 font-semibold text-amber-900">
                      {remaining} building{remaining !== 1 ? "s" : ""} remaining
                    </div>
                  </div>
                )
              })}
            </div>
          </div>
        )}

        {/* ── Mode selector ───────────────────────────────────────────────── */}
        <div className="grid gap-2 md:grid-cols-3">
          {MODE_INFO.map(({ key, label, sub, icon: Icon }) => {
            const active = key === mode
            return (
              <button
                key={key}
                type="button"
                onClick={() => { setMode(key); setFiles([]) }}
                className={`rounded-xl border px-3 py-3 text-left transition-all ${
                  active
                    ? "border-[#0F6E56] bg-[#DDEFEA] shadow-[inset_0_0_0_1px_#0F6E56]"
                    : "border-[#D3D1C7] bg-[#F7F6F2] hover:border-[#9CA3AF] hover:bg-[#F2F0EA]"
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="inline-flex h-7 w-7 items-center justify-center rounded-md bg-white text-[#0F6E56] shadow-sm">
                    <Icon className="h-3.5 w-3.5" />
                  </span>
                  <span className="text-sm font-semibold text-[#1F2937]">{label}</span>
                </div>
                <p className="mt-1 text-xs text-[#6B7280]">{sub}</p>
              </button>
            )
          })}
        </div>

        {/* ── Compact drop zone / add files button ────────────────────────── */}
        <div className="mt-4">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setIsDragOver(true) }}
            onDragLeave={() => setIsDragOver(false)}
            onDrop={(e) => { e.preventDefault(); setIsDragOver(false); addFiles(e.dataTransfer.files) }}
            className={`flex w-full items-center gap-3 rounded-xl border border-dashed px-4 py-3.5 transition-all ${
              isDragOver
                ? "scale-[1.01] border-[#0F6E56] bg-[#D7ECE4]"
                : "border-[#7FBEAC] bg-[#EBF6F2] hover:bg-[#E4F4EE]"
            }`}
          >
            <span className="inline-flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-[#0F6E56] text-white shadow-[0_4px_10px_rgba(15,110,86,0.28)]">
              <Upload className="h-4 w-4" />
            </span>
            <div className="text-left">
              <p className="text-sm font-semibold text-[#0E5B47]">
                {isDragOver ? "Drop files here" : "Add files"}
              </p>
              <p className="text-xs text-[#6B7280]">{ALLOWED[mode].replace(/\./g, "").toUpperCase().split(",").join(" · ")} · max 2 GB</p>
            </div>
          </button>

          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={ALLOWED[mode]}
            className="hidden"
            onChange={(e) => { if (e.target.files?.length) addFiles(e.target.files); e.target.value = "" }}
          />
        </div>

        {/* Orthophoto upload error banner */}
        {mode === "orthophoto" && orthophotoUploadError && (
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-3 py-2.5">
            <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-600" />
            <div>
              <p className="text-xs font-semibold text-red-700">Orthophoto upload failed</p>
              <p className="text-xs text-red-600">{orthophotoUploadError}</p>
            </div>
          </div>
        )}

        {needsCoord && files.length > 0 && (
          <label className="mt-3 inline-flex items-center gap-2 rounded-lg border border-[#D3D1C7] bg-white px-3 py-2 text-xs text-[#17352b]">
            <input
              type="checkbox"
              checked={useSameLocationForBatch}
              onChange={(event) => setUseSameLocationForBatch(event.target.checked)}
              className="h-3.5 w-3.5 accent-[#0F6E56]"
            />
            Reuse same location for all files in this batch
          </label>
        )}

        {/* ── File cards ──────────────────────────────────────────────────── */}
        <div className="mt-3 space-y-3">
          {files.length === 0 && (
            <div className="rounded-xl border border-[#D3D1C7] bg-[#F7F6F2] px-4 py-3.5 text-xs text-[#9CA3AF]">
              No files added yet — click &ldquo;Add files&rdquo; above.
            </div>
          )}

          {files.map((f, index) => {
            const isUploading = f.status === "uploading"
            const isDone      = f.status === "done"
            const isError     = f.status === "error"
            const isPending   = f.status === "pending"
            const coordMissing = needsCoord && isPending && !f.coord.locked

            return (
              <div
                key={f.id}
                className={`rounded-2xl border px-4 py-3.5 transition-colors ${
                  isDone    ? "border-[#A7D4C5] bg-[#EBF6F2]"
                  : isError ? "border-red-200 bg-red-50"
                  : coordMissing ? "border-[#FCD34D] bg-[#FFFBEB]"
                  : "border-[#D3D1C7] bg-white"
                }`}
              >
                {/* ── File header row ── */}
                <div className="flex items-start gap-3">
                  {/* Index badge + icon */}
                  <div className="flex flex-shrink-0 flex-col items-center gap-1">
                    <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-[#0F6E56] text-[10px] font-bold text-white">
                      {index + 1}
                    </span>
                    <span className={`inline-flex h-8 w-8 items-center justify-center rounded-lg ${
                      isDone    ? "bg-[#C4E8DC] text-[#0F6E56]"
                      : isError ? "bg-red-100 text-red-500"
                      : "bg-[#E8E5DE] text-[#4B5563]"
                    }`}>
                      {isUploading ? <Loader2 className="h-4 w-4 animate-spin" />
                        : isDone    ? <CheckCircle2 className="h-4 w-4" />
                        : isError   ? <AlertCircle className="h-4 w-4" />
                        : mode === "video" ? <Film className="h-4 w-4" />
                        : <ImageIcon className="h-4 w-4" />
                      }
                    </span>
                  </div>

                  {/* File info */}
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <p className="min-w-0 truncate text-sm font-semibold text-[#1F2937]">{f.name}</p>
                      <span className="flex-shrink-0 text-xs text-[#9CA3AF]">{f.sizeLabel}</span>
                    </div>

                    {/* Status / upload ID / error */}
                    {isUploading && (
                      <p className="mt-0.5 text-xs text-[#0F6E56]">Uploading…</p>
                    )}
                    {isDone && f.uploadId && (
                      <p className="mt-0.5 text-xs font-mono text-[#0F6E56]">{f.uploadId} · Saved ✓</p>
                    )}
                    {isError && (
                      <p className="mt-0.5 text-xs text-red-500">{f.errorMsg}</p>
                    )}

                    {/* Coord missing warning inline */}
                    {coordMissing && (
                      <div className="mt-1 flex items-center gap-1 text-[11px] text-amber-700">
                        <AlertCircle className="h-3 w-3 flex-shrink-0" />
                        Enter coordinates for this file before uploading
                      </div>
                    )}
                  </div>

                  {/* Remove button */}
                  {!isUploading && !isDone && (
                    <button
                      type="button"
                      onClick={() => removeFile(f.id)}
                      className="flex-shrink-0 inline-flex h-7 w-7 items-center justify-center rounded-full bg-[#E5E7EB] text-[#6B7280] transition-colors hover:bg-[#D1D5DB]"
                      aria-label={`Remove ${f.name}`}
                    >
                      <X className="h-3.5 w-3.5" />
                    </button>
                  )}
                </div>

                {/* ── Per-file coordinate row (only for modes that need it) ── */}
                {needsCoord && (
                  <CoordRow
                    fileId={f.id}
                    coord={f.coord}
                    disabled={isUploading || isDone}
                    onChange={handleCoordChange}
                    onLock={handleLock}
                    onUnlock={handleUnlock}
                    onGps={handleGps}
                  />
                )}
              </div>
            )
          })}
        </div>

        {/* ── Optional global fields ───────────────────────────────────────── */}
        {files.length > 0 && (
          <div className="mt-4 grid gap-2.5 md:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs font-semibold text-[#6B7280]">
                Field worker <span className="font-normal text-[#9CA3AF]">(optional)</span>
              </label>
              <input
                type="text"
                value={workerName}
                onChange={(e) => setWorkerName(e.target.value)}
                placeholder="e.g. Ravi Kumar"
                className="h-9 w-full rounded-xl border border-[#CFCBBF] bg-[#F4F2EC] px-3 text-sm text-[#1F2937] outline-none ring-[#0F6E56]/30 focus:ring-2"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-semibold text-[#6B7280]">
                Field note <span className="font-normal text-[#9CA3AF]">(optional, shared)</span>
              </label>
              <input
                type="text"
                value={fieldNote}
                onChange={(e) => setFieldNote(e.target.value)}
                placeholder="Roof collapsed, road blocked…"
                className="h-9 w-full rounded-xl border border-[#CFCBBF] bg-[#F4F2EC] px-3 text-sm text-[#1F2937] outline-none ring-[#0F6E56]/30 focus:ring-2"
              />
            </div>
          </div>
        )}

        {/* ── Summary banner: how many still need coords ───────────────────── */}
        {needsCoord && missingCoords.length > 0 && (
          <div className="mt-4 flex items-start gap-3 rounded-xl border border-[#FCD34D] bg-[#FFFBEB] px-4 py-3">
            <AlertCircle className="mt-px h-4 w-4 flex-shrink-0 text-amber-600" />
            <p className="text-xs text-amber-800">
              <strong>{missingCoords.length} file{missingCoords.length > 1 ? "s" : ""}</strong>{" "}
              {missingCoords.length > 1 ? "need" : "needs"} coordinates before you can upload.
              Use the <strong>Add</strong> or <strong>GPS</strong> buttons on each highlighted card.
            </p>
          </div>
        )}

        {/* ── Upload CTA — show when there are still pending/error files to (re)try ── */}
        {files.length > 0 && !allDone && (
          <button
            type="button"
            onClick={handleUploadAll}
            disabled={!canUpload}
            className={`mt-4 h-12 w-full rounded-xl text-sm font-semibold text-white shadow-[0_8px_18px_rgba(15,110,86,0.22)] transition-all ${
              canUpload
                ? "bg-[#0F6E56] hover:bg-[#0C614D] active:scale-[0.99]"
                : "cursor-not-allowed bg-[#A0C5BA]"
            }`}
          >
            {anyUploading ? (
              <span className="flex items-center justify-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" />
                Uploading…
              </span>
            ) : canUpload ? (
              `Upload ${files.filter(f => f.status === "pending" || f.status === "error").length} file${files.length > 1 ? "s" : ""} to server`
            ) : (
              `Assign coordinates to all files first`
            )}
          </button>
        )}

        {/* ── All done / partial-error banner ─────────────────────────────── */}
        {allFinished && (
          <div className="mt-4 space-y-2.5">
            {allDone ? (
              <div className="flex items-center justify-center gap-2 rounded-xl border border-[#A7D4C5] bg-[#EBF6F2] py-3 text-sm font-semibold text-[#0F6E56]">
                <CheckCircle2 className="h-5 w-5" />
                All files uploaded successfully
              </div>
            ) : anyErrors && !allDone ? (
              <div className="flex items-start gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3">
                <AlertCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-600" />
                <div>
                  <p className="text-sm font-semibold text-red-700">
                    {files.filter((f) => f.status === "error").length} file{files.filter((f) => f.status === "error").length > 1 ? "s" : ""} failed to upload
                  </p>
                  <p className="text-xs text-red-600">Check the cards above for details. You can retry by clicking Upload again.</p>
                </div>
              </div>
            ) : null}
            <button
              type="button"
              onClick={() => { setFiles([]); setWorkerName(""); setFieldNote("") }}
              className="h-10 w-full rounded-xl border border-[#D3D1C7] bg-[#F7F6F2] text-sm font-semibold text-[#4B5563] transition-colors hover:bg-[#EDEAE3]"
            >
              {allDone ? "Upload more files" : "Clear and start over"}
            </button>
          </div>
        )}

      </section>

    </main>
  )
}
