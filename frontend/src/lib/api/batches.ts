/**
 * API client for orthophoto batch analysis endpoints.
 */

const API_BASE = process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8000"

export type BatchStatus = "queued" | "processing" | "complete" | "failed"

export interface BatchRecord {
  batch_id: string
  site_name: string
  total_buildings: number
  processed: number
  failed: number
  skipped: number
  status: BatchStatus
  worker_name: string | null
  created_at: string | null
  completed_at: string | null
}

export interface PendingBatchRecord {
  batch_id: string
  site_name: string
  area_geojson?: {
    type: string
    coordinates: unknown
  } | null
  total_buildings: number
  processed: number
  failed: number
  skipped: number
  remaining_buildings?: number
  status: string
  created_at: string | null
  is_active_task?: boolean
}

export interface BatchBuildingsResult {
  batch_id: string
  osm_ids: number[]
  bbox: {
    west: number
    south: number
    east: number
    north: number
  } | null
}

export type GeoJsonGeometry = {
  type: string
  coordinates: unknown
}

export interface StartBatchRequest {
  post_ortho_upload_id?: string
  area_polygon: GeoJsonGeometry
  site_name: string
  worker_name?: string
  force_reanalyze?: boolean
}

export interface BatchSseEvent {
  type:
    | "batch_started"
    | "batch_complete"
    | "batch_failed"
    | "building_started"
    | "building_clipping"
    | "building_analyzing"
    | "building_ai_stage"
    | "building_done"
    | "building_skipped"
    | "building_failed"
    | "stream_closed"
  batch_id: string
  [key: string]: unknown
}

export interface BuildingCoverageResult {
  osm_id: number
  has_coverage: boolean
  upload_id: string | null
}

// ── REST calls ────────────────────────────────────────────────────────────────

export async function startOrthophotoBatch(
  req: StartBatchRequest
): Promise<{ batch_id: string; site_name: string; status: string }> {
  const res = await fetch(`${API_BASE}/batch/orthophoto`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  })
  const json = await res.json()
  // Handle both our {success, error} envelope and FastAPI's raw {detail} errors.
  if (!json.success) {
    const msg = json.error ?? json.detail ?? "Failed to start batch"
    throw new Error(String(msg))
  }
  return json.data
}

export async function getBatchStatus(batchId: string): Promise<BatchRecord> {
  const res = await fetch(`${API_BASE}/batch/${batchId}`)
  const json = await res.json()
  if (!json.success) throw new Error(json.error || "Batch not found")
  return json.data
}

export async function checkBuildingCoverage(
  osmId: number,
  lat: number,
  lon: number
): Promise<BuildingCoverageResult> {
  const params = new URLSearchParams({ lat: String(lat), lon: String(lon) })
  const res = await fetch(`${API_BASE}/batch/building/${osmId}/check-coverage?${params}`)
  const json = await res.json()
  if (!json.success) throw new Error(json.error || "Coverage check failed")
  return json.data
}

export async function analyzeSingleBuilding(
  osmId: number,
  lat: number,
  lon: number,
  siteName?: string,
): Promise<{ batch_id: string; osm_id: number; upload_id: string; stream_url: string }> {
  const params = new URLSearchParams({ lat: String(lat), lon: String(lon) })
  if (siteName) params.set("site_name", siteName)
  const res = await fetch(`${API_BASE}/batch/building/${osmId}/analyze?${params}`, {
    method: "POST",
  })
  const json = await res.json()
  if (!json.success) throw new Error(json.error || "Single building analysis failed")
  return json.data
}

export async function fetchBatchSites(): Promise<string[]> {
  try {
    const res = await fetch(`${API_BASE}/batch/sites`)
    const json = await res.json()
    if (!json.success) return []
    return json.data as string[]
  } catch {
    return []
  }
}

export async function fetchPendingBatches(limit = 20, onlyActive = true): Promise<PendingBatchRecord[]> {
  try {
    const res = await fetch(`${API_BASE}/batch/pending?limit=${limit}&only_active=${onlyActive ? "true" : "false"}`)
    const json = await res.json()
    if (!json.success) return []
    return (json.data ?? []) as PendingBatchRecord[]
  } catch {
    return []
  }
}

export async function fetchBatchBuildings(batchId: string, limit = 5000): Promise<BatchBuildingsResult> {
  const res = await fetch(`${API_BASE}/batch/${batchId}/buildings?limit=${limit}`)
  const json = await res.json()
  if (!json.success) throw new Error(json.error || "Failed to fetch batch buildings")
  return json.data as BatchBuildingsResult
}

export async function cancelBatch(batchId: string): Promise<{ batch_id: string; status: string; canceled: boolean }> {
  const res = await fetch(`${API_BASE}/batch/${batchId}/cancel`, { method: "POST" })
  const json = await res.json()
  if (!json.success) throw new Error(json.error || "Failed to cancel batch")
  return json.data
}

export async function analyzeExistingBatch(batchId: string): Promise<{ source_batch_id: string; batch_id: string; status: string }> {
  const res = await fetch(`${API_BASE}/batch/${batchId}/analyze`, { method: "POST" })
  const json = await res.json()
  if (!json.success) throw new Error(json.error || "Failed to start analysis for this site")
  return json.data
}

// ── Site-buildings API ────────────────────────────────────────────────────────

export interface SiteRecord {
  id: number
  name: string
  status: string
  total_buildings: number
  assessed_count: number
  critical_count: number
  boundary: unknown | null
  created_at: string | null
  updated_at: string | null
}

export interface SiteBuilding {
  osm_id: number
  centroid_lat: number
  centroid_lon: number
  area_m2: number
  polygon: unknown | null
  assessment_id: string | null
  severity: number | null
  damage_type: string | null
  structural_risk: string | null
  assessment_status: string | null
  input_type: string | null
  confidence: number | null
  recommended_action: string | null
  assessed_at: string | null
}

export interface SiteBuildingsResult {
  site_id: number
  site_name: string
  site_status: string
  total: number
  assessed: number
  buildings: SiteBuilding[]
}

export interface UnassignedUpload {
  id: string
  file_type: "ground_photo" | "video"
  lat: number
  lon: number
  filename: string
  uploaded_at: string | null
  worker_name: string | null
  nearby_osm_id: number | null
}

export interface UnassignedUploadsResult {
  count: number
  uploads: UnassignedUpload[]
}

export async function fetchUnassignedUploads(): Promise<UnassignedUploadsResult> {
  try {
    const res = await fetch(`${API_BASE}/batch/unassigned-uploads`)
    const json = await res.json()
    if (!json.success) return { count: 0, uploads: [] }
    return json.data as UnassignedUploadsResult
  } catch {
    return { count: 0, uploads: [] }
  }
}

export async function fetchSitesFull(): Promise<SiteRecord[]> {
  try {
    const res = await fetch(`${API_BASE}/batch/sites-full`)
    const json = await res.json()
    if (!json.success) return []
    return json.data as SiteRecord[]
  } catch {
    return []
  }
}

export async function fetchSiteBuildings(siteId: number, limit = 2000): Promise<SiteBuildingsResult | null> {
  try {
    const res = await fetch(`${API_BASE}/batch/sites/${siteId}/buildings?limit=${limit}`)
    const json = await res.json()
    if (!json.success) return null
    return json.data as SiteBuildingsResult
  } catch {
    return null
  }
}

// ── SSE stream ────────────────────────────────────────────────────────────────

/**
 * Subscribe to real-time SSE events for a batch.
 * Calls onEvent for each event, onComplete when done, onError on failure.
 * Returns a cleanup function to abort the stream.
 */
export function subscribeBatchStream(
  batchId: string,
  onEvent: (event: BatchSseEvent) => void,
  onComplete: () => void,
  onError: (err: Error) => void
): () => void {
  const controller = new AbortController()
  const url = `${API_BASE}/batch/${batchId}/stream`

  ;(async () => {
    try {
      const res = await fetch(url, { signal: controller.signal })
      if (!res.ok || !res.body) {
        throw new Error(`SSE connection failed: ${res.status}`)
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ""

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split("\n")
        buffer = lines.pop() ?? ""

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue
          const raw = line.slice(6).trim()
          if (!raw) continue
          try {
            const event = JSON.parse(raw) as BatchSseEvent
            onEvent(event)
            if (event.type === "batch_complete" || event.type === "stream_closed") {
              onComplete()
              return
            }
          } catch {
            // ignore malformed SSE lines
          }
        }
      }
      onComplete()
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        onError(err instanceof Error ? err : new Error(String(err)))
      }
    }
  })()

  return () => controller.abort()
}
