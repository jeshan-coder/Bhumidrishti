// Report API client for markdown generation and downloads.

export type ReportType = "site" | "building"

export type ReportGenerateRequest = {
  report_type: ReportType
  site_name?: string
  site_id?: number
  assessment_id?: string
  team_name: string
  language: string
  created_by?: string
}

export type ReportRecord = {
  id: string
  report_type: ReportType
  site_id: string | null
  assessment_id: string | null
  team_name: string | null
  language: string | null
  file_path: string | null
  status: string
  created_by: string | null
  created_at: string | null
}

type ReportContentResponse = ReportRecord & {
  markdown_content: string
}

type Envelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

type StreamCallbacks = {
  onProgress: (message: string) => void
  onThinking: (text: string) => void
  onToken: (token: string) => void
  onSection: (html: string) => void
  onToolCall: (toolName: string, args: Record<string, unknown>) => void
  onToolResult: (toolName: string, result: Record<string, unknown>) => void
  onDone: (payload: { report_id: string; status: string; download_url: string }) => void
}

const API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

function parseStreamEvent(rawEvent: string): { event: string; data: unknown } | null {
  const lines = rawEvent.split("\n")
  let eventName = "message"
  const dataLines: string[] = []
  for (const line of lines) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim()
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim())
  }
  if (dataLines.length === 0) return null
  const merged = dataLines.join("\n")
  try {
    return { event: eventName, data: JSON.parse(merged) }
  } catch {
    return { event: eventName, data: merged }
  }
}

export async function streamReportGeneration(
  payload: ReportGenerateRequest,
  callbacks: StreamCallbacks,
  signal?: AbortSignal
): Promise<void> {
  const response = await fetch(`${API_BASE}/reports/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify(payload),
    signal,
  })
  if (!response.ok) {
    throw new Error(`Report stream failed with status ${response.status}`)
  }
  if (!response.body) {
    throw new Error("Report stream returned empty body")
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let doneReceived = false

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    buffer = buffer.replace(/\r\n/g, "\n")
    const blocks = buffer.split(/\n\n/)
    buffer = blocks.pop() ?? ""

    for (const block of blocks) {
      const parsed = parseStreamEvent(block)
      if (!parsed) continue

      if (parsed.event === "progress" && typeof parsed.data === "object" && parsed.data !== null) {
        const message = (parsed.data as { message?: unknown }).message
        if (typeof message === "string") callbacks.onProgress(message)
        continue
      }

      if (parsed.event === "thinking" && typeof parsed.data === "object" && parsed.data !== null) {
        const text = (parsed.data as { text?: unknown }).text
        if (typeof text === "string") callbacks.onThinking(text)
        continue
      }

      if (parsed.event === "token" && typeof parsed.data === "object" && parsed.data !== null) {
        const token = (parsed.data as { token?: unknown }).token
        if (typeof token === "string") callbacks.onToken(token)
        continue
      }

      if (parsed.event === "section" && typeof parsed.data === "object" && parsed.data !== null) {
        const html = (parsed.data as { html?: unknown }).html
        if (typeof html === "string") callbacks.onSection(html)
        continue
      }

      if (parsed.event === "tool_call" && typeof parsed.data === "object" && parsed.data !== null) {
        const name = (parsed.data as { name?: unknown }).name
        const rawArgs = (parsed.data as { arguments?: unknown }).arguments
        const args =
          typeof rawArgs === "object" && rawArgs !== null ? (rawArgs as Record<string, unknown>) : {}
        if (typeof name === "string") callbacks.onToolCall(name, args)
        continue
      }

      if (parsed.event === "tool_result" && typeof parsed.data === "object" && parsed.data !== null) {
        const name = (parsed.data as { name?: unknown }).name
        const rawResult = (parsed.data as { result?: unknown }).result
        const result =
          typeof rawResult === "object" && rawResult !== null ? (rawResult as Record<string, unknown>) : {}
        if (typeof name === "string") callbacks.onToolResult(name, result)
        continue
      }

      if (parsed.event === "error" && typeof parsed.data === "object" && parsed.data !== null) {
        const message = (parsed.data as { message?: unknown }).message
        throw new Error(typeof message === "string" ? message : "Report stream failed")
      }

      if (parsed.event === "done" && typeof parsed.data === "object" && parsed.data !== null) {
        const reportId = (parsed.data as { report_id?: unknown }).report_id
        const status = (parsed.data as { status?: unknown }).status
        const downloadUrl = (parsed.data as { download_url?: unknown }).download_url
        if (typeof reportId === "string") {
          doneReceived = true
          callbacks.onDone({
            report_id: reportId,
            status: typeof status === "string" ? status : "ready",
            download_url: typeof downloadUrl === "string" ? downloadUrl : `/reports/${reportId}/download`,
          })
        }
      }
    }
  }

  if (!doneReceived && !(signal?.aborted)) {
    throw new Error("Stream closed before done event")
  }
}

export async function fetchReports(limit = 100): Promise<ReportRecord[]> {
  const response = await fetch(`${API_BASE}/reports?limit=${limit}`)
  if (!response.ok) {
    throw new Error(`Failed to fetch reports (${response.status})`)
  }
  const payload = (await response.json()) as Envelope<ReportRecord[]>
  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to fetch reports")
  }
  return payload.data
}

export async function fetchReportContent(reportId: string): Promise<ReportContentResponse> {
  const response = await fetch(`${API_BASE}/reports/${reportId}`)
  if (!response.ok) {
    throw new Error(`Failed to fetch report ${reportId}`)
  }
  const payload = (await response.json()) as Envelope<ReportContentResponse>
  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Failed to fetch report content")
  }
  return payload.data
}

export function reportDownloadUrl(reportId: string): string {
  return `${API_BASE}/reports/${reportId}/download`
}

