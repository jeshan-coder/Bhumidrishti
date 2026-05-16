"use client"

export type ToolCallStatus = "running" | "done" | "empty"

// ── Batch progress card ────────────────────────────────────────────────────

export type ActiveBatch = {
  batchId: string
  siteName: string
  total: number
  processed: number
  failed: number
  skipped: number
  done: boolean
  stopped: boolean
  thought: string
  tokensUsed: number
}

export function BatchProgressCard({ batch }: { batch: ActiveBatch }) {
  const done  = batch.total > 0 ? batch.processed + batch.failed + batch.skipped : 0
  const pct   = batch.total > 0 ? Math.round((done / batch.total) * 100) : 0
  const label = batch.done
    ? batch.stopped ? "Batch stopped" : "Batch complete"
    : `Analysing — ${batch.siteName || "site"}`

  return (
    <div className={`rounded-lg border px-3 py-2.5 text-xs ${
      batch.done
        ? batch.stopped ? "border-zinc-200 bg-zinc-50" : "border-emerald-200 bg-emerald-50"
        : "border-blue-200 bg-blue-50"
    }`}>
      <div className="mb-2 flex items-center gap-1.5">
        <span className="shrink-0 select-none">
          {batch.done ? (batch.stopped ? "⏹" : "✅") : "🔄"}
        </span>
        <span className={`font-semibold ${
          batch.done ? (batch.stopped ? "text-zinc-600" : "text-emerald-900") : "text-blue-900"
        }`}>{label}</span>
        {!batch.done && (
          <span className="ml-auto font-mono text-[10px] text-blue-600">{pct}%</span>
        )}
      </div>

      <div className="mb-1.5 h-1.5 overflow-hidden rounded-full bg-white/70">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            batch.done ? (batch.stopped ? "bg-zinc-400" : "bg-emerald-500") : "bg-blue-500"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>

      <div className="flex flex-wrap gap-x-2.5 gap-y-0.5 text-[10px]">
        <span className="text-emerald-700">{batch.processed} done</span>
        {batch.failed  > 0 && <span className="text-red-600">{batch.failed} failed</span>}
        {batch.skipped > 0 && <span className="text-zinc-400">{batch.skipped} skipped</span>}
        <span className="text-zinc-400">of {batch.total}</span>
        {batch.tokensUsed > 0 && (
          <span className="ml-auto text-zinc-400">
            {batch.tokensUsed >= 1_000_000
              ? `${(batch.tokensUsed / 1_000_000).toFixed(1)}M`
              : batch.tokensUsed >= 1000
              ? `${Math.round(batch.tokensUsed / 1000)}k`
              : batch.tokensUsed} tok
          </span>
        )}
      </div>

      {!batch.done && batch.thought && (
        <p className="mt-1.5 text-[10px] italic leading-snug text-blue-700">
          {batch.thought}
        </p>
      )}
    </div>
  )
}

const TOOL_ICONS: Record<string, string> = {
  get_building_info:        "🏢",
  get_flood_zone:           "🌊",
  get_location_info:        "📍",
  get_nearest_road:         "🛣️",
  get_elevation_slope:      "⛰️",
  get_nearest_shelter:      "🏥",
  get_assessments:          "📋",
  get_sites:                "🗺️",
  get_field_teams:          "👥",
  get_field_workers:        "👤",
  dispatch_assessments:     "🚀",
  update_assessment_status: "✏️",
  execute_read_query:       "🔍",
  query_nearest_shelter:    "🏥",
  query_site_assessments:   "📋",
  create_site_static_map:   "🗺️",
  generate_site_summary:    "✍️",
  generate_building_narrative: "📝",
  get_centroid:                "📌",
}

const TOOL_LABELS: Record<string, string> = {
  get_building_info:        "Building Info",
  get_flood_zone:           "Flood Zone",
  get_location_info:        "Location",
  get_nearest_road:         "Nearest Road",
  get_elevation_slope:      "Elevation",
  get_nearest_shelter:      "Nearest Shelter",
  get_assessments:          "Assessments",
  get_sites:                "Sites",
  get_field_teams:          "Field Teams",
  get_field_workers:        "Field Workers",
  dispatch_assessments:     "Dispatch",
  update_assessment_status: "Update Status",
  execute_read_query:       "Query",
  query_nearest_shelter:    "Nearest Shelter",
  query_site_assessments:   "Site Assessments",
  create_site_static_map:   "Site Map",
  generate_site_summary:    "Site Summary",
  generate_building_narrative: "Building Narrative",
  get_centroid:                "Centroid",
}

export function toolArgPills(toolName: string, args: Record<string, unknown>): string[] {
  const pills: string[] = []
  if (args.status)             pills.push(String(args.status))
  if (args.team_name)          pills.push(String(args.team_name))
  if (args.worker_name)        pills.push(String(args.worker_name))
  if (args.site_name)          pills.push(String(args.site_name))
  if (args.province)           pills.push(String(args.province))
  if (args.assessment_id)      pills.push(String(args.assessment_id))
  if (args.severity != null)   pills.push(`sev ${args.severity}`)
  else if (args.severity_min != null) pills.push(`sev≥${args.severity_min}`)
  if (args.damage_type)        pills.push(String(args.damage_type))
  if (args.detail)             pills.push(String(args.detail).slice(0, 32))
  if (args.osm_id)                pills.push(`osm:${args.osm_id}`)
  if (typeof args.lat === "number") pills.push(`${(args.lat as number).toFixed(3)},${String(args.lon ?? "").slice(0,8)}`)
  return pills.slice(0, 3)
}

export function toolResultSummary(
  toolName: string,
  result: Record<string, unknown>
): { summary: string; status: "done" | "empty" } {
  if (result.found === false) return { summary: "no results", status: "empty" }
  if (Array.isArray(result.items)) {
    if (result.items.length === 0) return { summary: "no results", status: "empty" }
    const noun = toolName.includes("team") ? "team" : toolName.includes("assess") ? "assessment" : "result"
    return { summary: `${result.items.length} ${noun}${result.items.length !== 1 ? "s" : ""}`, status: "done" }
  }
  if (Array.isArray(result.results)) {
    if (result.results.length === 0) return { summary: "no results", status: "empty" }
    return { summary: `${result.results.length} found`, status: "done" }
  }
  if (result.success === false) return { summary: String(result.error ?? "failed"), status: "empty" }
  if (result.success === true) {
    const specific: Record<string, string> = {
      dispatch_assessments: "dispatched",
      update_assessment_status: "updated",
    }
    return { summary: specific[toolName] ?? "done", status: "done" }
  }
  if (toolName === "get_centroid" && result.found && typeof result.lat === "number" && typeof result.lon === "number") {
    return { summary: `${(result.lat as number).toFixed(4)}, ${(result.lon as number).toFixed(4)}`, status: "done" }
  }
  if (toolName === "get_nearest_shelter" && result.found) {
    const dist = typeof result.distance_m === "number" ? ` ${Math.round(result.distance_m as number)}m` : ""
    return { summary: `${result.shelter_type ?? "shelter"}${dist}`, status: "done" }
  }
  if (Object.keys(result).length > 0) return { summary: "done", status: "done" }
  return { summary: "no results", status: "empty" }
}

export function ToolCallCard({
  toolName,
  args,
  status,
  summary,
}: {
  toolName: string
  args: Record<string, unknown>
  status: ToolCallStatus
  summary: string
}) {
  const icon  = TOOL_ICONS[toolName]  ?? "🔧"
  const label = TOOL_LABELS[toolName] ?? toolName.replace(/_/g, " ")
  const pills = toolArgPills(toolName, args)

  return (
    <div className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-xs transition-colors ${
      status === "running"
        ? "border-amber-200 bg-amber-50"
        : status === "done"
        ? "border-emerald-200 bg-emerald-50"
        : "border-zinc-200 bg-zinc-50"
    }`}>
      <span className="mt-0.5 shrink-0 select-none">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1">
          <span className={`font-semibold ${
            status === "running" ? "text-amber-900" :
            status === "done"    ? "text-emerald-900" : "text-zinc-500"
          }`}>
            {label}
          </span>
          {pills.map((p, i) => (
            <span
              key={i}
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                status === "running" ? "bg-amber-100 text-amber-800" :
                status === "done"    ? "bg-emerald-100 text-emerald-800" :
                "bg-zinc-100 text-zinc-500"
              }`}
            >
              {p}
            </span>
          ))}
        </div>
        <p className={`mt-0.5 flex items-center gap-1 text-[10px] ${
          status === "running" ? "text-amber-600" :
          status === "done"    ? "text-emerald-700" : "text-zinc-400"
        }`}>
          {status === "running" && (
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />
          )}
          {status === "done"    && <span>✓</span>}
          {summary}
        </p>
      </div>
    </div>
  )
}
