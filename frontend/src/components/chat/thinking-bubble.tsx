"use client"

import { useEffect, useRef, useState } from "react"
import { ChevronDown, ChevronUp } from "lucide-react"

type ThinkingBubbleProps = {
  text: string
  // When true, force-collapse on every new run so a fresh thought starts compact.
  resetKey?: string | number
}

// Renders the live Gemma4 thinking stream as a compact 4-line preview with
// an expand / collapse toggle so the thought log never overruns the viewport.
export function ThinkingBubble({ text, resetKey }: ThinkingBubbleProps) {
  const [isExpanded, setIsExpanded] = useState(false)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // Collapse again whenever a new thinking session starts (new user prompt).
  useEffect(() => {
    setIsExpanded(false)
  }, [resetKey])

  // Auto-scroll to the latest token while the user keeps the preview collapsed.
  useEffect(() => {
    const node = scrollRef.current
    if (!node) return
    node.scrollTop = node.scrollHeight
  }, [text])

  if (!text) return null

  return (
    <div className="mr-6 rounded-lg border border-[#CFE8DF] bg-[#F4FAF7] px-3 py-2 text-xs text-[#0b5f4b]">
      <div className="mb-1 flex items-center justify-between gap-2">
        <p className="text-[10px] font-semibold uppercase tracking-wide text-[#3A6F61]">
          Thinking
        </p>
        <button
          type="button"
          onClick={() => setIsExpanded((prev) => !prev)}
          className="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10px] font-medium text-[#0b5f4b] hover:bg-[#E1F5EE]"
          aria-label={isExpanded ? "Collapse thinking" : "Expand thinking"}
        >
          {isExpanded ? (
            <>
              <ChevronUp size={12} /> Collapse
            </>
          ) : (
            <>
              <ChevronDown size={12} /> Expand
            </>
          )}
        </button>
      </div>

      <div
        ref={scrollRef}
        className={
          isExpanded
            ? "max-h-72 overflow-y-auto whitespace-pre-wrap leading-relaxed"
            : "max-h-[5.5rem] overflow-hidden whitespace-pre-wrap leading-relaxed"
        }
      >
        {text}
      </div>
    </div>
  )
}
