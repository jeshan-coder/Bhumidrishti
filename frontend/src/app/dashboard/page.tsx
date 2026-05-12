"use client"

import { useEffect, useState } from "react"
import {
  closeAssessment,
  closeSiteAssessments,
  createFieldTeam,
  dispatchSiteToTeam,
  fetchFieldTeams,
  type FieldTeam as DispatchTeam,
} from "@/lib/api/dispatch"
import { toast } from "sonner"
import { DashboardChatSidebar } from "@/components/dashboard/dashboard-chat-sidebar"

type DashboardMetrics = {
  total_assessed: number
  critical: number
  pending_response: number
  responded: number
  active_sites: number
}

type DashboardDetails = {
  sites: SiteCardData[]
  severity_distribution: SeverityBar[]
  recent_activity: ActivityItem[]
  triage: TriageItem[]
  field_teams: FieldWorker[]
  field_workers?: FieldWorker[]
}

type SiteCardData = {
  site_name: string
  status: string
  total_buildings: number
  assessed_buildings: number
  created_by: string
  severity_breakdown: {
    sev5: number
    sev4: number
    sev3: number
    sev2: number
    sev1: number
  }
}

type SeverityBar = {
  severity: number
  count: number
}

type ActivityItem = {
  assessment_id: string
  severity: number
  building_id: number | null
  site_name: string
  worker_name: string
  input_type: string
  created_at: string | null
  signs_of_life: boolean
}

type TriageItem = ActivityItem & {
  status: string
}

type FieldWorker = {
  team_name?: string
  worker_name: string
  worker_count?: number
  workers?: string[]
  assessment_count: number
  last_activity_at: string | null
  status?: "available" | "busy"
}

const API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

const EMPTY_DETAILS: DashboardDetails = {
  sites: [],
  severity_distribution: [],
  recent_activity: [],
  triage: [],
  field_teams: [],
  field_workers: [],
}

function toTimeAgo(value: string | null): string {
  if (!value) return "just now"
  const ts = Date.parse(value)
  if (Number.isNaN(ts)) return "just now"
  const diffSec = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (diffSec < 60) return `${diffSec}s ago`
  const diffMin = Math.floor(diffSec / 60)
  if (diffMin < 60) return `${diffMin} min ago`
  const diffHour = Math.floor(diffMin / 60)
  if (diffHour < 24) return `${diffHour}h ago`
  return `${Math.floor(diffHour / 24)}d ago`
}

function severityColorClass(severity: number): string {
  if (severity >= 5) return "bg-red-600"
  if (severity === 4) return "bg-red-500"
  if (severity === 3) return "bg-amber-500"
  if (severity === 2) return "bg-lime-600"
  return "bg-green-700"
}

function statusPill(status: string): string {
  const value = status.toLowerCase()
  if (value === "processing") return "bg-emerald-100 text-emerald-800"
  if (value === "active") return "bg-teal-100 text-teal-800"
  if (value === "responded") return "bg-blue-100 text-blue-800"
  if (value === "closed") return "bg-slate-200 text-slate-700"
  if (value === "completed" || value === "complete") return "bg-slate-100 text-slate-700"
  return "bg-zinc-100 text-zinc-700"
}

export default function DashboardPage() {
  const [metrics, setMetrics] = useState<DashboardMetrics>({
    total_assessed: 0,
    critical: 0,
    pending_response: 0,
    responded: 0,
    active_sites: 0,
  })
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [details, setDetails] = useState<DashboardDetails>(EMPTY_DETAILS)
  const [selectedSiteFilter, setSelectedSiteFilter] = useState<string>("All")
  const [selectedStatusFilter, setSelectedStatusFilter] = useState<string>("All")
  const [isChatSidebarOpen, setIsChatSidebarOpen] = useState(false)
  const [dispatchWorkers, setDispatchWorkers] = useState<DispatchTeam[]>([])
  const [dispatchTargetSite, setDispatchTargetSite] = useState<string | null>(null)
  const [selectedWorkerName, setSelectedWorkerName] = useState("")
  const [newWorkerName, setNewWorkerName] = useState("")
  const [dispatchBusySite, setDispatchBusySite] = useState<string | null>(null)
  const [closeBusySite, setCloseBusySite] = useState<string | null>(null)
  const [closeBusyId, setCloseBusyId] = useState<string | null>(null)
  const [showAddWorkerDialog, setShowAddWorkerDialog] = useState(false)
  const [addWorkerName, setAddWorkerName] = useState("")
  const [addTeamWorkers, setAddTeamWorkers] = useState<string[]>([])
  const [addTeamWorkerInput, setAddTeamWorkerInput] = useState("")
  const [addWorkerBusy, setAddWorkerBusy] = useState(false)

  useEffect(() => {
    let isMounted = true

    const load = async (silent = false) => {
      try {
        if (!silent) setIsLoading(true)
        const [metricsRes, detailsRes, workers] = await Promise.all([
          fetch(`${API_BASE}/batch/dashboard-metrics`),
          fetch(`${API_BASE}/batch/dashboard-details`),
          fetchFieldTeams().catch(() => []),
        ])
        const metricsJson = await metricsRes.json()
        const detailsJson = await detailsRes.json()
        if (!metricsJson.success) {
          throw new Error(metricsJson.error || "Failed to load dashboard metrics")
        }
        if (!detailsJson.success) {
          throw new Error(detailsJson.error || "Failed to load dashboard details")
        }
        if (isMounted) {
          setMetrics(metricsJson.data as DashboardMetrics)
          setDetails((detailsJson.data as DashboardDetails) ?? EMPTY_DETAILS)
          setDispatchWorkers(workers)
          setError(null)
        }
      } catch (err) {
        if (!isMounted) return
        setError(err instanceof Error ? err.message : "Failed to load dashboard")
      } finally {
        if (isMounted && !silent) setIsLoading(false)
      }
    }

    void load(false)
    const intervalId = window.setInterval(() => {
      void load(true)
    }, 10000)

    return () => {
      isMounted = false
      window.clearInterval(intervalId)
    }
  }, [])

  const triageSites = [
    "All",
    ...Array.from(new Set(details.triage.map((item) => item.site_name).filter(Boolean))).sort(),
  ]
  const triageStatuses = ["All", "pending", "responded"]
  const filteredTriage = details.triage.filter((item) => {
    const siteMatch = selectedSiteFilter === "All" || item.site_name === selectedSiteFilter
    const statusMatch =
      selectedStatusFilter === "All" ||
      String(item.status || "")
        .toLowerCase()
        .trim() === selectedStatusFilter
    return siteMatch && statusMatch
  })
  const dispatchSites = Array.from(
    filteredTriage.reduce((acc, item) => {
      const key = item.site_name.trim()
      if (!key || key.toLowerCase() === "unknown") return acc
      const existing = acc.get(key) ?? { site_name: key, pending_count: 0, total_count: 0 }
      existing.total_count += 1
      if ((item.status || "").toLowerCase().trim() === "pending") {
        existing.pending_count += 1
      }
      acc.set(key, existing)
      return acc
    }, new Map<string, { site_name: string; pending_count: number; total_count: number }>())
  ).map((entry) => entry[1])
  const severityMax = Math.max(1, ...details.severity_distribution.map((row) => row.count))
  const fieldTeams: FieldWorker[] =
    details.field_teams.length > 0
      ? details.field_teams
      : (details.field_workers ?? []).map((worker) => ({
          ...worker,
          team_name: worker.worker_name,
          worker_count: worker.worker_count ?? 1,
          workers: worker.workers ?? [worker.worker_name],
        }))

  async function handleDispatch() {
    if (!dispatchTargetSite || dispatchBusySite) return
    const teamName = newWorkerName.trim() || selectedWorkerName.trim()
    if (!teamName) return
    if (dispatchTargetSite.toLowerCase() === "unknown") {
      toast.error("Cannot dispatch unknown site")
      return
    }
    try {
      setDispatchBusySite(dispatchTargetSite)
      const dispatchResult = await dispatchSiteToTeam(dispatchTargetSite, teamName, 200)
      setDispatchTargetSite(null)
      setSelectedWorkerName("")
      setNewWorkerName("")
      const [workers] = await Promise.all([fetchFieldTeams().catch(() => [])])
      setDispatchWorkers(workers)
      const detailsRes = await fetch(`${API_BASE}/batch/dashboard-details`)
      const detailsJson = await detailsRes.json()
      if (detailsJson.success) setDetails((detailsJson.data as DashboardDetails) ?? EMPTY_DETAILS)
      toast.success(`Dispatched ${dispatchResult.updated_count} buildings in ${dispatchTargetSite} to ${teamName}`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Dispatch failed"
      setError(msg)
      toast.error(msg)
    } finally {
      setDispatchBusySite(null)
    }
  }

  async function handleClose(assessmentId: string) {
    if (closeBusyId) return
    try {
      setCloseBusyId(assessmentId)
      await closeAssessment(assessmentId)
      const [workers] = await Promise.all([fetchFieldTeams().catch(() => [])])
      setDispatchWorkers(workers)
      const detailsRes = await fetch(`${API_BASE}/batch/dashboard-details`)
      const detailsJson = await detailsRes.json()
      if (detailsJson.success) setDetails((detailsJson.data as DashboardDetails) ?? EMPTY_DETAILS)
      toast.success("Assessment closed")
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Close update failed"
      setError(msg)
      toast.error(msg)
    } finally {
      setCloseBusyId(null)
    }
  }

  async function handleCloseSite(siteName: string) {
    if (closeBusySite) return
    try {
      setCloseBusySite(siteName)
      const closeResult = await closeSiteAssessments(
        siteName,
        selectedStatusFilter !== "All" ? selectedStatusFilter : undefined,
        200
      )
      const [workers] = await Promise.all([fetchFieldTeams().catch(() => [])])
      setDispatchWorkers(workers)
      const detailsRes = await fetch(`${API_BASE}/batch/dashboard-details`)
      const detailsJson = await detailsRes.json()
      if (detailsJson.success) setDetails((detailsJson.data as DashboardDetails) ?? EMPTY_DETAILS)
      toast.success(`Closed ${closeResult.updated_count} buildings in ${siteName}`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Close by site failed"
      setError(msg)
      toast.error(msg)
    } finally {
      setCloseBusySite(null)
    }
  }

  async function handleAddWorker() {
    const name = addWorkerName.trim()
    const workers = Array.from(new Set(addTeamWorkers.map((item) => item.trim()).filter(Boolean)))
    if (!name || workers.length === 0 || addWorkerBusy) return
    try {
      setAddWorkerBusy(true)
      const worker = await createFieldTeam(name, workers)
      setAddWorkerName("")
      setAddTeamWorkers([])
      setAddTeamWorkerInput("")
      setShowAddWorkerDialog(false)
      const [latestTeams] = await Promise.all([fetchFieldTeams().catch(() => [])])
      setDispatchWorkers(latestTeams)
      const detailsRes = await fetch(`${API_BASE}/batch/dashboard-details`)
      const detailsJson = await detailsRes.json()
      if (detailsJson.success) setDetails((detailsJson.data as DashboardDetails) ?? EMPTY_DETAILS)
      toast.success(`Team ${worker.name} added as available`)
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to add field team"
      setError(msg)
      toast.error(msg)
    } finally {
      setAddWorkerBusy(false)
    }
  }

  function handleAddTeamMember() {
    const memberName = addTeamWorkerInput.trim()
    if (!memberName) return
    setAddTeamWorkers((prev) => {
      const exists = prev.some((item) => item.toLowerCase() === memberName.toLowerCase())
      return exists ? prev : [...prev, memberName]
    })
    setAddTeamWorkerInput("")
  }

  function handleRemoveTeamMember(memberName: string) {
    setAddTeamWorkers((prev) => prev.filter((item) => item !== memberName))
  }

  return (
    <main className="relative min-h-[calc(100dvh-49px)] w-full bg-[#FAFAF8] p-6 lg:p-10">
      <div className={`mx-auto max-w-7xl transition-[padding] duration-300 ${isChatSidebarOpen ? "xl:pr-[24rem]" : ""}`}>
        {error && (
          <div className="mb-6 rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-700">
            {error}
          </div>
        )}

        {isLoading && !error ? (
          <div className="flex h-48 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-[#0F6E56] border-t-transparent" />
          </div>
        ) : (
          <>
            <section className="mb-5 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-5">
              <div className="rounded-2xl border border-[#E5E7EB] bg-white px-5 py-4 shadow-sm text-[#17352b]">
                <p className="text-xs font-semibold uppercase tracking-wide">Total assessed</p>
                <p className="mt-2 text-3xl font-bold">{metrics.total_assessed.toLocaleString()}</p>
              </div>
              <div className="rounded-2xl border border-red-200 bg-red-50 px-5 py-4 shadow-sm text-red-800">
                <p className="text-xs font-semibold uppercase tracking-wide">Critical</p>
                <p className="mt-2 text-3xl font-bold">{metrics.critical.toLocaleString()}</p>
              </div>
              <div className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4 shadow-sm text-amber-800">
                <p className="text-xs font-semibold uppercase tracking-wide">Pending response</p>
                <p className="mt-2 text-3xl font-bold">{metrics.pending_response.toLocaleString()}</p>
              </div>
              <div className="rounded-2xl border border-teal-200 bg-teal-50 px-5 py-4 shadow-sm text-teal-800">
                <p className="text-xs font-semibold uppercase tracking-wide">Responded</p>
                <p className="mt-2 text-3xl font-bold">{metrics.responded.toLocaleString()}</p>
              </div>
              <div className="rounded-2xl border border-teal-200 bg-teal-50 px-5 py-4 shadow-sm text-teal-800">
                <p className="text-xs font-semibold uppercase tracking-wide">Active sites</p>
                <p className="mt-2 text-3xl font-bold">{metrics.active_sites.toLocaleString()}</p>
              </div>
            </section>

            <section className="grid grid-cols-1 gap-5 xl:grid-cols-3">
              <div className="space-y-5 xl:col-span-2">
                <div className="rounded-2xl border border-[#D9D6CB] bg-white p-4 shadow-sm">
                  <div className="mb-3 flex items-center justify-between">
                    <h2 className="text-sm font-semibold text-[#17352b]">Sites</h2>
                  </div>
                  <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
                    {details.sites.map((site) => {
                      const total = Math.max(0, Number(site.total_buildings) || 0)
                      const assessed = Math.max(0, Number(site.assessed_buildings) || 0)
                      const progress = total > 0 ? Math.min(100, Math.round((assessed / total) * 100)) : 0
                      const isProcessing = String(site.status).toLowerCase() === "processing"
                      return (
                        <div
                          key={site.site_name}
                          className={`rounded-xl border p-3 ${
                            isProcessing ? "border-emerald-500 bg-emerald-50/30" : "border-[#E8E5DA] bg-[#FAFAF8]"
                          }`}
                        >
                          <div className="mb-2 flex items-start justify-between gap-2">
                            <p className="line-clamp-1 text-sm font-semibold text-[#17352b]">{site.site_name}</p>
                            <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${statusPill(site.status)}`}>
                              {site.status}
                            </span>
                          </div>
                          <div className="mb-1 h-2 overflow-hidden rounded-full bg-[#E8E6DD]">
                            <div className="h-full rounded-full bg-[#0F6E56]" style={{ width: `${progress}%` }} />
                          </div>
                          <p className="text-[11px] text-[#6b7280]">
                            {assessed} / {total} assessed
                          </p>
                          <div className="mt-2 flex flex-wrap gap-1">
                            <span className="rounded bg-red-600/90 px-1.5 py-0.5 text-[10px] text-white">
                              {site.severity_breakdown.sev5} extreme
                            </span>
                            <span className="rounded bg-red-500 px-1.5 py-0.5 text-[10px] text-white">
                              {site.severity_breakdown.sev4} critical
                            </span>
                            <span className="rounded bg-amber-500 px-1.5 py-0.5 text-[10px] text-white">
                              {site.severity_breakdown.sev3} moderate
                            </span>
                            <span className="rounded bg-lime-600 px-1.5 py-0.5 text-[10px] text-white">
                              {site.severity_breakdown.sev2} low
                            </span>
                            <span className="rounded bg-green-700 px-1.5 py-0.5 text-[10px] text-white">
                              {site.severity_breakdown.sev1} minimal
                            </span>
                          </div>
                          <p className="mt-2 text-[11px] text-[#6b7280]">by {site.created_by || "Unknown"}</p>
                        </div>
                      )
                    })}
                  </div>
                </div>

                <div className="rounded-2xl border border-[#D9D6CB] bg-white p-4 shadow-sm">
                  <h2 className="mb-3 text-sm font-semibold text-[#17352b]">Severity distribution</h2>
                  <div className="space-y-2">
                    {details.severity_distribution.map((row) => (
                      <div key={row.severity} className="grid grid-cols-[58px_1fr_86px] items-center gap-2">
                        <span className="text-xs font-medium text-[#4b5563]">Sev {row.severity}</span>
                        <div className="h-4 overflow-hidden rounded bg-[#EFECE3]">
                          <div
                            className={`h-full ${severityColorClass(row.severity)}`}
                            style={{ width: `${Math.max(2, Math.round((row.count / severityMax) * 100))}%` }}
                          />
                        </div>
                        <span className="text-right text-xs text-[#4b5563]">{row.count} buildings</span>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="rounded-2xl border border-[#D9D6CB] bg-white p-4 shadow-sm">
                  <h2 className="mb-3 text-sm font-semibold text-[#17352b]">Recent activity</h2>
                  <div className="space-y-2">
                    {details.recent_activity.slice(0, 5).map((item) => (
                      <div key={item.assessment_id} className="flex items-start justify-between gap-3 rounded-lg border border-[#EEEADD] px-3 py-2">
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold text-white ${severityColorClass(item.severity)}`}>
                              {item.severity}
                            </span>
                            <p className="truncate text-xs font-medium text-[#17352b]">
                              {item.building_id ? `Building ${item.building_id}` : "Building unknown"} · {item.site_name}
                            </p>
                          </div>
                          <p className="mt-0.5 text-[11px] text-[#6b7280]">
                            {item.worker_name} · {item.input_type.replace("_", " ")}
                          </p>
                        </div>
                        <span className="shrink-0 text-[11px] text-[#6b7280]">{toTimeAgo(item.created_at)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              </div>

              <div className="space-y-5">
                <div className="rounded-2xl border border-[#D9D6CB] bg-white p-4 shadow-sm">
                  <h2 className="mb-3 text-sm font-semibold text-[#17352b]">Triage list</h2>
                  <div className="mb-3 flex flex-wrap gap-1.5">
                    {triageSites.map((siteName) => (
                      <button
                        key={siteName}
                        onClick={() => setSelectedSiteFilter(siteName)}
                        className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
                          selectedSiteFilter === siteName
                            ? "bg-[#0F6E56] text-white"
                            : "bg-[#F0EEE7] text-[#374151]"
                        }`}
                      >
                        {siteName}
                      </button>
                    ))}
                  </div>
                  <div className="mb-3 flex flex-wrap gap-1.5">
                    {triageStatuses.map((status) => (
                      <button
                        key={status}
                        onClick={() => setSelectedStatusFilter(status)}
                        className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${
                          selectedStatusFilter === status
                            ? "bg-[#0F6E56] text-white"
                            : "bg-[#F0EEE7] text-[#374151]"
                        }`}
                      >
                        {status}
                      </button>
                    ))}
                  </div>
                  <div className="mb-3 space-y-2 rounded-lg border border-[#EEEADD] bg-[#FAFAF8] p-2">
                    <p className="text-[11px] font-semibold text-[#17352b]">Dispatch by site</p>
                    <div className="max-h-28 space-y-1 overflow-y-auto pr-1">
                      {dispatchSites.length === 0 ? (
                        <p className="text-[11px] text-[#6b7280]">No sites available for current filters.</p>
                      ) : (
                        dispatchSites.map((site) => (
                          <div key={site.site_name} className="flex items-center justify-between rounded bg-white px-2 py-1">
                            <p className="text-[11px] text-[#17352b]">
                              {site.site_name} · {site.pending_count} pending / {site.total_count} total
                            </p>
                            <button
                              onClick={() => {
                                setDispatchTargetSite(site.site_name)
                                setSelectedWorkerName("")
                                setNewWorkerName("")
                              }}
                              disabled={dispatchBusySite === site.site_name || site.pending_count === 0}
                              className="rounded-md bg-[#0F6E56] px-2 py-0.5 text-[10px] font-semibold text-white disabled:opacity-50"
                            >
                              {dispatchBusySite === site.site_name ? "Dispatching..." : "Dispatch site"}
                            </button>
                            <button
                              onClick={() => void handleCloseSite(site.site_name)}
                              disabled={closeBusySite === site.site_name || site.total_count === 0}
                              className="rounded-md bg-slate-700 px-2 py-0.5 text-[10px] font-semibold text-white disabled:opacity-50"
                            >
                              {closeBusySite === site.site_name ? "Closing..." : "Close site"}
                            </button>
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                  <div className="max-h-72 space-y-2 overflow-y-auto pr-1">
                    {filteredTriage.map((item) => (
                      <div key={item.assessment_id} className="rounded-lg border border-[#EEEADD] px-3 py-2">
                        <div className="flex items-center justify-between gap-2">
                          <div className="flex items-center gap-2">
                            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold text-white ${severityColorClass(item.severity)}`}>
                              {item.severity}
                            </span>
                            <span className="text-xs font-medium text-[#17352b]">
                              {item.building_id ? `OSM:${item.building_id}` : "OSM:unknown"}
                            </span>
                          </div>
                          <div className="flex items-center gap-1.5">
                            <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${statusPill(item.status)}`}>
                              {item.status}
                            </span>
                            {item.signs_of_life && (
                              <span className="rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-semibold text-white">
                                Signs of life
                              </span>
                            )}
                          </div>
                        </div>
                        <p className="mt-1 text-[11px] text-[#6b7280]">
                          {item.site_name} · {item.worker_name} · {toTimeAgo(item.created_at)}
                        </p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          <button
                            onClick={() => void handleClose(item.assessment_id)}
                            disabled={closeBusyId === item.assessment_id || item.status.toLowerCase() === "closed"}
                            className="rounded-md bg-slate-700 px-2.5 py-1 text-[11px] font-semibold text-white disabled:opacity-50"
                          >
                            {closeBusyId === item.assessment_id ? "Closing..." : "Close"}
                          </button>
                        </div>
                      </div>
                    ))}
                  </div>
                  {dispatchTargetSite && (
                    <div className="mt-3 rounded-lg border border-[#D9D6CB] bg-[#F8F7F2] p-3">
                      <p className="text-xs font-semibold text-[#17352b]">
                        Dispatch all pending buildings in site: {dispatchTargetSite}
                      </p>
                      <div className="mt-2 grid grid-cols-1 gap-2">
                        <select
                          value={selectedWorkerName}
                          onChange={(e) => setSelectedWorkerName(e.target.value)}
                          className="h-9 rounded-md border border-[#D9D6CB] bg-white px-2 text-xs"
                        >
                          <option value="">Select available team</option>
                          {dispatchWorkers.map((worker) => (
                            <option
                              key={worker.id}
                              value={worker.name}
                              disabled={worker.status === "busy"}
                            >
                              {worker.name} ({worker.status})
                            </option>
                          ))}
                        </select>
                        <input
                          value={newWorkerName}
                          onChange={(e) => setNewWorkerName(e.target.value)}
                          placeholder="Or type existing team name"
                          className="h-9 rounded-md border border-[#D9D6CB] bg-white px-2 text-xs"
                        />
                        <div className="flex gap-2">
                          <button
                            onClick={() => void handleDispatch()}
                            disabled={dispatchBusySite != null || (!selectedWorkerName && !newWorkerName.trim())}
                            className="rounded-md bg-[#0F6E56] px-3 py-1 text-xs font-semibold text-white disabled:opacity-50"
                          >
                            Confirm dispatch
                          </button>
                          <button
                            onClick={() => {
                              setDispatchTargetSite(null)
                              setSelectedWorkerName("")
                              setNewWorkerName("")
                            }}
                            className="rounded-md bg-zinc-200 px-3 py-1 text-xs font-semibold text-zinc-800"
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    </div>
                  )}
                </div>

                <div className="rounded-2xl border border-[#D9D6CB] bg-white p-4 shadow-sm">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <h2 className="text-sm font-semibold text-[#17352b]">Field teams</h2>
                    <button
                      onClick={() => {
                        setAddWorkerName("")
                        setAddTeamWorkers([])
                        setAddTeamWorkerInput("")
                        setShowAddWorkerDialog(true)
                      }}
                      className="flex h-6 w-6 items-center justify-center rounded-full bg-[#0F6E56] text-sm font-bold text-white"
                      aria-label="Add field team"
                      title="Add field team"
                    >
                      +
                    </button>
                  </div>
                  <div className="space-y-2">
                    {fieldTeams.map((worker) => {
                      const workerStatus = (worker.status ?? "available").toLowerCase()
                      const available = workerStatus === "available"
                      const teamName = worker.team_name ?? worker.worker_name
                      return (
                        <div key={teamName} className="flex items-center justify-between rounded-lg border border-[#EEEADD] px-3 py-2">
                          <div className="flex items-center gap-2">
                            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-[#E8E5DA] text-[11px] font-semibold text-[#17352b]">
                              {teamName
                                .split(" ")
                                .filter(Boolean)
                                .slice(0, 2)
                                .map((p) => p[0]?.toUpperCase() ?? "")
                                .join("")}
                            </div>
                            <div>
                              <p className="text-xs font-medium text-[#17352b]">{teamName}</p>
                              <p className="text-[11px] text-[#6b7280]">
                                {available ? "Available" : "Busy"} · {worker.worker_count ?? 1} workers · {worker.assessment_count} assessments
                              </p>
                              {!!worker.workers?.length && (
                                <p className="text-[11px] text-[#6b7280]">Members: {worker.workers.join(", ")}</p>
                              )}
                            </div>
                          </div>
                          <span className={`h-2.5 w-2.5 rounded-full ${available ? "bg-emerald-500" : "bg-amber-500"}`} />
                        </div>
                      )
                    })}
                  </div>
                </div>

                {showAddWorkerDialog && (
                  <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
                    <div className="w-full max-w-sm rounded-xl border border-[#D9D6CB] bg-white p-4 shadow-lg">
                      <h3 className="text-sm font-semibold text-[#17352b]">Add field team</h3>
                      <input
                        autoFocus
                        value={addWorkerName}
                        onChange={(e) => setAddWorkerName(e.target.value)}
                        placeholder="Team name"
                        className="mt-3 h-9 w-full rounded-md border border-[#D9D6CB] bg-white px-3 text-xs outline-none focus:border-[#0F6E56]"
                      />
                      <div className="mt-2 space-y-2">
                        <label className="block text-xs font-semibold text-[#17352b]">Field workers</label>
                        <div className="flex gap-2">
                          <input
                            value={addTeamWorkerInput}
                            onChange={(e) => setAddTeamWorkerInput(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") {
                                e.preventDefault()
                                handleAddTeamMember()
                              }
                            }}
                            placeholder="Worker name"
                            className="h-9 flex-1 rounded-md border border-[#D9D6CB] bg-white px-3 text-xs outline-none focus:border-[#0F6E56]"
                          />
                          <button
                            type="button"
                            onClick={handleAddTeamMember}
                            disabled={!addTeamWorkerInput.trim()}
                            className="h-9 rounded-md bg-[#0F6E56] px-3 text-sm font-bold text-white disabled:opacity-50"
                            aria-label="Add worker to team"
                            title="Add worker"
                          >
                            +
                          </button>
                        </div>
                        <div className="max-h-24 space-y-1 overflow-y-auto rounded-md border border-[#EEEADD] bg-[#FAFAF8] p-2">
                          {addTeamWorkers.length === 0 ? (
                            <p className="text-[11px] text-[#6b7280]">Add at least one worker</p>
                          ) : (
                            addTeamWorkers.map((member) => (
                              <div key={member} className="flex items-center justify-between rounded bg-white px-2 py-1 text-xs">
                                <span className="text-[#17352b]">{member}</span>
                                <button
                                  type="button"
                                  onClick={() => handleRemoveTeamMember(member)}
                                  className="text-[11px] font-semibold text-red-600"
                                >
                                  Remove
                                </button>
                              </div>
                            ))
                          )}
                        </div>
                      </div>
                      <div className="mt-3 flex justify-end gap-2">
                        <button
                          onClick={() => {
                            if (addWorkerBusy) return
                            setShowAddWorkerDialog(false)
                            setAddWorkerName("")
                            setAddTeamWorkers([])
                            setAddTeamWorkerInput("")
                          }}
                          className="rounded-md bg-zinc-200 px-3 py-1.5 text-xs font-semibold text-zinc-800"
                        >
                          Cancel
                        </button>
                        <button
                          onClick={() => void handleAddWorker()}
                          disabled={addWorkerBusy || !addWorkerName.trim() || addTeamWorkers.length === 0}
                          className="rounded-md bg-[#0F6E56] px-3 py-1.5 text-xs font-semibold text-white disabled:opacity-50"
                        >
                          {addWorkerBusy ? "Adding..." : "Add"}
                        </button>
                      </div>
                    </div>
                  </div>
                )}

              </div>
            </section>
          </>
        )}
      </div>
      <DashboardChatSidebar isOpen={isChatSidebarOpen} onOpenChange={setIsChatSidebarOpen} />
    </main>
  )
}
