"use client"

import { useEffect, useMemo, useRef, useState } from "react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { toast } from "sonner"
import {
  fetchReportContent,
  fetchReports,
  reportDownloadUrl,
  streamReportGeneration,
  type ReportRecord,
  type ReportType,
} from "@/lib/api/reports"
import { fetchFieldTeams, type FieldTeam } from "@/lib/api/dispatch"
import { ThinkingBubble } from "@/components/chat/thinking-bubble"

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const API_BASE =
  process.env.NEXT_PUBLIC_BACKEND_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "tr", label: "Turkish" },
  { code: "ar", label: "Arabic" },
  { code: "fr", label: "French" },
  { code: "es", label: "Spanish" },
  { code: "de", label: "German" },
  { code: "hi", label: "Hindi" },
  { code: "ur", label: "Urdu" },
  { code: "fa", label: "Persian" },
  { code: "ru", label: "Russian" },
  { code: "zh", label: "Chinese" },
  { code: "ja", label: "Japanese" },
]

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function StatusBadge({ status }: { status: string }) {
  const s = status.toLowerCase()
  if (s === "ready")
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] font-semibold text-emerald-700 ring-1 ring-inset ring-emerald-600/20">
        <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
        Ready
      </span>
    )
  if (s === "generating")
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700 ring-1 ring-inset ring-amber-600/20">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-500" />
        Generating
      </span>
    )
  if (s === "failed")
    return (
      <span className="inline-flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-[10px] font-semibold text-red-700 ring-1 ring-inset ring-red-600/20">
        <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
        Failed
      </span>
    )
  return (
    <span className="inline-flex items-center rounded-full bg-zinc-100 px-2 py-0.5 text-[10px] font-medium text-zinc-500">
      {status}
    </span>
  )
}

function TypeBadge({ type }: { type: ReportType }) {
  return type === "site" ? (
    <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-blue-700">
      Site
    </span>
  ) : (
    <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-zinc-500">
      {type}
    </span>
  )
}

function formatDate(iso: string | null) {
  if (!iso) return "—"
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    })
  } catch {
    return iso
  }
}

// ---------------------------------------------------------------------------
// Report card (sidebar list item)
// ---------------------------------------------------------------------------

function ReportCard({
  report,
  isActive,
  onClick,
}: {
  report: ReportRecord
  isActive: boolean
  onClick: () => void
}) {
  const target = report.site_id || report.assessment_id || "—"
  return (
    <button
      type="button"
      onClick={onClick}
      className={`w-full rounded-lg border px-3 py-2.5 text-left transition-all ${
        isActive
          ? "border-emerald-200 bg-emerald-50 shadow-sm"
          : "border-transparent bg-transparent hover:border-zinc-200 hover:bg-zinc-50"
      }`}
    >
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[11px] font-semibold text-zinc-700">
          {report.id}
        </span>
        <StatusBadge status={report.status} />
      </div>
      <div className="flex items-center gap-1.5">
        <TypeBadge type={report.report_type} />
        <span className="truncate text-[11px] text-zinc-500">{target}</span>
      </div>
      <div className="mt-1 flex items-center justify-between">
        <span className="text-[10px] text-zinc-400">
          {report.team_name || "—"} · {(report.language ?? "en").toUpperCase()}
        </span>
        <span className="text-[10px] text-zinc-400">{formatDate(report.created_at)}</span>
      </div>
    </button>
  )
}

// ---------------------------------------------------------------------------
// Report CSS
// ---------------------------------------------------------------------------

const REPORT_STYLES = `
.bd-report { font-family: system-ui, -apple-system, sans-serif; font-size: 14px; color: #1a1a1a; line-height: 1.65; }
.bd-report h1 { font-size: 1.6rem; font-weight: 700; color: #111; margin: 0 0 1rem; padding-bottom: 0.75rem; border-bottom: 2px solid #e5e7eb; }
.bd-report h2 { font-size: 1.15rem; font-weight: 700; color: #111; margin: 1.75rem 0 0.5rem; padding-bottom: 4px; border-bottom: 1px solid #f0f0f0; }
.bd-report h3 { font-size: 1rem; font-weight: 600; color: #1a1a1a; margin: 1.25rem 0 0.4rem; }
.bd-report h4 { font-size: 0.9rem; font-weight: 600; color: #374151; margin: 1rem 0 0.3rem; }
.bd-report p { margin: 0 0 0.75rem; }
.bd-report ul, .bd-report ol { margin: 0.5rem 0 0.75rem 1.25rem; padding: 0; }
.bd-report li { margin-bottom: 0.3rem; }
.bd-report strong { font-weight: 600; color: #111; }
.bd-report table { width: 100%; border-collapse: collapse; margin: 0.75rem 0; font-size: 0.85rem; }
.bd-report th { background: #f8f9fa; font-weight: 600; text-align: left; padding: 7px 10px; border: 1px solid #e5e7eb; color: #374151; }
.bd-report td { padding: 6px 10px; border: 1px solid #e5e7eb; vertical-align: top; }
.bd-report tr:nth-child(even) td { background: #fafafa; }
.bd-report img { max-width: 100%; height: auto; border-radius: 6px; border: 1px solid #e5e7eb; display: block; margin: 0.5rem 0; }
.bd-report a { color: #0f6e56; text-decoration: underline; }
.bd-report hr { border: none; border-top: 1px solid #e5e7eb; margin: 1.5rem 0; }
.bd-report code { font-size: 0.82em; background: #f3f4f6; padding: 1px 5px; border-radius: 3px; }
.bd-report pre { background: #f8f9fa; border: 1px solid #e5e7eb; border-radius: 6px; padding: 1rem; overflow-x: auto; font-size: 0.82em; margin: 0.75rem 0; }
.bd-report .section-heading { font-size: 1.15rem; font-weight: 700; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; margin: 1.75rem 0 0.75rem; }
.bd-report .building-card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; background: #fff; }
.bd-report .sev-border-5 { border-left: 4px solid #7f1d1d; }
.bd-report .sev-border-4 { border-left: 4px solid #dc2626; }
.bd-report .sev-border-3 { border-left: 4px solid #d97706; }
.bd-report .sev-border-2 { border-left: 4px solid #ca8a04; }
.bd-report .sev-border-1 { border-left: 4px solid #16a34a; }
.bd-report .sev { display: inline-block; padding: 1px 7px; border-radius: 4px; font-weight: 600; font-size: 0.78rem; }
.bd-report .sev-5 { background: #7f1d1d; color: #fff; }
.bd-report .sev-4 { background: #dc2626; color: #fff; }
.bd-report .sev-3 { background: #d97706; color: #fff; }
.bd-report .sev-2 { background: #ca8a04; color: #fff; }
.bd-report .sev-1 { background: #16a34a; color: #fff; }
.bd-report .stats-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 0.75rem 0 1.25rem; }
.bd-report .stat-badge { border-radius: 10px; padding: 10px 16px; min-width: 90px; text-align: center; }
.bd-report .stat-num { font-size: 1.5rem; font-weight: 700; line-height: 1; }
.bd-report .stat-label { font-size: 0.68rem; margin-top: 4px; opacity: 0.85; }
.bd-report .stat-total { background: #f3f4f6; color: #111; }
.bd-report .stat-extreme { background: #7f1d1d; color: #fff; }
.bd-report .stat-critical { background: #dc2626; color: #fff; }
.bd-report .stat-moderate { background: #d97706; color: #fff; }
.bd-report .stat-life { background: #be123c; color: #fff; }
.bd-report .stat-flood { background: #1d4ed8; color: #fff; }
.bd-report .warn-tag { background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; padding: 1px 7px; border-radius: 4px; font-size: 0.78rem; margin-right: 4px; display: inline-block; margin-bottom: 2px; }
.bd-report .action-block { background: #fffbeb; border: 1px solid #fde68a; border-radius: 8px; padding: 10px 14px; margin: 0.75rem 0; }
.bd-report .action-urgent { background: #7f1d1d; color: #fff; padding: 2px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8rem; }
.bd-report .action-high { background: #dc2626; color: #fff; padding: 2px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8rem; }
.bd-report .action-medium { background: #d97706; color: #fff; padding: 2px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8rem; }
.bd-report .action-low { background: #2563eb; color: #fff; padding: 2px 9px; border-radius: 4px; font-weight: 700; font-size: 0.8rem; }
.bd-report .life-banner { background: #fef2f2; border: 1px solid #fecaca; color: #991b1b; padding: 7px 12px; border-radius: 6px; font-weight: 600; margin: 6px 0; }
.bd-report .data-row { display: flex; border-bottom: 1px solid #f3f4f6; font-size: 0.85rem; }
.bd-report .data-row.alt { background: #f9fafb; }
.bd-report .data-key { width: 38%; padding: 5px 8px; color: #6b7280; font-weight: 500; }
.bd-report .data-val { width: 62%; padding: 5px 8px; color: #111; }
.bd-report .route-step { display: flex; gap: 8px; padding: 4px 0; align-items: baseline; font-size: 0.85rem; }
.bd-report .step-num { background: #0f6e56; color: #fff; border-radius: 50%; width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-size: 0.7rem; font-weight: 700; flex-shrink: 0; }
.bd-report .gemma-note { background: #f0fdf4; border-left: 3px solid #16a34a; padding: 8px 12px; margin: 0.75rem 0; border-radius: 0 6px 6px 0; font-style: italic; font-size: 0.88rem; }
.bd-report .map-placeholder, .bd-report .img-placeholder { background: #f3f4f6; border: 1px dashed #d1d5db; border-radius: 6px; padding: 1.5rem; text-align: center; color: #9ca3af; font-size: 0.8rem; margin: 0.5rem 0; }
.bd-report .pre-post-row { display: flex; gap: 12px; margin: 0.5rem 0; }
.bd-report .pre-post-row > div { flex: 1; }
.bd-report .img-label { font-size: 0.75rem; color: #6b7280; font-weight: 500; margin-bottom: 4px; }
.bd-report .report-footer { border-top: 1px solid #e5e7eb; padding-top: 1rem; margin-top: 2rem; color: #6b7280; font-size: 0.78rem; }
.bd-report .priority-badge { background: #7f1d1d; color: #fff; padding: 1px 8px; border-radius: 4px; font-size: 0.73rem; font-weight: 700; }
.bd-report .page-break { border-top: 2px dashed #e5e7eb; margin: 2rem 0; }
.bd-report .bd-page-break { border-top: 2px dashed #e5e7eb; margin: 2rem 0; }
/* ── V2 report layout ── */
.bd-report .bd-v2-header { text-align:center; padding:12px 0 16px; border-bottom:2px solid #d1fae5; margin-bottom:16px; }
.bd-report .bd-v2-logo { font-size:0.72rem; font-weight:700; letter-spacing:2px; text-transform:uppercase; color:#6b7280; margin-bottom:6px; }
.bd-report .bd-logo-badge { display:inline-block; background:#f59e0b; color:#1a1a1a; padding:1px 6px; border-radius:3px; font-size:0.65rem; vertical-align:middle; margin-left:6px; font-weight:700; }
.bd-report .bd-v2-title { font-size:1.8rem; font-weight:700; color:#0c4a2f; margin:0 0 4px; }
.bd-report .bd-v2-subtitle { font-size:0.9rem; color:#6b7280; margin-bottom:6px; }
.bd-report .bd-v2-meta-row { display:flex; gap:16px; justify-content:center; flex-wrap:wrap; font-size:0.78rem; color:#9ca3af; }
.bd-report .bd-shelter-block { background:#f0fdf4; border:1px solid #bbf7d0; border-radius:8px; padding:8px 14px; margin:12px 0; }
.bd-report .bd-shelter-name { font-weight:600; color:#166534; font-size:0.9rem; }
.bd-report .bd-shelter-meta { font-size:0.8rem; color:#4b7c5b; }
.bd-report .bd-narrative-block { background:#f0fdf4; border-left:3px solid #16a34a; padding:10px 14px; margin:12px 0; border-radius:0 6px 6px 0; font-size:0.9rem; line-height:1.65; }
.bd-report .bd-map-section { margin:12px 0; }
.bd-report .bd-map-section img { max-width:100%; height:auto; border-radius:8px; border:1px solid #e5e7eb; display:block; }
.bd-report .bd-map-site { width:100%; }
.bd-report .bd-map-building { max-height:380px; width:auto; }
.bd-report .bd-map-route { width:100%; }
.bd-report .bd-map-caption { font-size:0.72rem; color:#9ca3af; text-align:center; margin-top:4px; }
.bd-report .bd-building-header { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:8px; }
.bd-report .bd-assessment-id { font-size:1.05rem; font-weight:700; color:#0c4a2f; }
.bd-report .bd-building-coords { font-size:0.72rem; color:#9ca3af; margin-left:auto; }
.bd-report .bd-action-tag { display:inline-block; padding:2px 8px; border-radius:4px; font-size:0.7rem; font-weight:700; text-transform:uppercase; letter-spacing:0.3px; }
.bd-report .bd-action-urgent { background:#7f1d1d; color:#fff; }
.bd-report .bd-action-high   { background:#dc2626; color:#fff; }
.bd-report .bd-action-medium { background:#d97706; color:#fff; }
.bd-report .bd-action-low    { background:#ca8a04; color:#fff; }
.bd-report .bd-action-none   { background:#6b7280; color:#fff; }
.bd-report .bd-data-table { width:100%; border-collapse:collapse; margin:10px 0; font-size:0.85rem; }
.bd-report .bd-data-key { width:34%; padding:5px 8px; color:#6b7280; font-weight:500; background:#f9fafb; border-bottom:1px solid #f0f0f0; vertical-align:top; }
.bd-report .bd-data-val { width:66%; padding:5px 8px; border-bottom:1px solid #f0f0f0; }
.bd-report .bd-data-alt .bd-data-key, .bd-report .bd-data-alt .bd-data-val { background:#f3f4f6; }
.bd-report .bd-data-critical { font-weight:700; color:#991b1b; }
.bd-report .bd-data-blocked  { font-weight:700; color:#b45309; }
.bd-report .bd-media-section { margin:12px 0; }
.bd-report .bd-subsection-heading { font-size:0.85rem; font-weight:600; color:#374151; margin:8px 0 6px; }
.bd-report .bd-media-row { display:flex; gap:8px; flex-wrap:wrap; }
.bd-report .bd-media-item { flex:1; min-width:100px; max-width:200px; }
.bd-report .bd-media-item img { max-width:100%; height:auto; border-radius:5px; }
.bd-report .bd-media-label { font-size:0.72rem; color:#6b7280; margin-bottom:3px; }
.bd-report .bd-route-section { margin:12px 0; }
.bd-report .bd-route-meta { font-size:0.78rem; color:#6b7280; margin:4px 0 6px; }
.bd-report .bd-route-directions { margin-top:6px; }
.bd-report .bd-route-step { display:flex; gap:9px; padding:4px 0; align-items:baseline; font-size:0.85rem; }
.bd-report .bd-step-num { background:#0c4a2f; color:#fff; border-radius:50%; width:22px; height:22px; display:flex; align-items:center; justify-content:center; font-size:0.7rem; font-weight:700; flex-shrink:0; }
.bd-report .bd-step-text { flex:1; }
.bd-report .bd-warnings-row { margin-top:8px; }
.bd-report .bd-warn-tag { background:#fef3c7; color:#92400e; border:1px solid #fcd34d; padding:2px 7px; border-radius:4px; font-size:0.78rem; display:inline-block; margin-right:4px; margin-bottom:3px; }
.bd-report .bd-part-heading { font-size:1.3rem; font-weight:700; color:#0c4a2f; margin:2rem 0 0.75rem; padding-bottom:6px; border-bottom:2px solid #d1fae5; }
.bd-report .bd-section-heading { font-size:1rem; font-weight:700; color:#064e35; margin:1.5rem 0 0.5rem; padding-bottom:3px; border-bottom:1px solid #e5e7eb; }
.bd-report .bd-stats { display:flex; gap:8px; margin:12px 0 16px; flex-wrap:wrap; }
.bd-report .bd-stat { flex:1; min-width:70px; border-radius:8px; padding:10px 8px 8px; text-align:center; color:#fff; }
.bd-report .bd-stat-num { font-size:1.4rem; font-weight:700; line-height:1.1; }
.bd-report .bd-stat-label { font-size:0.65rem; margin-top:4px; opacity:0.85; text-transform:uppercase; }
.bd-report .bd-stat-gray  { background:#374151; }
.bd-report .bd-stat-dark  { background:#7f1d1d; }
.bd-report .bd-stat-red   { background:#dc2626; }
.bd-report .bd-stat-amber { background:#d97706; }
.bd-report .bd-stat-pink  { background:#9f1239; }
.bd-report .bd-stat-blue  { background:#1e40af; }
.bd-report .bd-stat-teal  { background:#0f6e56; }
/* ── Live token streaming ── */
@keyframes bd-blink { 0%, 100% { opacity: 1; } 50% { opacity: 0; } }
.bd-report .bd-cursor-blink { display:inline-block; width:2px; height:1.1em; background:#16a34a; vertical-align:text-bottom; margin-left:2px; animation:bd-blink 0.85s step-end infinite; }
.bd-report .bd-writing-live { background:#f0fff4; border-left-color:#60a5fa; opacity:0.88; white-space:pre-wrap; }
`

// ---------------------------------------------------------------------------
// Report content renderer
// ---------------------------------------------------------------------------

function ReportRenderer({ content }: { content: string }) {
  const isHtml = content.trimStart().startsWith("<")
  if (isHtml) {
    return (
      <>
        <style>{REPORT_STYLES}</style>
        {/* eslint-disable-next-line react/no-danger */}
        <div className="bd-report" dangerouslySetInnerHTML={{ __html: content }} />
      </>
    )
  }
  return (
    <article className="prose prose-zinc max-w-none text-[14px] leading-[1.65]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h1 className="mb-4 border-b border-zinc-200 pb-3 text-2xl font-bold text-zinc-900">
              {children}
            </h1>
          ),
          h2: ({ children }) => (
            <h2 className="mb-2 mt-7 border-b border-zinc-100 pb-1 text-lg font-semibold text-zinc-900">
              {children}
            </h2>
          ),
          h3: ({ children }) => (
            <h3 className="mb-2 mt-5 text-base font-semibold text-zinc-800">{children}</h3>
          ),
          p: ({ children }) => <p className="mb-3 text-zinc-800">{children}</p>,
          ul: ({ children }) => (
            <ul className="mb-3 list-disc space-y-1 pl-5 text-zinc-800">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="mb-3 list-decimal space-y-1 pl-5 text-zinc-800">{children}</ol>
          ),
          table: ({ children }) => (
            <div className="mb-4 overflow-x-auto rounded-lg border border-zinc-200">
              <table className="w-full border-collapse text-sm">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border-b border-zinc-200 bg-zinc-50 px-3 py-2 text-left text-xs font-semibold text-zinc-600">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border-b border-zinc-100 px-3 py-2 align-top text-zinc-800">
              {children}
            </td>
          ),
          img: ({ src, alt }) => (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={src ?? ""}
              alt={alt ?? ""}
              className="my-2 h-auto max-w-full rounded-lg border border-zinc-200"
            />
          ),
          code: ({ children }) => (
            <code className="rounded bg-zinc-100 px-1 py-0.5 text-[0.82em] text-zinc-700">
              {children}
            </code>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </article>
  )
}

// ---------------------------------------------------------------------------
// Generation activity panel
// ---------------------------------------------------------------------------

function GenerationPanel({
  streamStatus,
  thinkingText,
  thinkingResetKey,
  onStop,
}: {
  streamStatus: string
  thinkingText: string
  thinkingResetKey: number
  onStop: () => void
}) {
  return (
    <div className="flex flex-col gap-2.5 rounded-xl border border-emerald-100 bg-white p-3 shadow-sm">
      <div className="flex items-center gap-2">
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-600">
          <svg className="h-3 w-3 animate-spin text-white" fill="none" viewBox="0 0 24 24">
            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
          </svg>
        </span>
        <span className="flex-1 text-xs font-semibold text-emerald-800">{streamStatus}</span>
        <button
          type="button"
          onClick={onStop}
          className="flex items-center gap-1 rounded-md border border-red-200 bg-red-50 px-2 py-0.5 text-xs font-medium text-red-700 hover:bg-red-100 active:scale-95"
        >
          <svg className="h-3 w-3" fill="currentColor" viewBox="0 0 24 24">
            <rect x="6" y="6" width="12" height="12" rx="1" />
          </svg>
          Stop
        </button>
      </div>

      {thinkingText && (
        <ThinkingBubble text={thinkingText} resetKey={thinkingResetKey} />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Right-side report drawer
// ---------------------------------------------------------------------------

function ReportDrawer({
  isOpen,
  onClose,
  title,
  statusNode,
  downloadUrl,
  isGenerating,
  scrollRef,
  children,
}: {
  isOpen: boolean
  onClose: () => void
  title: string | null
  statusNode?: React.ReactNode
  downloadUrl?: string
  isGenerating?: boolean
  scrollRef?: React.RefObject<HTMLDivElement>
  children: React.ReactNode
}) {
  return (
    <>
      {/* Backdrop — click to close, but not while actively generating */}
      {isOpen && !isGenerating && (
        <div
          className="fixed inset-0 z-40 bg-black/20 backdrop-blur-[1px]"
          onClick={onClose}
        />
      )}

      {/* Drawer panel */}
      <div
        className={`fixed inset-y-0 right-0 z-50 flex w-[64vw] max-w-4xl flex-col bg-white shadow-2xl transition-transform duration-300 ease-in-out ${
          isOpen ? "translate-x-0" : "translate-x-full"
        }`}
      >
        {/* Drawer header */}
        <div className="flex h-12 shrink-0 items-center justify-between border-b border-zinc-200 px-5">
          <div className="flex min-w-0 items-center gap-2">
            {title ? (
              <span className="truncate font-mono text-sm font-semibold text-zinc-800">{title}</span>
            ) : (
              <span className="text-sm text-zinc-400">Report</span>
            )}
            {statusNode}
          </div>
          <div className="flex items-center gap-2">
            {downloadUrl && (
              <a
                href={downloadUrl}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1.5 rounded-lg border border-emerald-700 px-3 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50"
              >
                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5M16.5 12 12 16.5m0 0L7.5 12m4.5 4.5V3" />
                </svg>
                Download PDF
              </a>
            )}
            {!isGenerating && (
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg p-1.5 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600"
                aria-label="Close report"
              >
                <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* Drawer body — scrollable */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto">
          {children}
        </div>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Generate dialog
// ---------------------------------------------------------------------------

function GenerateDialog({
  isOpen,
  onClose,
  onGenerate,
  isGenerating,
  selectedSiteName,
  setSelectedSiteName,
  language,
  setLanguage,
  customLanguage,
  setCustomLanguage,
  assignedAssignee,
  selectedFallbackTeam,
  setSelectedFallbackTeam,
  availableTeamNames,
  siteOptions,
}: {
  isOpen: boolean
  onClose: () => void
  onGenerate: () => void
  isGenerating: boolean
  selectedSiteName: string
  setSelectedSiteName: (v: string) => void
  language: string
  setLanguage: (v: string) => void
  customLanguage: string
  setCustomLanguage: (v: string) => void
  assignedAssignee: string | null
  selectedFallbackTeam: string
  setSelectedFallbackTeam: (v: string) => void
  availableTeamNames: string[]
  siteOptions: string[]
}) {
  if (!isOpen) return null
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/50 p-4 backdrop-blur-sm">
      <div className="w-full max-w-lg overflow-hidden rounded-2xl bg-white shadow-2xl">
        <div className="border-b border-zinc-100 px-6 py-4">
          <h2 className="text-base font-semibold text-zinc-900">Generate Site Report</h2>
          <p className="mt-0.5 text-xs text-zinc-500">
            AI will analyse the site data and produce a formatted field report.
          </p>
        </div>

        <div className="space-y-5 px-6 py-5">
          {/* Site */}
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Site
            </label>
            <select
              value={selectedSiteName}
              onChange={(e) => setSelectedSiteName(e.target.value)}
              className="w-full rounded-lg border border-zinc-200 bg-white px-3 py-2.5 text-sm text-zinc-800 shadow-sm focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
            >
              <option value="">Select a site…</option>
              {siteOptions.map((name) => (
                <option key={name} value={name}>{name}</option>
              ))}
            </select>
          </div>

          {/* Team */}
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Field Team / Coordinator
            </label>
            {assignedAssignee ? (
              <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2.5">
                <span className="flex h-5 w-5 items-center justify-center rounded-full bg-emerald-600 text-[10px] font-bold text-white">✓</span>
                <span className="text-sm font-medium text-emerald-900">{assignedAssignee}</span>
                <span className="ml-auto text-[10px] text-emerald-600">Auto-assigned</span>
              </div>
            ) : (
              <select
                value={selectedFallbackTeam}
                onChange={(e) => setSelectedFallbackTeam(e.target.value)}
                className="w-full rounded-lg border border-zinc-200 bg-white px-3 py-2.5 text-sm text-zinc-800 shadow-sm focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
              >
                <option value="">Choose available team…</option>
                {availableTeamNames.map((name) => (
                  <option key={name} value={name}>{name}</option>
                ))}
              </select>
            )}
          </div>

          {/* Language */}
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-zinc-500">
              Report Language
            </label>
            <div className="grid grid-cols-4 gap-1.5">
              {LANGUAGES.map((lang) => (
                <button
                  key={lang.code}
                  type="button"
                  onClick={() => { setLanguage(lang.code); setCustomLanguage("") }}
                  className={`rounded-lg border py-1.5 text-xs font-semibold transition-all ${
                    language === lang.code && !customLanguage
                      ? "border-emerald-600 bg-emerald-600 text-white"
                      : "border-zinc-200 bg-white text-zinc-600 hover:border-zinc-300"
                  }`}
                >
                  {lang.label}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={customLanguage}
              onChange={(e) => setCustomLanguage(e.target.value)}
              placeholder="Other language (e.g. Swahili, Bengali, Greek…)"
              className="mt-2 w-full rounded-lg border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-800 placeholder:text-zinc-400 shadow-sm focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500"
            />
            {customLanguage.trim() && (
              <p className="mt-1 text-[11px] text-emerald-700">
                Report will be written in: <b>{customLanguage.trim()}</b>
              </p>
            )}
          </div>
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-zinc-100 px-6 py-4">
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg border border-zinc-200 px-4 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onGenerate}
            disabled={isGenerating}
            className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white shadow-sm hover:bg-emerald-800 disabled:opacity-60 active:scale-95"
          >
            {isGenerating ? "Generating…" : "Generate Report"}
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function ReportsPage() {
  const [selectedFallbackTeam, setSelectedFallbackTeam] = useState("")
  const [language, setLanguage] = useState("en")
  const [customLanguage, setCustomLanguage] = useState("")
  const [selectedSiteName, setSelectedSiteName] = useState("")

  // Streaming state
  const [liveHtmlSections, setLiveHtmlSections] = useState<string[]>([])
  const [streamingTokens, setStreamingTokens] = useState("")
  const [thinkingText, setThinkingText] = useState("")
  const [thinkingResetKey, setThinkingResetKey] = useState(0)
  const [streamStatus, setStreamStatus] = useState("")
  const [isGenerating, setIsGenerating] = useState(false)

  const [reports, setReports] = useState<ReportRecord[]>([])
  const [activeReportId, setActiveReportId] = useState<string | null>(null)
  const [loadedContent, setLoadedContent] = useState<string>("")
  const [isLoadingContent, setIsLoadingContent] = useState(false)

  const [details, setDetails] = useState<DashboardDetails>({ sites: [], triage: [] })
  const [availableTeams, setAvailableTeams] = useState<FieldTeam[]>([])
  const [isGenerateDialogOpen, setIsGenerateDialogOpen] = useState(false)
  const [isDrawerOpen, setIsDrawerOpen] = useState(false)

  const drawerScrollRef = useRef<HTMLDivElement>(null)
  const abortControllerRef = useRef<AbortController | null>(null)

  // ── derived ──────────────────────────────────────────────────────────────

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
    if (!selectedSiteName) return null
    const names = Array.from(
      new Set(
        details.triage
          .filter((item) => item.site_name === selectedSiteName)
          .map((item) => (item.worker_name ?? "").trim())
          .filter((name) => name.length > 0 && name.toLowerCase() !== "unknown")
      )
    )
    return names.length === 0 ? null : names.join(", ")
  }, [details.triage, selectedSiteName])

  const availableTeamNames = useMemo(
    () => availableTeams.filter((t) => t.status === "available").map((t) => t.name),
    [availableTeams]
  )

  const effectiveTeamName = assignedAssignee || selectedFallbackTeam || ""
  const liveHtml = liveHtmlSections.join("")

  // Build preview: completed sections + live-streaming tokens appended inline
  const streamingBlock = streamingTokens
    ? `<div class="bd-narrative-block bd-writing-live">${streamingTokens
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      }<span class="bd-cursor-blink"></span></div>`
    : ""
  const previewContent = isGenerating ? liveHtml + streamingBlock : loadedContent
  const activeReport = reports.find((r) => r.id === activeReportId)

  // Auto-scroll drawer as new sections or tokens arrive during generation
  useEffect(() => {
    if (!isGenerating) return
    if (drawerScrollRef.current) {
      drawerScrollRef.current.scrollTop = drawerScrollRef.current.scrollHeight
    }
  }, [liveHtmlSections, streamingTokens, isGenerating])

  // ── data loading ─────────────────────────────────────────────────────────

  useEffect(() => {
    let active = true
    async function loadData() {
      try {
        const [detailsRes, reportRows, teams] = await Promise.all([
          fetch(`${API_BASE}/batch/dashboard-details`),
          fetchReports(100),
          fetchFieldTeams().catch(() => [] as FieldTeam[]),
        ])
        const detailsJson = await detailsRes.json() as { success: boolean; data: DashboardDetails }
        if (!active) return
        if (detailsJson.success) setDetails(detailsJson.data ?? { sites: [], triage: [] })
        setAvailableTeams(teams)
        setReports(reportRows)
      } catch (err) {
        if (!active) return
        toast.error(err instanceof Error ? err.message : "Failed to load data")
      }
    }
    void loadData()
    return () => { active = false }
  }, [])

  async function refreshReports() {
    try {
      const rows = await fetchReports(100)
      setReports(rows)
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Failed to refresh reports")
    }
  }

  async function handleSelectReport(reportId: string) {
    setActiveReportId(reportId)
    setLiveHtmlSections([])
    setLoadedContent("")
    setIsDrawerOpen(true)
    setIsLoadingContent(true)
    try {
      const data = await fetchReportContent(reportId)
      setLoadedContent(data.markdown_content ?? "")
    } catch {
      setLoadedContent("")
    } finally {
      setIsLoadingContent(false)
    }
  }

  // ── generation ───────────────────────────────────────────────────────────

  async function handleGenerate() {
    if (isGenerating) return
    if (!selectedSiteName) {
      toast.error("Select a site to generate a report")
      return
    }
    if (!effectiveTeamName.trim()) {
      toast.error("No assigned team found — choose an available team.")
      return
    }

    setLiveHtmlSections([])
    setStreamingTokens("")
    setLoadedContent("")
    setThinkingText("")
    setThinkingResetKey((k) => k + 1)
    setStreamStatus("Starting…")
    setIsGenerating(true)
    setIsGenerateDialogOpen(false)
    setIsDrawerOpen(true)

    const controller = new AbortController()
    abortControllerRef.current = controller

    try {
      await streamReportGeneration(
        {
          report_type: "site",
          site_name: selectedSiteName,
          team_name: effectiveTeamName.trim(),
          language: customLanguage.trim() || language,
          created_by: "coordinator",
        },
        {
          onProgress: (msg) => {
            setStreamStatus(msg)
            setThinkingText("")   // clear between phases
            setStreamingTokens("")
          },

          onThinking: (text) => {
            setThinkingText(text)
            setStreamingTokens("")
          },

          onToken: (token) => {
            setStreamingTokens((prev) => prev + token)
            setThinkingText("")
          },

          onSection: (html) => {
            setLiveHtmlSections((prev) => [...prev, html])
            setStreamingTokens("")
            setThinkingText("")
          },

          onToolCall: () => {},
          onToolResult: () => {},

          onDone: ({ report_id }) => {
            setActiveReportId(report_id)
            setStreamStatus("Report ready")
            toast.success(`Report ${report_id} ready`)
          },
        },
        controller.signal
      )
      await refreshReports()
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        setStreamStatus("Stopped")
      } else {
        setStreamStatus("Generation failed")
        toast.error(err instanceof Error ? err.message : "Report generation failed")
      }
    } finally {
      abortControllerRef.current = null
      setLoadedContent((prev) => prev || liveHtmlSections.join(""))
      setIsGenerating(false)
      setThinkingText("")
      setStreamingTokens("")
    }
  }

  function handleStop() {
    abortControllerRef.current?.abort()
  }

  // ── render ───────────────────────────────────────────────────────────────

  return (
    <main className="flex h-[calc(100dvh-49px)] w-full overflow-hidden bg-[#f8f8f6]">

      {/* ── Left sidebar: report list ──────────────────────────────── */}
      <aside className="flex w-72 shrink-0 flex-col border-r border-zinc-200 bg-white">
        <div className="flex items-center justify-between border-b border-zinc-100 px-4 py-3">
          <h1 className="text-sm font-semibold text-zinc-900">Reports</h1>
          <button
            type="button"
            onClick={() => setIsGenerateDialogOpen(true)}
            className="flex items-center gap-1.5 rounded-lg bg-emerald-700 px-2.5 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-emerald-800 active:scale-95"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            New Report
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2">
          {isGenerating && (
            <div className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
              <div className="flex items-center gap-2">
                <svg className="h-3.5 w-3.5 animate-spin text-amber-600" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                </svg>
                <span className="text-xs font-semibold text-amber-800">Generating…</span>
              </div>
              <button
                type="button"
                onClick={() => setIsDrawerOpen(true)}
                className="mt-1.5 w-full rounded-md bg-amber-100 py-1 text-[10px] font-semibold text-amber-800 hover:bg-amber-200"
              >
                View live →
              </button>
            </div>
          )}

          {reports.length === 0 && !isGenerating ? (
            <div className="mt-8 px-2 text-center text-xs text-zinc-400">
              No reports yet.<br />Generate your first report.
            </div>
          ) : (
            <div className="space-y-1">
              {reports.map((report) => (
                <ReportCard
                  key={report.id}
                  report={report}
                  isActive={report.id === activeReportId && isDrawerOpen && !isGenerating}
                  onClick={() => { void handleSelectReport(report.id) }}
                />
              ))}
            </div>
          )}
        </div>
      </aside>

      {/* ── Main area: empty state / overview ─────────────────────── */}
      <div className="flex flex-1 flex-col items-center justify-center gap-6 px-8">
        <div className="flex h-20 w-20 items-center justify-center rounded-3xl bg-emerald-50">
          <svg className="h-10 w-10 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z" />
          </svg>
        </div>
        <div className="text-center">
          <p className="text-sm font-semibold text-zinc-800">
            {reports.length > 0 ? `${reports.length} report${reports.length !== 1 ? "s" : ""} available` : "No reports yet"}
          </p>
          <p className="mt-1 max-w-xs text-xs text-zinc-500">
            Select a report from the sidebar to open it, or generate a new field report from site assessment data.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setIsGenerateDialogOpen(true)}
          className="flex items-center gap-2 rounded-xl bg-emerald-700 px-5 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-emerald-800 active:scale-95"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
          </svg>
          Generate New Report
        </button>
      </div>

      {/* ── Right drawer: report viewer ────────────────────────────── */}
      <ReportDrawer
        isOpen={isDrawerOpen}
        onClose={() => setIsDrawerOpen(false)}
        title={
          isGenerating
            ? `Generating — ${selectedSiteName || "report"}`
            : activeReport?.id ?? null
        }
        statusNode={
          isGenerating ? (
            <StatusBadge status="generating" />
          ) : activeReport ? (
            <>
              <TypeBadge type={activeReport.report_type} />
              <StatusBadge status={activeReport.status} />
            </>
          ) : null
        }
        downloadUrl={
          activeReportId && !isGenerating && activeReport?.status === "ready"
            ? reportDownloadUrl(activeReportId)
            : undefined
        }
        isGenerating={isGenerating}
        scrollRef={drawerScrollRef}
      >
        {isLoadingContent ? (
          <div className="flex h-full items-center justify-center">
            <svg className="h-6 w-6 animate-spin text-emerald-600" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
            </svg>
          </div>
        ) : isGenerating ? (
          <div className="mx-auto max-w-3xl px-6 pt-5 pb-16">
            <div className="sticky top-0 z-10 pb-3">
              <GenerationPanel
                streamStatus={streamStatus}
                thinkingText={thinkingText}
                thinkingResetKey={thinkingResetKey}
                onStop={handleStop}
              />
            </div>
            {previewContent && <ReportRenderer content={previewContent} />}
          </div>
        ) : previewContent ? (
          <div className="mx-auto max-w-3xl px-6 py-7 pb-16">
            <ReportRenderer content={previewContent} />
          </div>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-zinc-400">
            No content available
          </div>
        )}
      </ReportDrawer>

      {/* ── Generate dialog ────────────────────────────────────────── */}
      <GenerateDialog
        isOpen={isGenerateDialogOpen}
        onClose={() => setIsGenerateDialogOpen(false)}
        onGenerate={() => void handleGenerate()}
        isGenerating={isGenerating}
        selectedSiteName={selectedSiteName}
        setSelectedSiteName={setSelectedSiteName}
        language={language}
        setLanguage={setLanguage}
        customLanguage={customLanguage}
        setCustomLanguage={setCustomLanguage}
        assignedAssignee={assignedAssignee}
        selectedFallbackTeam={selectedFallbackTeam}
        setSelectedFallbackTeam={setSelectedFallbackTeam}
        availableTeamNames={availableTeamNames}
        siteOptions={siteOptions}
      />
    </main>
  )
}
