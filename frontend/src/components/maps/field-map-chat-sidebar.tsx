"use client"

import { useEffect, useState } from "react"
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
  const setIsSidebarOpen = onOpenChange

  // This variable stores chat history for current map session.
  const [messages, setMessages] = useState<UiChatMessage[]>([])

  // This variable stores the pending user prompt.
  const [draftMessage, setDraftMessage] = useState("")

  // This variable tracks active streaming request state.
  const [isSending, setIsSending] = useState(false)

  // This variable tracks the pre-token phase where model is still thinking.
  const [isWaitingForFirstToken, setIsWaitingForFirstToken] = useState(false)

  // This function animates thinking text while the model has not started token output.
  useEffect(() => {
    if (!isWaitingForFirstToken) {
      return
    }

    const frames = ["Gemma4 is thinking.", "Gemma4 is thinking..", "Gemma4 is thinking..."]
    let frameIndex = 0

    const intervalId = window.setInterval(() => {
      frameIndex = (frameIndex + 1) % frames.length
      setMessages((currentMessages) => {
        const updated = [...currentMessages]
        const lastIndex = updated.length - 1
        if (lastIndex >= 0 && updated[lastIndex].role === "assistant") {
          updated[lastIndex] = {
            ...updated[lastIndex],
            content: frames[frameIndex],
          }
        }
        return updated
      })
    }, 260)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [isWaitingForFirstToken])

  // This function streams a message to backend and updates UI incrementally.
  const handleSendMessage = async () => {
    const trimmed = draftMessage.trim()
    if (!trimmed || isSending) {
      return
    }

    const nextUserMessage: UiChatMessage = { role: "user", content: trimmed }
    const nextMessages = [...messages, nextUserMessage]
    setMessages([...nextMessages, { role: "assistant", content: "Gemma4 is thinking..." }])
    setDraftMessage("")
    setIsSending(true)
    setIsWaitingForFirstToken(true)

    try {
      const chatPayload = [
        {
          role: "system" as const,
          content:
            "Share brief reasoning inside <think></think> first, then provide final answer outside those tags.",
        },
        ...nextMessages.map((message) => ({
          role: message.role,
          content: message.content,
        })),
      ]

      let assistantReplyRaw = ""

      await streamChatRequest(chatPayload, {
        onThinking: (text) => {
          setIsWaitingForFirstToken(true)
          setMessages((currentMessages) => {
            const updated = [...currentMessages]
            const lastIndex = updated.length - 1
            if (lastIndex >= 0 && updated[lastIndex].role === "assistant") {
              updated[lastIndex] = {
                ...updated[lastIndex],
                content: text,
              }
            }
            return updated
          })
        },
        onToken: (token) => {
          assistantReplyRaw += token
          const parsed = parseModelStream(assistantReplyRaw)

          setIsWaitingForFirstToken(false)

          setMessages((currentMessages) => {
            const updated = [...currentMessages]
            const lastIndex = updated.length - 1
            if (lastIndex >= 0 && updated[lastIndex].role === "assistant") {
              if (parsed.hasExplicitThinking && !parsed.thinkingCompleted) {
                updated[lastIndex] = {
                  ...updated[lastIndex],
                  content: parsed.thinking.trim() || "Gemma4 is thinking...",
                }
              } else {
                const assistantAnswer = parsed.answer.trimStart()
                updated[lastIndex] = { ...updated[lastIndex], content: assistantAnswer }
              }
            }
            return updated
          })
        },
        onDone: () => {
          setIsWaitingForFirstToken(false)
          const parsed = parseModelStream(assistantReplyRaw)
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
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to contact AI endpoint"
      setIsWaitingForFirstToken(false)
      setMessages((currentMessages) => [
        ...currentMessages.filter(
          (chatMessage, index, array) => !(index === array.length - 1 && chatMessage.role === "assistant")
        ),
        { role: "assistant", content: `Error: ${message}` },
      ])
    } finally {
      setIsWaitingForFirstToken(false)
      setIsSending(false)
    }
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

            {messages.map((message, index) => (
              <div
                key={`${message.role}-${index}`}
                className={
                  message.role === "user"
                    ? "ml-6 rounded-lg bg-[#0F6E56] px-3 py-2 text-xs text-white"
                    : "mr-6 rounded-lg bg-[#E1F5EE] px-3 py-2 text-xs text-[#085041]"
                }
              >
                {message.content}
              </div>
            ))}
          </div>

          <div className="border-t border-[#D3D1C7] p-3">
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
            <button
              type="button"
              onClick={handleSendMessage}
              disabled={isSending}
              className="mt-2 w-full rounded-md bg-[#0F6E56] px-3 py-2 text-xs font-semibold text-white transition-colors hover:bg-[#085041] disabled:cursor-not-allowed disabled:bg-[#8cb8ad]"
            >
              {isSending ? "Sending..." : "Send"}
            </button>
          </div>
        </aside>
      ) : null}
    </>
  )
}
