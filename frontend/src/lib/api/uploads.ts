// This file defines frontend helpers for uploads API operations.

// This type defines supported unfinished upload statuses.
export type UnfinishedUploadStatus = "uploaded" | "queued" | "processing" | "failed" | "done" | "skipped"

// This type defines upload item shape used by unfinished uploads UI.
export type UnfinishedUploadItem = {
  id: string
  original_filename: string
  saved_path: string
  file_type: string
  status: UnfinishedUploadStatus
  is_analyzed: boolean
  assessment_id: string | null
  error_message: string | null
  retry_count: number
  uploaded_at: string | null
  lat: number | null
  lon: number | null
  worker_name: string | null
  field_note: string | null
  progress_percent?: number | null
  analysis_stage?: string | null
  analysis_thought?: string | null
  analysis_active?: boolean
}

// This type defines a location group of uploads at the same coordinates.
export type LocationGroup = {
  group_id: string
  center_lat: number
  center_lon: number
  upload_count: number
  location_name: string | null
  uploads: UnfinishedUploadItem[]
}

// This type defines response from by-location endpoint.
export type UnfinishedUploadsByLocationResponse = {
  radius_meters: number
  location_groups: LocationGroup[]
  uploads_without_coords: UnfinishedUploadItem[]
  total_locations: number
  total_uploads_without_coords: number
  total_uploads: number
}

// This type defines one ongoing AI analysis item returned by backend progress endpoint.
export type OngoingAssessmentItem = {
  upload_id: string
  original_filename: string | null
  file_type: string | null
  lat: number | null
  lon: number | null
  worker_name: string | null
  field_note: string | null
  status: string
  progress_percent: number | null
  stage: string | null
  thought: string | null
  is_active: boolean
  assessment_id: string | null
  error_message: string | null
  updated_at: string | null
  uploaded_at: string | null
}

// This type defines backend payload for post-earthquake imagery map overlay.
type PostEarthquakeLayerResponseData = {
  layer: string
  feature_count: number
  geojson: {
    type: string
    features: Array<Record<string, unknown>>
  }
}

// This type defines the backend API envelope format.
type BackendEnvelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

// This type defines response data returned by unfinished uploads endpoint.
type UnfinishedUploadsResponseData = {
  statuses: string[]
  count: number
  uploads: UnfinishedUploadItem[]
}

// This type defines response data returned by ongoing assessments endpoint.
type OngoingAssessmentsResponseData = {
  count: number
  items: OngoingAssessmentItem[]
}

// This variable defines API base URL for browser-side requests.
const API_BASE_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

// This function fetches unfinished uploads that still require processing.
export async function fetchUnfinishedUploads(limit = 100): Promise<UnfinishedUploadItem[]> {
  const response = await fetch(`${API_BASE_URL}/uploads/unfinished?limit=${limit}`)

  if (!response.ok) {
    throw new Error(`Unfinished uploads request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<UnfinishedUploadsResponseData>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Invalid unfinished uploads response")
  }

  return payload.data.uploads
}

// This function triggers analysis for one upload record.
export async function triggerUploadAnalysis(uploadId: string): Promise<{ status: string }> {
  const response = await fetch(`${API_BASE_URL}/uploads/${uploadId}/analyze`, {
    method: "POST",
  })

  if (!response.ok) {
    throw new Error(`Analyze request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<{ status?: string }>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to start analysis")
  }

  return {
    status: payload.data.status ?? "processing",
  }
}

// This function fetches user-uploaded orthophoto/drone imagery as a map overlay layer.
export async function fetchPostEarthquakeLayer(maxFeatures = 5000): Promise<{
  featureCount: number
  geojson: { type: string; features: Array<Record<string, unknown>> }
}> {
  const response = await fetch(`${API_BASE_URL}/uploads/post-earthquake-layer?max_features=${maxFeatures}`)

  if (!response.ok) {
    throw new Error(`Post-earthquake layer request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<PostEarthquakeLayerResponseData>
  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to load post-earthquake layer")
  }

  return {
    featureCount: payload.data.feature_count,
    geojson: payload.data.geojson,
  }
}

// This function fetches unfinished uploads grouped by geographic location.
export async function fetchUnfinishedUploadsByLocation(
  radiusMeters = 10,
  limit = 100
): Promise<UnfinishedUploadsByLocationResponse> {
  const url = `${API_BASE_URL}/uploads/by-location?radius_meters=${radiusMeters}&limit=${limit}`
  const response = await fetch(url)

  if (!response.ok) {
    throw new Error(`Location-grouped uploads request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<UnfinishedUploadsByLocationResponse>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Invalid location-grouped uploads response")
  }

  return payload.data
}

// This function fetches currently ongoing AI analyses with live progress state.
export async function fetchOngoingAssessments(): Promise<OngoingAssessmentItem[]> {
  const response = await fetch(`${API_BASE_URL}/uploads/ongoing-assessments`)

  if (!response.ok) {
    throw new Error(`Ongoing assessments request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<OngoingAssessmentsResponseData>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Invalid ongoing assessments response")
  }

  return payload.data.items
}

// This function triggers batch analysis for all uploads at a location.
// The backend pipeline will process all images together as one assessment.
export async function triggerLocationBatchAnalysis(uploadIds: string[]): Promise<{ started: number }> {
  const response = await fetch(`${API_BASE_URL}/uploads/analyze-location`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ upload_ids: uploadIds }),
  })

  if (!response.ok) {
    throw new Error(`Location analysis request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<{ started?: number }>
  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to start location analysis")
  }

  const started = payload.data.started ?? 0

  return { started }
}

// This function cancels a running upload analysis task.
export async function cancelUploadAnalysis(uploadId: string): Promise<{ status: string }> {
  const response = await fetch(`${API_BASE_URL}/uploads/${uploadId}/cancel-analysis`, {
    method: "POST",
  })

  if (!response.ok) {
    throw new Error(`Cancel analysis request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<{ status?: string }>
  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to cancel analysis")
  }

  return {
    status: payload.data.status ?? "canceled",
  }
}

// This function retries upload analysis in background.
export async function retryUploadAnalysis(uploadId: string): Promise<{ status: string }> {
  const response = await fetch(`${API_BASE_URL}/uploads/${uploadId}/retry-analysis`, {
    method: "POST",
  })

  if (!response.ok) {
    throw new Error(`Retry analysis request failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<{ status?: string }>
  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to retry analysis")
  }

  return {
    status: payload.data.status ?? "processing",
  }
}
