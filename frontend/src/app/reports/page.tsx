"use client"

import { useEffect, useMemo, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { toast } from "sonner"
import {
  fetchReports,
  reportDownloadUrl,
  streamReportGeneration,
  type ReportRecord,
  type ReportType,
} from "@/lib/api/reports"
import { fetchFieldTeams, type FieldTeam } from "@/lib/api/dispatch"

type DashboardDetails = {
  sites: Array<{ site_name: string }>
  triage: Array<{
    assessment_id: string
    site_name: string
    severity: number
    worker_name?: string
    status?: string
  }>
}

const API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "tr", label: "Turkish" },
  { code: "ar", label: "Arabic" },
  { code: "fr", label: "French" },
]

export default function ReportsPage() {
  const [reportType, setReportType] = useState<ReportType>("site")
  const [selectedFallbackTeam, setSelectedFallbackTeam] = useState("")
  const [language, setLanguage] = useState("en")
  const [selectedSiteName, setSelectedSiteName] = useState("")
  const [selectedAssessmentId, setSelectedAssessmentId] = useState("")
  const [streamingText, setStreamingText] = useState("")
  const [thinkingText, setThinkingText] = useState("")
  const [toolEvents, setToolEvents] = useState<string[]>([])
  const [streamStatus, setStreamStatus] = useState("Idle")
  const [isGenerating, setIsGenerating] = useState(false)
  const [reports, setReports] = useState<ReportRecord[]>([])
  const [currentReportId, setCurrentReportId] = useState<string | null>(null)
  const [details, setDetails] = useState<DashboardDetails>({ sites: [], triage: [] })
  const [availableTeams, setAvailableTeams] = useState<FieldTeam[]>([])
  const [isGenerateDialogOpen, setIsGenerateDialogOpen] = useState(false)
  const [isReportsDialogOpen, setIsReportsDialogOpen] = useState(false)

  const buildingOptions = useMemo(
    () =>
      details.triage
        .filter((item) => item.assessment_id)
        .map((item) => ({
          assessment_id: item.assessment_id,
          label: `${item.assessment_id} · ${item.site_name || "Unknown"} · Sev ${item.severity ?? "?"}`,
        })),
    [details.triage]
  )

  const siteOptions = useMemo(() => {
    const deduped = new Map<string, string>()
    for (const site of details.sites) {
      const normalized = (site.site_name ?? "").replace(/\s+/g, " ").trim()
      if (!normalized) continue
      const key = normalized.toLowerCase()
      if (!deduped.has(key)) deduped.set(key, normalized)
    }
    return Array.from(deduped.values()).sort((a, b) => a.localeCompare(b))
  }, [details.sites])

  const assignedAssignee = useMemo(() => {
    if (reportType === "site") {
      if (!selectedSiteName) return null
      const names = Array.from(
        new Set(
          details.triage
            .filter((item) => item.site_name === selectedSiteName)
            .map((item) => (item.worker_name ?? "").trim())
            .filter((name) => name.length > 0 && name.toLowerCase() !== "unknown")
        )
      )
      if (names.length === 0) return null
      return names.join(", ")
    }

    if (!selectedAssessmentId) return null
    const match = details.triage.find((item) => item.assessment_id === selectedAssessmentId)
    const name = (match?.worker_name ?? "").trim()
    if (!name || name.toLowerCase() === "unknown") return null
    return name
  }, [details.triage, reportType, selectedAssessmentId, selectedSiteName])

  const availableTeamNames = useMemo(
    () => availableTeams.filter((team) => team.status === "available").map((team) => team.name),
    [availableTeams]
  )

  const effectiveTeamName = assignedAssignee || selectedFallbackTeam || ""

  useEffect(() => {
    let active = true

    async function loadData() {
      try {
        const [detailsRes, reportRows, teams] = await Promise.all([
          fetch(`${API_BASE}/batch/dashboard-details`),
          fetchReports(100),
          fetchFieldTeams().catch(() => []),
        ])
        const detailsJson = await detailsRes.json()
        if (active && detailsJson.success) {
          setDetails((detailsJson.data as DashboardDetails) ?? { sites: [], triage: [] })
        }
        if (active) {
          setAvailableTeams(teams)
          setReports(reportRows)
          if (reportRows.length > 0) setCurrentReportId(reportRows[0].id)
        }
      } catch (error) {
        if (!active) return
        toast.error(error instanceof Error ? error.message : "Failed to load report options")
      }
    }

    void loadData()
    return () => {
      active = false
    }
  }, [])

  async function refreshReports() {
    try {
      const rows = await fetchReports(100)
      setReports(rows)
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to refresh reports")
    }
  }

  async function handleGenerate() {
    if (isGenerating) return
    if (reportType === "site" && !selectedSiteName) {
      toast.error("Select a site for site report")
      return
    }
    if (reportType === "building" && !selectedAssessmentId) {
      toast.error("Select an assessment for building report")
      return
    }
    if (!effectiveTeamName.trim()) {
      toast.error("No assigned team/worker found. Please choose an available team.")
      return
    }

    setStreamingText("")
    setThinkingText("")
    setToolEvents([])
    setStreamStatus("Starting report generation...")
    setIsGenerating(true)
    setIsGenerateDialogOpen(false)
    try {
      await streamReportGeneration(
        {
          report_type: reportType,
          site_name: reportType === "site" ? selectedSiteName : undefined,
          assessment_id: reportType === "building" ? selectedAssessmentId : undefined,
          team_name: effectiveTeamName.trim(),
          language,
          created_by: "coordinator",
        },
        {
          onProgress: (message) => {
            setStreamStatus(message)
          },
          onThinking: (text) => {
            setThinkingText(text)
          },
          onToken: (token) => {
            setStreamingText((prev) => prev + token)
          },
          onToolCall: (toolName) => {
            setToolEvents((prev) => {
              const entry = `→ ${toolName}`
              return prev.includes(entry) ? prev : [...prev, entry]
            })
          },
          onToolResult: (toolName, result) => {
            setToolEvents((prev) => {
              const entry = `✓ ${toolName}${typeof result.success === "boolean" && !result.success ? " (failed)" : ""}`
              return prev.includes(entry) ? prev : [...prev, entry]
            })
          },
          onDone: ({ report_id }) => {
            setCurrentReportId(report_id)
            setStreamStatus("Report ready")
            toast.success(`Report ${report_id} is ready`)
          },
        }
      )
      await refreshReports()
    } catch (error) {
      setStreamStatus("Generation failed")
      toast.error(error instanceof Error ? error.message : "Report generation failed")
    } finally {
      setIsGenerating(false)
      setThinkingText("")
    }
  }

  return (
    <main className="mx-auto h-[calc(100dvh-49px)] w-full max-w-screen-2xl overflow-hidden px-4 py-4 sm:px-6">
      <section className="mb-3 flex items-center justify-between">
        <h1 className="text-lg font-semibold text-zinc-900">Reports</h1>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => {
              void refreshReports()
              setIsReportsDialogOpen(true)
            }}
            className="rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-700"
          >
            Available Reports
          </button>
          <button
            type="button"
            onClick={() => setIsGenerateDialogOpen(true)}
            className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-semibold text-white"
          >
            Make Report
          </button>
        </div>
      </section>

      <section className="h-[calc(100%-44px)] rounded-xl border border-zinc-200 bg-white p-4 shadow-sm">
        {streamingText || isGenerating ? (
          <div className="flex h-full min-h-0 flex-col">
            {/* Header bar */}
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-zinc-500">Report Preview</span>
              <div className="flex items-center gap-2">
                <span className="text-xs text-emerald-700">{streamStatus}</span>
                {currentReportId && !isGenerating ? (
                  <a
                    href={reportDownloadUrl(currentReportId)}
                    target="_blank"
                    rel="noreferrer"
                    className="rounded-md border border-emerald-700 px-2 py-1 text-xs font-medium text-emerald-700"
                  >
                    Download PDF
                  </a>
                ) : null}
              </div>
            </div>

            {/* AI activity bubble — visible only while generating, auto-hides on done */}
            {isGenerating ? (
              <div className="mb-2 rounded-lg border border-[#CFE8DF] bg-[#F4FAF7] px-3 py-2 text-xs text-[#0b5f4b]">
                <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[#3A6F61]">
                  AI Activity
                </p>
                {thinkingText ? (
                  <p className="mb-1.5 leading-relaxed">{thinkingText}</p>
                ) : null}
                {toolEvents.length > 0 ? (
                  <div className="flex flex-wrap gap-1">
                    {toolEvents.map((event, idx) => (
                      <span
                        key={`${event}-${idx}`}
                        className="rounded bg-[#D6F0E5] px-1.5 py-0.5 text-[10px] font-medium text-[#085041]"
                      >
                        {event}
                      </span>
                    ))}
                  </div>
                ) : null}
                {!thinkingText && toolEvents.length === 0 ? (
                  <span className="animate-pulse">Gemma is thinking...</span>
                ) : null}
              </div>
            ) : null}

            {/* Report content area */}
            <div className="mx-auto h-full min-h-0 w-full max-w-[920px] overflow-auto rounded-md border border-zinc-200 bg-white p-8 shadow-inner">
              {isGenerating ? (
                <pre className="whitespace-pre-wrap text-sm leading-6 text-zinc-900">
                  {streamingText || ""}
                </pre>
              ) : streamingText ? (
                streamingText.trimStart().startsWith("<") ? (
                  <>
                    <style>{`
                      .report { font-family: system-ui, sans-serif; }
                      .report-title { font-size: 1.75rem; font-weight: 700; margin-bottom: 1rem; }
                      .section-heading { font-size: 1.15rem; font-weight: 600; border-bottom: 2px solid #e5e7eb; padding-bottom: 4px; margin: 1.5rem 0 0.75rem; }
                      .building-heading { font-size: 1rem; font-weight: 600; margin: 1rem 0 0.5rem; }
                      .summary-text { margin-bottom: 0.75rem; line-height: 1.7; }
                      .building-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem; margin-bottom: 1.5rem; }
                      .sev-border-5 { border-left: 4px solid #7f1d1d; }
                      .sev-border-4 { border-left: 4px solid #dc2626; }
                      .sev-border-3 { border-left: 4px solid #d97706; }
                      .sev-border-2 { border-left: 4px solid #ca8a04; }
                      .sev-border-1 { border-left: 4px solid #16a34a; }
                      .sev { display: inline-block; padding: 1px 6px; border-radius: 4px; font-weight: 600; font-size: 0.8rem; }
                      .sev-5 { background: #7f1d1d; color: white; }
                      .sev-4 { background: #dc2626; color: white; }
                      .sev-3 { background: #d97706; color: white; }
                      .sev-2 { background: #ca8a04; color: white; }
                      .sev-1 { background: #16a34a; color: white; }
                      .action-urgent { background: #7f1d1d; color: white; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
                      .action-high { background: #dc2626; color: white; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
                      .action-medium { background: #d97706; color: white; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
                      .action-low { background: #2563eb; color: white; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
                      .action-none { background: #6b7280; color: white; padding: 2px 8px; border-radius: 4px; font-weight: 700; }
                      .warn-tag { background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; padding: 2px 8px; border-radius: 4px; font-size: 0.78rem; margin-right: 4px; display: inline-block; }
                      .stats-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 0.75rem 0; }
                      .stat-badge { border-radius: 8px; padding: 8px 14px; min-width: 80px; text-align: center; }
                      .stat-total { background: #f3f4f6; color: #111827; }
                      .stat-extreme { background: #7f1d1d; color: white; }
                      .stat-critical { background: #dc2626; color: white; }
                      .stat-moderate { background: #d97706; color: white; }
                      .stat-life { background: #be123c; color: white; }
                      .stat-flood { background: #1d4ed8; color: white; }
                      .stat-num { font-size: 1.4rem; font-weight: 700; }
                      .stat-label { font-size: 0.7rem; margin-top: 2px; }
                      .data-grid { width: 100%; border-collapse: collapse; margin: 0.5rem 0; font-size: 0.85rem; }
                      .data-row { display: flex; border-bottom: 1px solid #f3f4f6; }
                      .data-row.alt { background: #f9fafb; }
                      .data-key { width: 40%; padding: 4px 8px; color: #6b7280; font-weight: 500; }
                      .data-val { width: 60%; padding: 4px 8px; color: #111827; }
                      .route-steps { margin: 0.5rem 0; }
                      .route-step { display: flex; gap: 8px; padding: 4px 0; align-items: baseline; font-size: 0.85rem; }
                      .step-num { background: #0f6e56; color: white; border-radius: 50%; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-size: 0.7rem; font-weight: 700; flex-shrink: 0; }
                      .step-instruction { flex: 1; }
                      .step-distance { color: #6b7280; white-space: nowrap; }
                      .life-banner { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 6px 12px; border-radius: 6px; font-weight: 600; margin: 6px 0; }
                      .priority-badge { background: #7f1d1d; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 700; }
                      .page-break { border-top: 2px dashed #e5e7eb; margin: 2rem 0; }
                      .map-placeholder, .img-placeholder { background: #f3f4f6; border: 1px dashed #d1d5db; border-radius: 6px; padding: 1.5rem; text-align: center; color: #9ca3af; font-size: 0.8rem; margin: 0.5rem 0; }
                      .pre-post-row { display: flex; gap: 12px; margin: 0.5rem 0; }
                      .pre-post-row > div { flex: 1; }
                      .img-label { font-size: 0.75rem; color: #6b7280; font-weight: 500; margin-bottom: 4px; }
                      .report-header { border-bottom: 2px solid #e5e7eb; padding-bottom: 1rem; margin-bottom: 1.5rem; }
                      .report-footer { border-top: 1px solid #e5e7eb; padding-top: 1rem; margin-top: 2rem; color: #6b7280; font-size: 0.78rem; }
                      .gemma-note { background: #f0fdf4; border-left: 3px solid #16a34a; padding: 8px 12px; margin: 0.75rem 0; border-radius: 0 6px 6px 0; font-style: italic; }
                      .action-block { background: #fff7ed; border: 1px solid #fed7aa; border-radius: 6px; padding: 10px 14px; margin: 0.75rem 0; }
                      .warnings-row { margin: 0.5rem 0; }
                      .route-section, .evacuation-section { margin: 0.75rem 0; }
                      .route-header { font-size: 0.85rem; font-weight: 600; color: #374151; margin-bottom: 6px; }
                      .situation-summary { margin: 1rem 0; }
                      .site-map-section, .safety-section { margin: 1rem 0; }
                      .building-photo { margin-bottom: 0.5rem; }
                    `}</style>
                    {/* eslint-disable-next-line react/no-danger */}
                    <div dangerouslySetInnerHTML={{ __html: streamingText }} />
                  </>
                ) : (
                  <article className="text-[15px] leading-7 text-zinc-900">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        h1: ({ children }) => <h1 className="mb-4 text-3xl font-bold text-zinc-900">{children}</h1>,
                        h2: ({ children }) => (
                          <h2 className="mb-3 mt-8 border-b border-zinc-200 pb-1 text-xl font-semibold text-zinc-900">
                            {children}
                          </h2>
                        ),
                        h3: ({ children }) => <h3 className="mb-2 mt-6 text-lg font-semibold text-zinc-900">{children}</h3>,
                        p: ({ children }) => <p className="mb-3 text-zinc-800">{children}</p>,
                        ul: ({ children }) => <ul className="mb-4 list-disc space-y-1 pl-6 text-zinc-800">{children}</ul>,
                        ol: ({ children }) => <ol className="mb-4 list-decimal space-y-1 pl-6 text-zinc-800">{children}</ol>,
                        table: ({ children }) => (
                          <div className="mb-4 overflow-x-auto">
                            <table className="w-full table-fixed border-collapse border border-zinc-300 text-sm">
                              {children}
                            </table>
                          </div>
                        ),
                        th: ({ children }) => (
                          <th className="border border-zinc-300 bg-zinc-100 px-2 py-1 text-left font-semibold">{children}</th>
                        ),
                        td: ({ children }) => <td className="align-top border border-zinc-300 px-2 py-1">{children}</td>,
                        img: ({ src, alt }) => (
                          // eslint-disable-next-line @next/next/no-img-element
                          <img
                            src={src ?? ""}
                            alt={alt ?? "report image"}
                            className="h-auto w-full rounded border border-zinc-200 object-contain"
                          />
                        ),
                      }}
                    >
                      {streamingText}
                    </ReactMarkdown>
                  </article>
                )
              ) : (
                <div className="text-sm text-zinc-500">Gemma is generating report...</div>
              )}
            </div>
          </div>
        ) : (
          <div className="flex h-full items-center justify-center">
            <button
              type="button"
              onClick={() => setIsGenerateDialogOpen(true)}
              className="rounded-lg bg-emerald-700 px-5 py-3 text-base font-semibold text-white"
            >
              Make Report
            </button>
          </div>
        )}
      </section>

      {isGenerateDialogOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-xl rounded-xl bg-white p-4 shadow-xl">
            <h2 className="text-base font-semibold text-zinc-900">Generate Report</h2>
            <p className="mt-1 text-sm text-zinc-600">Choose report target and start AI generation.</p>
            <div className="mt-4 space-y-3">
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setReportType("site")}
                  className={`rounded-md px-3 py-2 text-sm font-medium ${
                    reportType === "site" ? "bg-emerald-700 text-white" : "bg-zinc-100 text-zinc-700"
                  }`}
                >
                  Site Report
                </button>
                <button
                  type="button"
                  onClick={() => setReportType("building")}
                  className={`rounded-md px-3 py-2 text-sm font-medium ${
                    reportType === "building" ? "bg-emerald-700 text-white" : "bg-zinc-100 text-zinc-700"
                  }`}
                >
                  Building Report
                </button>
              </div>

              {reportType === "site" ? (
                <select
                  value={selectedSiteName}
                  onChange={(event) => setSelectedSiteName(event.target.value)}
                  className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm"
                >
                  <option value="">Select site</option>
                  {siteOptions.map((siteName) => (
                    <option key={siteName} value={siteName}>
                      {siteName}
                    </option>
                  ))}
                </select>
              ) : (
                <select
                  value={selectedAssessmentId}
                  onChange={(event) => setSelectedAssessmentId(event.target.value)}
                  className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm"
                >
                  <option value="">Select building assessment</option>
                  {buildingOptions.map((item) => (
                    <option key={item.assessment_id} value={item.assessment_id}>
                      {item.label}
                    </option>
                  ))}
                </select>
              )}

              <div>
                <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-zinc-600">
                  Assigned Team / Worker
                </label>
                {assignedAssignee ? (
                  <div className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-900">
                    {assignedAssignee}
                  </div>
                ) : (
                  <select
                    value={selectedFallbackTeam}
                    onChange={(event) => setSelectedFallbackTeam(event.target.value)}
                    className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm"
                  >
                    <option value="">Choose available team</option>
                    {availableTeamNames.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                )}
              </div>

              <select
                value={language}
                onChange={(event) => setLanguage(event.target.value)}
                className="w-full rounded-md border border-zinc-300 px-3 py-2 text-sm"
              >
                {LANGUAGES.map((item) => (
                  <option key={item.code} value={item.code}>
                    {item.label}
                  </option>
                ))}
              </select>
            </div>

            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setIsGenerateDialogOpen(false)}
                className="rounded-md border border-zinc-300 px-3 py-2 text-sm text-zinc-700"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => void handleGenerate()}
                disabled={isGenerating}
                className="rounded-md bg-emerald-700 px-3 py-2 text-sm font-semibold text-white disabled:opacity-60"
              >
                Generate Report
              </button>
            </div>
          </div>
        </div>
      ) : null}

      {isReportsDialogOpen ? (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
          <div className="w-full max-w-4xl rounded-xl bg-white p-4 shadow-xl">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="text-base font-semibold text-zinc-900">Available Reports</h2>
              <button
                type="button"
                onClick={() => setIsReportsDialogOpen(false)}
                className="rounded-md border border-zinc-300 px-2 py-1 text-sm text-zinc-700"
              >
                Close
              </button>
            </div>
            <div className="max-h-[60vh] overflow-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-zinc-200 text-xs uppercase text-zinc-500">
                  <tr>
                    <th className="py-2">Report ID</th>
                    <th className="py-2">Type</th>
                    <th className="py-2">Site/Assessment</th>
                    <th className="py-2">Team</th>
                    <th className="py-2">Language</th>
                    <th className="py-2">Status</th>
                    <th className="py-2">Download</th>
                  </tr>
                </thead>
                <tbody>
                  {reports.map((row) => (
                    <tr key={row.id} className="border-b border-zinc-100">
                      <td className="py-2 text-xs font-medium">{row.id}</td>
                      <td className="py-2 text-xs">{row.report_type}</td>
                      <td className="py-2 text-xs">{row.site_id || row.assessment_id || "N/A"}</td>
                      <td className="py-2 text-xs">{row.team_name || "N/A"}</td>
                      <td className="py-2 text-xs">{row.language || "N/A"}</td>
                      <td className="py-2 text-xs">{row.status}</td>
                      <td className="py-2 text-xs">
                        {row.status === "ready" ? (
                          <a
                            href={reportDownloadUrl(row.id)}
                            target="_blank"
                            rel="noreferrer"
                            className="rounded border border-emerald-700 px-2 py-1 text-emerald-700"
                          >
                            Download
                          </a>
                        ) : (
                          <span className="text-zinc-400">Pending</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ) : null}
    </main>
  )
}

