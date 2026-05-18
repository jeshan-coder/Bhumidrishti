"use client"

import { useRef, useState } from "react"
import type { ComponentPropsWithoutRef } from "react"
import { MessageSquare, Pencil, RotateCcw, X } from "lucide-react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { streamChatRequest } from "@/lib/api/chat"
import { ThinkingBubble } from "@/components/chat/thinking-bubble"
import { ToolCallCard, toolResultSummary, type ToolCallStatus } from "@/components/chat/tool-call-card"

// ── Message types ──────────────────────────────────────────────────────────

type UserMsg      = { kind: "user";      id: string; content: string }
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

type DashboardChatSidebarProps = {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

const SYSTEM_PROMPT =
  "You are BhumiDrishti dashboard coordinator assistant. Use tools for factual coordination actions. " +
  "For dispatch requests: first check available teams using get_field_teams, do not dispatch to busy teams, " +
  "use dispatch_assessments to assign and set responded, and use update_assessment_status for closed."

// ── Component ──────────────────────────────────────────────────────────────

export function DashboardChatSidebar({ isOpen, onOpenChange }: DashboardChatSidebarProps) {
  const [msgs, setMsgs]           = useState<UiMsg[]>([])
  const [draft, setDraft]         = useState("")
  const [sending, setSending]     = useState(false)
  const [modelThinking, setModelThinking]   = useState("")
  const [thinkingStatus, setThinkingStatus] = useState("")
  const [thinkingKey, setThinkingKey]       = useState(0)
  const [editIdx, setEditIdx]     = useState<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const handleStop = () => {
    abortRef.current?.abort()
    setModelThinking("")
    setThinkingStatus("")
    setSending(false)
  }

  const chatPayload = (history: UiMsg[]) => [
    { role: "system" as const, content: SYSTEM_PROMPT },
    ...history
      .filter((m): m is UserMsg | AssistantMsg => m.kind === "user" || m.kind === "assistant")
      .map((m) => ({ role: m.kind as "user" | "assistant", content: m.content })),
  ]

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
        chatPayload(history),
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
        const msg = err instanceof Error ? err.message : "Failed to contact AI"
        setMsgs((prev) => [
          ...prev.filter((m) => !(m.kind === "assistant" && m.id === assistantId)),
          { kind: "assistant", id: uid(), content: `Error: ${msg}` },
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
      const before = msgs.filter((m): m is UserMsg | AssistantMsg => m.kind === "user" || m.kind === "assistant").slice(0, editIdx)
      setEditIdx(null)
      await stream([...before, userMsg])
      return
    }
    await stream([...msgs, userMsg])
  }

  const retry = async (msgId: string) => {
    if (sending) return
    const idx = msgs.findIndex((m) => m.id === msgId && m.kind === "user")
    if (idx === -1) return
    const before = msgs.slice(0, idx).filter((m): m is UserMsg | AssistantMsg => m.kind === "user" || m.kind === "assistant")
    await stream([...before, msgs[idx] as UserMsg])
  }

  const edit = (msgId: string, idx: number) => {
    if (sending) return
    const m = msgs.find((m) => m.id === msgId)
    if (!m || m.kind !== "user") return
    setEditIdx(idx)
    setDraft(m.content)
  }

  // Count only user messages for thinkingKey reset
  const userCount = msgs.filter((m) => m.kind === "user").length

  return (
    <>
      {!isOpen && (
        <button
          type="button"
          onClick={() => onOpenChange(true)}
          className="fixed right-4 top-16 z-40 flex items-center gap-1 rounded-md border border-[#0a5d49] bg-[#0F6E56] px-3 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#085041]"
          aria-label="Open dashboard chat"
        >
          <MessageSquare size={15} />
          Chat
        </button>
      )}

      {isOpen && (
        <aside className="fixed right-0 top-[49px] z-40 flex h-[calc(100dvh-49px)] w-full max-w-sm flex-col border-l border-[#D3D1C7] bg-[#FAFAF8] shadow-xl">
          {/* Header */}
          <div className="flex items-center justify-between border-b border-[#D3D1C7] px-4 py-3">
            <h2 className="text-sm font-semibold text-[#085041]">Gemma4 Assistant</h2>
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="rounded-md p-1 text-[#085041] hover:bg-[#E1F5EE]"
            >
              <X size={14} />
            </button>
          </div>

          {/* Message list */}
          <div className="flex-1 space-y-2 overflow-y-auto p-4">
            {msgs.length === 0 && (
              <p className="text-xs text-[#5a6b65]">Ask about triage priorities, dispatch, or close actions.</p>
            )}

            {msgs.map((msg, idx) => {
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
                          onClick={() => edit(msg.id, idx)}
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
                <div key={msg.id} className="mr-6 rounded-lg bg-[#E1F5EE] px-3 py-2 text-xs text-[#085041]">
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
                </div>
              )
            })}

            {/* Live thinking — shown while model is reasoning */}
            {sending && (modelThinking || thinkingStatus) && (
              <div className="mr-6 space-y-1.5">
                {thinkingStatus && (
                  <p className="flex items-center gap-1.5 text-[10px] font-mono text-[#3A6F61]">
                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-[#0F6E56]" />
                    {thinkingStatus}
                  </p>
                )}
                {modelThinking && (
                  <ThinkingBubble text={modelThinking} resetKey={thinkingKey} />
                )}
              </div>
            )}
          </div>

          {/* Input */}
          <div className="border-t border-[#D3D1C7] p-3">
            {editIdx !== null && (
              <p className="mb-2 text-[11px] font-medium text-[#0b5f4b]">Editing selected question…</p>
            )}
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
