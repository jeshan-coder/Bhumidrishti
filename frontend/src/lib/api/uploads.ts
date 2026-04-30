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

// This variable defines API base URL for browser-side requests.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

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
