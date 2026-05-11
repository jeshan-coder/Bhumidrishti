"use client"

import { Pencil, X } from "lucide-react"

type MapDrawControlsProps = {
  drawMode: boolean
  drawPointsCount: number
  onStartDraw: () => void
  onCancelDraw: () => void
}

// This component renders compact draw controls and hint banner for polygon selection.
export function MapDrawControls({
  drawMode,
  drawPointsCount,
  onStartDraw,
  onCancelDraw,
}: MapDrawControlsProps) {
  return (
    <>
      <div className="absolute left-4 top-24 z-20 flex flex-col gap-2">
        {drawMode ? (
          <button
            type="button"
            onClick={onCancelDraw}
            title="Cancel drawing"
            aria-label="Cancel drawing"
            className="flex h-8 w-8 items-center justify-center rounded-md border border-red-300 bg-white text-red-600 shadow-lg transition-all hover:bg-red-50"
          >
            <X size={14} />
          </button>
        ) : (
          <button
            type="button"
            onClick={onStartDraw}
            title="Draw analysis zone"
            aria-label="Draw analysis zone"
            className="flex h-8 w-8 items-center justify-center rounded-md border border-[#0F6E56] bg-[#0F6E56] text-white shadow-lg transition-all hover:bg-[#0C614D]"
          >
            <Pencil size={14} />
          </button>
        )}
      </div>

      {drawMode && (
        <div className="absolute left-1/2 top-4 z-30 -translate-x-1/2 rounded-lg border border-[#0F6E56] bg-white px-4 py-2 shadow-lg">
          <div className="flex items-center gap-3 text-sm font-medium text-[#17352b]">
            <Pencil size={14} className="text-[#0F6E56]" />
            <span>Click to add points · Right-click or click first point to finish ({drawPointsCount} pts)</span>
            <button
              type="button"
              onClick={onCancelDraw}
              className="ml-2 rounded-md border border-[#D3D1C7] px-2 py-0.5 text-xs text-red-600 hover:bg-red-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </>
  )
}
