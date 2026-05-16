"use client"

import { useRef, useState } from "react"
import type { ComponentPropsWithoutRef } from "react"
import { Check, Copy, Maximize2, MessageCircle, Minimize2, Pencil, RotateCcw, X } from "lucide-react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { streamChatRequest } from "@/lib/api/chat"
import { ThinkingBubble } from "@/components/chat/thinking-bubble"
import { ToolCallCard, BatchProgressCard, toolResultSummary, type ToolCallStatus, type ActiveBatch } from "@/components/chat/tool-call-card"

// ── Message types ──────────────────────────────────────────────────────────

type UserMsg      = { kind: "user";      id: string; content: string; hidden?: boolean }
type AssistantMsg = { kind: "assistant"; id: string; content: string }
type ToolMsg      = {
  kind: "tool"
  id: string
  toolName: string
  args: Record<string, unknown>
  status: ToolCallStatus
  summary: string
}
type UiMsg = UserMsg | AssistantMsg | ToolMsg

let _seq = 0
const uid = () => `m${++_seq}`

function parseThink(raw: string): { thinking: string; answer: string; done: boolean } {
  const si = raw.indexOf("<think>")
  if (si === -1) return { thinking: "", answer: raw, done: true }
  const after = raw.slice(si + 7)
  const ei = after.indexOf("</think>")
  if (ei === -1) return { thinking: after, answer: "", done: false }
  return { thinking: after.slice(0, ei), answer: after.slice(ei + 8), done: true }
}

// ── Props ──────────────────────────────────────────────────────────────────

type SelectedBuildingChatContext = {
  label: string
  geometry: unknown
}

type FieldMapChatSidebarProps = {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
  onToolResult?: (toolName: string, result: Record<string, unknown>) => void
  selectedBuildingContext?: SelectedBuildingChatContext | null
  onClearSelectedBuildingContext?: () => void
  activeBatch?: ActiveBatch | null
}

// ── Component ──────────────────────────────────────────────────────────────

export function FieldMapChatSidebar({
  isOpen,
  onOpenChange,
  onToolResult,
  selectedBuildingContext,
  onClearSelectedBuildingContext,
  activeBatch,
}: FieldMapChatSidebarProps) {
  const [msgs, setMsgs]                   = useState<UiMsg[]>([])
  const [draft, setDraft]                 = useState("")
  const [sending, setSending]             = useState(false)
  const [modelThinking, setModelThinking] = useState("")
  const [thinkingStatus, setThinkingStatus] = useState("")
  const [thinkingKey, setThinkingKey]     = useState(0)
  const [editIdx, setEditIdx]             = useState<number | null>(null)
  const [copiedId, setCopiedId]           = useState<string | null>(null)
  const [isExpanded, setIsExpanded]       = useState(false)
  const abortRef = useRef<AbortController | null>(null)

  // Token estimate
  const SYS_TOKENS = 2500
  const MAX_TOKENS = 256000
  const estTokens = SYS_TOKENS + Math.ceil(
    msgs.reduce((s, m) => s + (m.kind !== "tool" ? m.content.length : 0), 0) / 4
  )
  const tokenPct = (estTokens / MAX_TOKENS) * 100
  const tokenBarColor =
    tokenPct > 80 ? "bg-red-500" :
    tokenPct > 60 ? "bg-orange-400" :
    tokenPct > 30 ? "bg-yellow-400" :
    "bg-[#0F6E56]"
  const tokenLabel = estTokens >= 1000 ? `~${(estTokens / 1000).toFixed(1)}k` : `~${estTokens}`

  const handleStop = () => {
    abortRef.current?.abort()
    setModelThinking("")
    setThinkingStatus("")
    setSending(false)
  }

  const handleNewConversation = () => {
    abortRef.current?.abort()
    setMsgs([])
    setDraft("")
    setModelThinking("")
    setThinkingStatus("")
    setSending(false)
    setEditIdx(null)
  }

  const chatHistory = (history: UiMsg[]) =>
    history
      .filter((m): m is UserMsg | AssistantMsg => m.kind === "user" || m.kind === "assistant")
      .map((m) => ({ role: m.kind as "user" | "assistant" | "system", content: m.content }))

  const stream = async (history: UiMsg[]) => {
    const ctrl = new AbortController()
    abortRef.current = ctrl
    const assistantId = uid()
    setMsgs([...history, { kind: "assistant", id: assistantId, content: "" }])
    setDraft("")
    setSending(true)
    setModelThinking("")
    setThinkingStatus("")
    setThinkingKey((k) => k + 1)

    let raw = ""
    let lastToolId: string | null = null

    try {
      await streamChatRequest(
        chatHistory(history),
        {
          onThinkingStatus: (text) => setThinkingStatus(text),
          onThinkingModel:  (text) => setModelThinking(text),

          onToolCall: (toolName, args) => {
            const toolId = uid()
            lastToolId = toolId
            setMsgs((prev) => [
              ...prev,
              { kind: "tool", id: toolId, toolName, args, status: "running", summary: "running…" },
            ])
          },

          onToolResult: (toolName, result) => {
            const { summary, status } = toolResultSummary(toolName, result)
            setMsgs((prev) =>
              prev.map((m) =>
                m.kind === "tool" && m.id === lastToolId
                  ? { ...m, status, summary }
                  : m
              )
            )
            lastToolId = null
            onToolResult?.(toolName, result)
          },

          onToken: (token) => {
            raw += token
            const { thinking, answer, done } = parseThink(raw)
            setMsgs((prev) =>
              prev.map((m) =>
                m.kind === "assistant" && m.id === assistantId
                  ? { ...m, content: answer.trimStart() }
                  : m
              )
            )
            if (!done) setModelThinking(thinking.trim())
            else { setModelThinking(""); setThinkingStatus("") }
          },

          onDone: () => {
            setModelThinking("")
            setThinkingStatus("")
            const { answer } = parseThink(raw)
            if (!answer.trim()) {
              setMsgs((prev) =>
                prev.map((m) =>
                  m.kind === "assistant" && m.id === assistantId
                    ? { ...m, content: "No response from model." }
                    : m
                )
              )
            }
          },
        },
        { signal: ctrl.signal }
      )
    } catch (err) {
      const aborted = err instanceof DOMException && err.name === "AbortError"
      if (aborted) {
        setMsgs((prev) =>
          prev.map((m) =>
            m.kind === "assistant" && m.id === assistantId && !m.content.trim()
              ? { ...m, content: "Response stopped." }
              : m
          )
        )
      } else {
        const errMsg = err instanceof Error ? err.message : "Failed to contact AI"
        setMsgs((prev) => [
          ...prev.filter((m) => !(m.kind === "assistant" && m.id === assistantId)),
          { kind: "assistant", id: uid(), content: `Error: ${errMsg}` },
        ])
      }
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null
      setSending(false)
      setModelThinking("")
      setThinkingStatus("")
    }
  }

  const send = async () => {
    const text = draft.trim()
    if (!text || sending) return
    const userMsg: UiMsg = { kind: "user", id: uid(), content: text }

    if (editIdx !== null) {
      const before = msgs
        .filter((m): m is UserMsg | AssistantMsg => m.kind === "user" || m.kind === "assistant")
        .slice(0, editIdx)
      setEditIdx(null)
      await stream([...before, userMsg])
      return
    }

    // Strip stale map context, inject fresh one if present
    const stripped = msgs.filter(
      (m): m is UiMsg => !(m.kind === "user" && m.hidden && m.content.startsWith("Persistent current-map context"))
    )
    const next: UiMsg[] = selectedBuildingContext
      ? [
          ...stripped,
          {
            kind: "user" as const,
            id: uid(),
            hidden: true,
            content: [
              "Persistent current-map context for this chat session.",
              `The user selected ${selectedBuildingContext.label} on the field map.`,
              "When the user refers to this/that/selected building later, use this context.",
              `geometry=${JSON.stringify(selectedBuildingContext.geometry)}`,
            ].join("\n"),
          },
          userMsg,
        ]
      : [...stripped, userMsg]

    if (selectedBuildingContext) onClearSelectedBuildingContext?.()
    await stream(next)
  }

  const retry = async (msgId: string) => {
    if (sending) return
    const idx = msgs.findIndex((m) => m.id === msgId && m.kind === "user")
    if (idx === -1) return
    const before = msgs.slice(0, idx).filter((m): m is UserMsg | AssistantMsg => m.kind === "user" || m.kind === "assistant")
    await stream([...before, msgs[idx] as UserMsg])
  }

  const editMsg = (msgId: string, visIdx: number) => {
    if (sending) return
    const m = msgs.find((m) => m.id === msgId)
    if (!m || m.kind !== "user") return
    setEditIdx(visIdx)
    setDraft(m.content)
  }

  const copyText = (id: string, text: string) => {
    void navigator.clipboard.writeText(text).then(() => {
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 1500)
    })
  }

  const visibleMsgs = msgs.filter((m) => !(m.kind === "user" && (m as UserMsg).hidden))
  const thinkingKey_ = msgs.filter((m) => m.kind === "user").length

  return (
    <>
      <button
        type="button"
        onClick={() => onOpenChange(true)}
        className="absolute left-4 top-4 z-20 rounded-md border border-[#0a5d49] bg-[#0F6E56] p-2 text-white shadow-sm transition-colors hover:bg-[#085041]"
        aria-label="Open chat sidebar"
      >
        <MessageCircle size={18} />
      </button>

      {isOpen && (
        <aside className={`absolute left-0 top-0 z-30 flex h-full w-full flex-col border-r border-[#D3D1C7] bg-[#FAFAF8] shadow-xl transition-[max-width] duration-300 ease-in-out ${isExpanded ? "max-w-2xl" : "max-w-sm"}`}>

          {/* Header */}
          <div className="flex items-center justify-between border-b border-[#D3D1C7] px-4 py-3">
            <h2 className="text-sm font-semibold text-[#085041]">Gemma4 Assistant</h2>
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={handleNewConversation}
                title="New conversation"
                className="rounded-md px-2 py-1 text-xs font-medium text-[#085041] hover:bg-[#E1F5EE]"
              >
                New Chat
              </button>
              <button
                type="button"
                onClick={() => setIsExpanded((p) => !p)}
                title={isExpanded ? "Collapse" : "Expand"}
                className="rounded-md p-1.5 text-[#085041] hover:bg-[#E1F5EE]"
              >
                {isExpanded ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
              </button>
              <button
                type="button"
                onClick={() => onOpenChange(false)}
                className="rounded-md p-1.5 text-[#085041] hover:bg-[#E1F5EE]"
              >
                <X size={14} />
              </button>
            </div>
          </div>

          {/* Message list */}
          <div className="flex-1 space-y-2 overflow-y-auto p-4">
            {activeBatch && <BatchProgressCard batch={activeBatch} />}

            {visibleMsgs.length === 0 && !activeBatch && (
              <p className="text-xs text-[#5a6b65]">Start by asking Gemma4 about this field area.</p>
            )}

            {visibleMsgs.map((msg, visIdx) => {
              if (msg.kind === "tool") {
                return (
                  <ToolCallCard
                    key={msg.id}
                    toolName={msg.toolName}
                    args={msg.args}
                    status={msg.status}
                    summary={msg.summary}
                  />
                )
              }

              if (msg.kind === "user") {
                return (
                  <div key={msg.id} className="group ml-6 rounded-lg bg-[#0F6E56] px-3 py-2 text-xs text-white">
                    <p>{msg.content}</p>
                    {!sending && (
                      <div className="mt-2 flex justify-end gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={() => void retry(msg.id)}
                          className="inline-flex h-6 w-6 items-center justify-center rounded border border-white/35 text-white hover:bg-white/10"
                          title="Retry"
                        >
                          <RotateCcw size={12} />
                        </button>
                        <button
                          type="button"
                          onClick={() => editMsg(msg.id, visIdx)}
                          className="inline-flex h-6 w-6 items-center justify-center rounded border border-white/35 text-white hover:bg-white/10"
                          title="Edit"
                        >
                          <Pencil size={12} />
                        </button>
                      </div>
                    )}
                  </div>
                )
              }

              // assistant
              if (!msg.content.trim()) return null
              return (
                <div key={msg.id} className="group mr-6 rounded-lg bg-[#E1F5EE] px-3 py-2 text-xs text-[#085041]">
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      p:   ({ children }: ComponentPropsWithoutRef<"p">)   => <p className="mb-2 last:mb-0">{children}</p>,
                      ul:  ({ children }: ComponentPropsWithoutRef<"ul">)  => <ul className="mb-2 list-disc space-y-1 pl-4 last:mb-0">{children}</ul>,
                      ol:  ({ children }: ComponentPropsWithoutRef<"ol">)  => <ol className="mb-2 list-decimal space-y-1 pl-4 last:mb-0">{children}</ol>,
                      code:({ children }: ComponentPropsWithoutRef<"code">)=> <code className="rounded bg-[#d8efe8] px-1 py-0.5 font-mono text-[11px]">{children}</code>,
                      pre: ({ children }: ComponentPropsWithoutRef<"pre">) => <pre className="mb-2 overflow-x-auto rounded bg-[#d8efe8] p-2 text-[11px] last:mb-0">{children}</pre>,
                    }}
                  >
                    {msg.content}
                  </ReactMarkdown>
                  {!sending && msg.content.trim() && (
                    <div className="mt-1.5 flex justify-end opacity-0 transition-opacity duration-150 group-hover:opacity-100">
                      <button
                        type="button"
                        onClick={() => copyText(msg.id, msg.content)}
                        className="inline-flex h-6 w-6 items-center justify-center rounded border border-[#0b5f4b]/25 text-[#0b5f4b] hover:bg-[#d0ece3]"
                        title="Copy"
                      >
                        {copiedId === msg.id ? <Check size={12} /> : <Copy size={12} />}
                      </button>
                    </div>
                  )}
                </div>
              )
            })}

            {/* Live thinking */}
            {sending && (modelThinking || thinkingStatus) && (
              <div className="mr-6 space-y-1.5">
                {thinkingStatus && (
                  <p className="flex items-center gap-1.5 text-[10px] font-mono text-[#3A6F61]">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[#0F6E56]" />
                    {thinkingStatus}
                  </p>
                )}
                {modelThinking && (
                  <ThinkingBubble text={modelThinking} resetKey={thinkingKey_} />
                )}
              </div>
            )}
          </div>

          {/* Input area */}
          <div className="border-t border-[#D3D1C7] p-3">
            {editIdx !== null && (
              <p className="mb-2 text-[11px] font-medium text-[#0b5f4b]">Editing selected question…</p>
            )}
            {selectedBuildingContext && (
              <div className="mb-2 flex items-center justify-between rounded-md border border-[#0F6E56] bg-[#E1F5EE] px-2.5 py-1.5 text-xs text-[#085041]">
                <span className="font-semibold">{selectedBuildingContext.label}</span>
                <button
                  type="button"
                  onClick={onClearSelectedBuildingContext}
                  className="rounded p-0.5 hover:bg-[#ccebe0]"
                >
                  <X size={13} />
                </button>
              </div>
            )}
            {/* Token bar */}
            <div className="mb-1.5 flex items-center gap-2">
              <div className="h-1 flex-1 overflow-hidden rounded-full bg-[#E6E3D8]">
                <div
                  className={`h-full rounded-full transition-all duration-300 ${tokenBarColor}`}
                  style={{ width: `${Math.min(tokenPct, 100)}%` }}
                />
              </div>
              <span className="shrink-0 text-[10px] font-mono text-[#5a6b65]">{tokenLabel} / 256K</span>
            </div>
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send() }
              }}
              placeholder="Ask Gemma4…"
              rows={3}
              className="w-full resize-none rounded-md border border-[#D3D1C7] bg-white px-3 py-2 text-xs text-[#1f2d28] outline-none focus:border-[#0F6E56]"
            />
            <div className="mt-2 grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={() => void send()}
                disabled={sending}
                className="rounded-md bg-[#0F6E56] px-3 py-2 text-xs font-semibold text-white hover:bg-[#085041] disabled:cursor-not-allowed disabled:bg-[#8cb8ad]"
              >
                {sending ? "Sending…" : editIdx !== null ? "Save & Send" : "Send"}
              </button>
              <button
                type="button"
                onClick={handleStop}
                disabled={!sending}
                className="rounded-md border border-[#0F6E56] bg-white px-3 py-2 text-xs font-semibold text-[#0F6E56] hover:bg-[#E1F5EE] disabled:cursor-not-allowed disabled:border-[#8cb8ad] disabled:text-[#8cb8ad]"
              >
                Stop
              </button>
            </div>
          </div>
        </aside>
      )}
    </>
  )
}
