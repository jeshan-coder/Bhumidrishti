const API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

type Envelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

export type FieldTeam = {
  id: number
  name: string
  status: "available" | "busy"
  current_assessment_id: string | null
  current_site_name: string | null
  workers?: string[]
  worker_count?: number
}

type FieldTeamListData = {
  success: boolean
  items: FieldTeam[]
}

type DispatchResult = {
  success: boolean
  updated_count: number
  assessment_ids: string[]
}

export async function fetchFieldTeams(): Promise<FieldTeam[]> {
  const response = await fetch(`${API_BASE}/dispatch/field-teams?limit=200`)
  if (!response.ok) throw new Error(`Failed to fetch teams (${response.status})`)
  const payload = (await response.json()) as Envelope<FieldTeamListData>
  if (!payload.success || !payload.data?.success) throw new Error(payload.error ?? "Failed to fetch teams")
  return payload.data.items ?? []
}

export async function dispatchAssessmentToTeam(
  assessmentId: string,
  teamName: string,
  workerName?: string,
  createTeamIfMissing = false
): Promise<DispatchResult> {
  const response = await fetch(`${API_BASE}/dispatch/assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      assessment_id: assessmentId,
      team_name: teamName,
      worker_name: workerName,
      create_team_if_missing: createTeamIfMissing,
    }),
  })
  const payload = (await response.json()) as Envelope<DispatchResult>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Dispatch failed")
  }
  return payload.data
}

export async function dispatchSiteToTeam(
  siteName: string,
  teamName: string,
  limit = 200
): Promise<DispatchResult> {
  const response = await fetch(`${API_BASE}/dispatch/assign`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      site_name: siteName,
      team_name: teamName,
      status: "pending",
      limit,
      create_team_if_missing: false,
    }),
  })
  const payload = (await response.json()) as Envelope<DispatchResult>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Site dispatch failed")
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

export async function closeSiteAssessments(
  siteName: string,
  currentStatus?: string,
  limit = 200
): Promise<DispatchResult> {
  const response = await fetch(`${API_BASE}/dispatch/status`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      site_name: siteName,
      status: "closed",
      current_status: currentStatus && currentStatus !== "All" ? currentStatus : undefined,
      limit,
    }),
  })
  const payload = (await response.json()) as Envelope<DispatchResult>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Close by site failed")
  }
  return payload.data
}

export async function createFieldTeam(name: string, workers: string[]): Promise<FieldTeam> {
  const response = await fetch(`${API_BASE}/dispatch/field-teams`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, workers }),
  })
  const payload = (await response.json()) as Envelope<{ success: boolean; team: FieldTeam }>
  if (!response.ok || !payload.success || !payload.data?.success) {
    throw new Error(payload.error ?? "Failed to create field team")
  }
  return payload.data.team
}
