const API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

type Envelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

export type FieldWorker = {
  id: number
  name: string
  status: "available" | "busy"
  current_assessment_id: string | null
  current_site_name: string | null
}

type FieldWorkerListData = {
  success: boolean
  items: FieldWorker[]
}

type DispatchResult = {
  success: boolean
  updated_count: number
  assessment_ids: string[]
}

export async function fetchFieldWorkers(): Promise<FieldWorker[]> {
  const response = await fetch(`${API_BASE}/dispatch/field-workers?limit=200`)
  if (!response.ok) throw new Error(`Failed to fetch workers (${response.status})`)
  const payload = (await response.json()) as Envelope<FieldWorkerListData>
  if (!payload.success || !payload.data?.success) throw new Error(payload.error ?? "Failed to fetch workers")
  return payload.data.items ?? []
}

export async function dispatchAssessmentToWorker(
  assessmentId: string,
  workerName: string,
  createWorkerIfMissing = true
): Promise<DispatchResult> {
  const response = await fetch(`${API_BASE}/dispatch/assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      assessment_id: assessmentId,
      worker_name: workerName,
      create_worker_if_missing: createWorkerIfMissing,
    }),
  })
  const payload = (await response.json()) as Envelope<DispatchResult>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Dispatch failed")
  }
  return payload.data
}

export async function closeAssessment(assessmentId: string): Promise<DispatchResult> {
  const response = await fetch(`${API_BASE}/dispatch/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      assessment_id: assessmentId,
      status: "closed",
    }),
  })
  const payload = (await response.json()) as Envelope<DispatchResult>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Close status update failed")
  }
  return payload.data
}

export async function createFieldWorker(name: string): Promise<FieldWorker> {
  const response = await fetch(`${API_BASE}/dispatch/field-workers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  })
  const payload = (await response.json()) as Envelope<{ success: boolean; worker: FieldWorker }>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Failed to create field worker")
  }
  return payload.data.worker
}
