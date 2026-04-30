"use client"

import { useRef, useState } from "react"
import type { ComponentPropsWithoutRef } from "react"
import { Pencil, RotateCcw } from "lucide-react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import { streamChatRequest } from "@/lib/api/chat"

// This type defines one chat message rendered in the sidebar.
type UiChatMessage = {
  role: "user" | "assistant"
  content: string
}

// This type defines parsed model output split into thinking and final answer parts.
type ParsedModelStream = {
  thinking: string
  answer: string
  hasExplicitThinking: boolean
  thinkingCompleted: boolean
}

// This function parses optional <think>...</think> segments from streamed model text.
function parseModelStream(rawText: string): ParsedModelStream {
  const startTag = "<think>"
  const endTag = "</think>"

  const thinkStartIndex = rawText.indexOf(startTag)
  if (thinkStartIndex === -1) {
    return {
      thinking: "",
      answer: rawText,
      hasExplicitThinking: false,
      thinkingCompleted: true,
    }
  }

  const contentAfterStart = rawText.slice(thinkStartIndex + startTag.length)
  const thinkEndIndex = contentAfterStart.indexOf(endTag)

  if (thinkEndIndex === -1) {
    return {
      thinking: contentAfterStart,
      answer: "",
      hasExplicitThinking: true,
      thinkingCompleted: false,
    }
  }

  return {
    thinking: contentAfterStart.slice(0, thinkEndIndex),
    answer: contentAfterStart.slice(thinkEndIndex + endTag.length),
    hasExplicitThinking: true,
    thinkingCompleted: true,
  }
}

// This type defines props for controlled sidebar state.
type FieldMapChatSidebarProps = {
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

// This component renders the field map chat toggle button and chat sidebar.
export function FieldMapChatSidebar({ isOpen, onOpenChange }: FieldMapChatSidebarProps) {
  // This variable controls sidebar visibility (controlled by parent).
  const isSidebarOpen = isOpen

  // This variable stores chat history for current map session.
  const [messages, setMessages] = useState<UiChatMessage[]>([])

  // This variable stores the pending user prompt.
  const [draftMessage, setDraftMessage] = useState("")

  // This variable tracks active streaming request state.
  const [isSending, setIsSending] = useState(false)

  // This variable stores live model thinking text emitted via SSE before/while response forms.
  const [thinkingText, setThinkingText] = useState("")

  // This variable stores an active abort controller for mid-stream cancellation.
  const activeStreamAbortControllerRef = useRef<AbortController | null>(null)

  // This variable tracks which user message is being edited before resend.
  const [editingUserMessageIndex, setEditingUserMessageIndex] = useState<number | null>(null)

  // This function identifies browser abort errors from cancelled streaming requests.
  const isAbortError = (error: unknown): boolean => {
    if (error instanceof DOMException) {
      return error.name === "AbortError"
    }

    return error instanceof Error && error.name === "AbortError"
  }

  // This function aborts the active stream and restores input controls.
  const handleStopStreaming = () => {
    activeStreamAbortControllerRef.current?.abort()
    setThinkingText("")
    setIsSending(false)
  }

  // This function starts one streaming response from a prepared chat history.
  const streamAssistantResponse = async (nextMessages: UiChatMessage[]) => {
    const streamAbortController = new AbortController()
    activeStreamAbortControllerRef.current = streamAbortController

    setMessages([...nextMessages, { role: "assistant", content: "" }])
    setDraftMessage("")
    setIsSending(true)
    setThinkingText("Gemma4 is thinking...")

    try {
      const chatPayload = nextMessages.map((message) => ({
        role: message.role,
        content: message.content,
      }))

      let assistantReplyRaw = ""

      await streamChatRequest(
        chatPayload,
        {
          onThinking: (text) => {
            setThinkingText(text)
          },
          onToolCall: (toolName, args) => {
            const argsPreview = Object.keys(args).length > 0 ? ` ${JSON.stringify(args)}` : ""
            setThinkingText(`Calling tool: ${toolName}${argsPreview}`)
          },
          onToken: (token) => {
            assistantReplyRaw += token
            const parsed = parseModelStream(assistantReplyRaw)

            setMessages((currentMessages) => {
              const updated = [...currentMessages]
              const lastIndex = updated.length - 1
              if (lastIndex >= 0 && updated[lastIndex].role === "assistant") {
                if (parsed.hasExplicitThinking && !parsed.thinkingCompleted) {
                  setThinkingText(parsed.thinking.trim() || "Gemma4 is thinking...")
                } else {
                  const assistantAnswer = parsed.answer.trimStart()
                  setThinkingText("")
                  updated[lastIndex] = { ...updated[lastIndex], content: assistantAnswer }
                }
              }
              return updated
            })
          },
          onDone: () => {
            const parsed = parseModelStream(assistantReplyRaw)
            setThinkingText("")
            if (!parsed.answer.trim()) {
              setMessages((currentMessages) => {
                const updated = [...currentMessages]
                const lastIndex = updated.length - 1
                if (lastIndex >= 0 && updated[lastIndex].role === "assistant") {
                  updated[lastIndex] = {
                    ...updated[lastIndex],
                    content: "No response from model.",
                  }
                }
                return updated
              })
            }
          },
        },
        {
          signal: streamAbortController.signal,
        }
      )
    } catch (error) {
      if (isAbortError(error)) {
        setMessages((currentMessages) => {
          const updated = [...currentMessages]
          const lastIndex = updated.length - 1
          if (lastIndex >= 0 && updated[lastIndex].role === "assistant" && !updated[lastIndex].content.trim()) {
            updated[lastIndex] = {
              ...updated[lastIndex],
              content: "Response stopped.",
            }
          }
          return updated
        })
        return
      }

      const message = error instanceof Error ? error.message : "Failed to contact AI endpoint"
      setMessages((currentMessages) => [
        ...currentMessages.filter(
          (chatMessage, index, array) => !(index === array.length - 1 && chatMessage.role === "assistant")
        ),
        { role: "assistant", content: `Error: ${message}` },
      ])
    } finally {
      if (activeStreamAbortControllerRef.current === streamAbortController) {
        activeStreamAbortControllerRef.current = null
      }
      setThinkingText("")
      setIsSending(false)
    }
  }

  // This function streams a message to backend and updates UI incrementally.
  const handleSendMessage = async () => {
    const trimmed = draftMessage.trim()
    if (!trimmed || isSending) {
      return
    }

    const nextUserMessage: UiChatMessage = { role: "user", content: trimmed }

    if (editingUserMessageIndex !== null) {
      const conversationBeforeEdit = messages.slice(0, editingUserMessageIndex)
      setEditingUserMessageIndex(null)
      await streamAssistantResponse([...conversationBeforeEdit, nextUserMessage])
      return
    }

    await streamAssistantResponse([...messages, nextUserMessage])
  }

  // This function retries a selected user message and regenerates from that point.
  const handleRetryMessage = async (userMessageIndex: number) => {
    if (isSending) {
      return
    }

    const selectedMessage = messages[userMessageIndex]
    if (!selectedMessage || selectedMessage.role !== "user") {
      return
    }

    const conversationBeforeRetry = messages.slice(0, userMessageIndex)
    await streamAssistantResponse([...conversationBeforeRetry, selectedMessage])
  }

  // This function pre-fills the input to edit a selected user question.
  const handleEditMessage = (userMessageIndex: number) => {
    if (isSending) {
      return
    }

    const selectedMessage = messages[userMessageIndex]
    if (!selectedMessage || selectedMessage.role !== "user") {
      return
    }

    setEditingUserMessageIndex(userMessageIndex)
    setDraftMessage(selectedMessage.content)
  }

  return (
    <>
      <button
        type="button"
        onClick={() => onOpenChange(true)}
        className="absolute left-4 top-4 z-20 rounded-md border border-[#0a5d49] bg-[#0F6E56] px-3 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#085041]"
        aria-label="Open chat sidebar"
      >
        ☰
      </button>

      {isSidebarOpen ? (
        <aside className="absolute left-0 top-0 z-30 flex h-full w-full max-w-sm flex-col border-r border-[#D3D1C7] bg-[#FAFAF8] shadow-xl">
          <div className="flex items-center justify-between border-b border-[#D3D1C7] px-4 py-3">
            <h2 className="text-sm font-semibold text-[#085041]">Gemma4 Assistant</h2>
            <button
              type="button"
              onClick={() => onOpenChange(false)}
              className="rounded-md px-2 py-1 text-xs font-medium text-[#085041] hover:bg-[#E1F5EE]"
            >
              Close
            </button>
          </div>

          <div className="flex-1 space-y-2 overflow-y-auto p-4">
            {messages.length === 0 ? (
              <p className="text-xs text-[#5a6b65]">Start by asking Gemma4 about this field area.</p>
            ) : null}

            {isSending && thinkingText ? (
              <div className="mr-6 rounded-lg border border-[#CFE8DF] bg-[#F4FAF7] px-3 py-2 text-xs text-[#0b5f4b]">
                <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[#3A6F61]">Thinking</p>
                {thinkingText}
              </div>
            ) : null}

            {messages.map((message, index) => (
              <div
                key={`${message.role}-${index}`}
                hidden={message.role === "assistant" && !message.content.trim()}
                className={
                  message.role === "user"
                    ? "group ml-6 rounded-lg bg-[#0F6E56] px-3 py-2 text-xs text-white"
                    : "mr-6 rounded-lg bg-[#E1F5EE] px-3 py-2 text-xs text-[#085041]"
                }
              >
                {message.role === "assistant" ? (
                  <ReactMarkdown
                    remarkPlugins={[remarkGfm]}
                    components={{
                      p: ({ children }: ComponentPropsWithoutRef<"p">) => <p className="mb-2 last:mb-0">{children}</p>,
                      ul: ({ children }: ComponentPropsWithoutRef<"ul">) => <ul className="mb-2 list-disc space-y-1 pl-4 last:mb-0">{children}</ul>,
                      ol: ({ children }: ComponentPropsWithoutRef<"ol">) => <ol className="mb-2 list-decimal space-y-1 pl-4 last:mb-0">{children}</ol>,
                      code: ({ children }: ComponentPropsWithoutRef<"code">) => <code className="rounded bg-[#d8efe8] px-1 py-0.5 font-mono text-[11px]">{children}</code>,
                      pre: ({ children }: ComponentPropsWithoutRef<"pre">) => <pre className="mb-2 overflow-x-auto rounded bg-[#d8efe8] p-2 text-[11px] last:mb-0">{children}</pre>,
                    }}
                  >
                    {message.content}
                  </ReactMarkdown>
                ) : (
                  <>
                    <p>{message.content}</p>
                    {!isSending ? (
                      <div className="mt-2 flex justify-end gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100">
                        <button
                          type="button"
                          onClick={() => void handleRetryMessage(index)}
                          className="inline-flex h-6 w-6 items-center justify-center rounded border border-white/35 text-white transition-colors hover:bg-white/10"
                          aria-label="Retry question"
                          title="Retry"
                        >
                          <RotateCcw size={12} />
                        </button>
                        <button
                          type="button"
                          onClick={() => handleEditMessage(index)}
                          className="inline-flex h-6 w-6 items-center justify-center rounded border border-white/35 text-white transition-colors hover:bg-white/10"
                          aria-label="Edit question"
                          title="Edit"
                        >
                          <Pencil size={12} />
                        </button>
                      </div>
                    ) : null}
                  </>
                )}
              </div>
            ))}
          </div>

          <div className="border-t border-[#D3D1C7] p-3">
            {editingUserMessageIndex !== null ? (
              <p className="mb-2 text-[11px] font-medium text-[#0b5f4b]">Editing selected question…</p>
            ) : null}
            <textarea
              value={draftMessage}
              onChange={(event) => setDraftMessage(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault()
                  void handleSendMessage()
                }
              }}
              placeholder="Ask Gemma4..."
              rows={3}
              className="w-full resize-none rounded-md border border-[#D3D1C7] bg-white px-3 py-2 text-xs text-[#1f2d28] outline-none focus:border-[#0F6E56]"
            />
            <div className="mt-2 grid grid-cols-2 gap-2">
              <button
                type="button"
                onClick={handleSendMessage}
                disabled={isSending}
                className="rounded-md bg-[#0F6E56] px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-[#085041] disabled:cursor-not-allowed disabled:bg-[#8cb8ad]"
              >
                {isSending ? "Sending..." : editingUserMessageIndex !== null ? "Save & Send" : "Send"}
              </button>
              <button
                type="button"
                onClick={handleStopStreaming}
                disabled={!isSending}
                className="rounded-md border border-[#0F6E56] bg-white px-3 py-2 text-xs font-semibold text-[#0F6E56] transition-colors hover:bg-[#E1F5EE] disabled:cursor-not-allowed disabled:border-[#8cb8ad] disabled:text-[#8cb8ad]"
              >
                Stop
              </button>
            </div>
          </div>
        </aside>
      ) : null}
    </>
  )
}
