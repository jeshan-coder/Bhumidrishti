"use client"

import { X } from "lucide-react"
import type { PendingBatchRecord } from "@/lib/api/batches"

function getBatchRemaining(batch: PendingBatchRecord): number {
  const total = Math.max(0, Number(batch.total_buildings) || 0)
  const doneCount = Math.max(0, Number(batch.processed) || 0)
  const skippedCount = Math.max(0, Number(batch.skipped) || 0)
  return Math.max(
    0,
    batch.remaining_buildings == null
      ? total - doneCount - skippedCount
      : Number(batch.remaining_buildings),
  )
}

type PendingBatchCardsProps = {
  batches: PendingBatchRecord[]
  selectedBatchId: string | null
  onDismiss: (batchId: string) => void
  onShowInMap: (batch: PendingBatchRecord) => void
  onAnalyze: (batch: PendingBatchRecord) => void
}

export function PendingBatchCards({
  batches,
  selectedBatchId,
  onDismiss,
  onShowInMap,
  onAnalyze,
}: PendingBatchCardsProps) {
  if (batches.length === 0) return null

  return (
    <div className="absolute right-4 top-20 z-25 w-80 space-y-2">
      {batches.slice(0, 4).map((batch) => {
        const total = Math.max(0, Number(batch.total_buildings) || 0)
        const doneCount = Math.max(0, Number(batch.processed) || 0)
        const failedCount = Math.max(0, Number(batch.failed) || 0)
        const skippedCount = Math.max(0, Number(batch.skipped) || 0)
        const remaining = getBatchRemaining(batch)

        return (
          <div
            key={batch.batch_id}
            className={`rounded-lg border shadow-lg ${
              selectedBatchId === batch.batch_id
                ? "border-blue-400 bg-blue-50"
                : "border-amber-300 bg-amber-50"
            }`}
          >
            <div className="flex items-center justify-between border-b border-amber-200 px-3 py-2">
              <div className="min-w-0">
                <div className="truncate text-xs font-bold text-amber-900">
                  {batch.site_name || batch.batch_id}
                </div>
                <div className="text-[10px] text-amber-800">
                  {doneCount} done · {skippedCount} skipped · {failedCount} failed · of {total}
                </div>
              </div>
              <button
                type="button"
                onClick={() => onDismiss(batch.batch_id)}
                className="ml-2 rounded p-1 text-amber-800 hover:bg-amber-100"
                aria-label={`Dismiss ${batch.batch_id}`}
                title="Dismiss"
              >
                <X size={14} />
              </button>
            </div>
            <div className="flex items-center justify-between px-3 py-2">
              <span className="text-[11px] font-semibold text-amber-900">
                {remaining} building{remaining !== 1 ? "s" : ""} remaining
              </span>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => onShowInMap(batch)}
                  className="rounded border border-blue-300 bg-white px-2.5 py-1 text-[11px] font-semibold text-blue-900 hover:bg-blue-100"
                >
                  Show in map
                </button>
                <button
                  type="button"
                  onClick={() => onAnalyze(batch)}
                  className="rounded border border-amber-300 bg-white px-2.5 py-1 text-[11px] font-semibold text-amber-900 hover:bg-amber-100"
                >
                  Analyse
                </button>
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
}
