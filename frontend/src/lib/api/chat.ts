// This file defines frontend helpers for backend AI chat requests.

export type ChatRole = "system" | "user" | "assistant"

// This type defines one chat message payload sent to backend.
export type ChatMessagePayload = {
  role: ChatRole
  content: string
}

// This type defines successful backend chat response data.
export type ChatResponseData = {
  model: string
  response: string
}

// This type defines the backend API envelope format.
type BackendEnvelope<T> = {
  success: boolean
  data: T | null
  error: string | null
}

// This type defines one streamed SSE event from backend chat endpoint.
type StreamEvent = {
  event: string
  data: unknown
}

// This type defines callback hooks used by streaming chat requests.
type StreamCallbacks = {
  onThinking: (text: string, mode?: "append" | "replace") => void
  onToken: (token: string) => void
  onToolCall?: (toolName: string, args: Record<string, unknown>) => void
  onToolResult?: (toolName: string, result: Record<string, unknown>) => void
  onDone: () => void
}

// This type defines optional controls for the streaming request lifecycle.
type StreamRequestOptions = {
  signal?: AbortSignal
}

// This variable defines API base URL for browser-side requests.
const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"

// This function sends chat context to backend and returns the AI response text.
export async function sendChatRequest(messages: ChatMessagePayload[]): Promise<ChatResponseData> {
  const response = await fetch(`${API_BASE_URL}/chat`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      messages,
      temperature: 0.2,
    }),
  })

  if (!response.ok) {
    throw new Error(`Chat API failed with status ${response.status}`)
  }

  const payload = (await response.json()) as BackendEnvelope<ChatResponseData>

  if (!payload.success || !payload.data) {
    throw new Error(payload.error ?? "Chat API returned an invalid response")
  }

  return payload.data
}

// This function parses one raw SSE event block into structured event + payload.
function parseStreamEvent(rawEvent: string): StreamEvent | null {
  const lines = rawEvent.split("\n")
  let eventName = "message"
  const dataLines: string[] = []

  for (const line of lines) {
    if (line.startsWith("event:")) {
      eventName = line.slice(6).trim()
    }

    if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trim())
    }
  }

  if (dataLines.length === 0) {
    return null
  }

  const rawData = dataLines.join("\n")
  try {
    return { event: eventName, data: JSON.parse(rawData) }
  } catch {
    return { event: eventName, data: rawData }
  }
}

// This function streams chat response from backend SSE endpoint.
export async function streamChatRequest(
  messages: ChatMessagePayload[],
  callbacks: StreamCallbacks,
  options?: StreamRequestOptions
): Promise<void> {
  callbacks.onThinking("Gemma4 is thinking...")

  const response = await fetch(`${API_BASE_URL}/chat/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      messages,
      temperature: 0.2,
    }),
    signal: options?.signal,
  })

  if (!response.ok) {
    throw new Error(`Chat stream API failed with status ${response.status}`)
  }

  if (!response.body) {
    throw new Error("Chat stream API returned an empty body")
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  let receivedDoneEvent = false
  let thinkingBuffer = ""

  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      break
    }

    buffer += decoder.decode(value, { stream: true })
    buffer = buffer.replace(/\r\n/g, "\n")
    const eventBlocks = buffer.split(/\n\n/)
    buffer = eventBlocks.pop() ?? ""

    for (const block of eventBlocks) {
      const parsed = parseStreamEvent(block)
      if (!parsed) {
        continue
      }

      if (parsed.event === "thinking" && typeof parsed.data === "object" && parsed.data !== null) {
        const text = (parsed.data as { text?: unknown }).text
        const mode = (parsed.data as { mode?: unknown }).mode
        if (typeof text === "string" && text.length > 0) {
          if (mode === "append") {
            thinkingBuffer += text
            callbacks.onThinking(thinkingBuffer, "append")
          } else {
            thinkingBuffer = text
            callbacks.onThinking(thinkingBuffer, "replace")
          }
        } else {
          callbacks.onThinking("Gemma4 is thinking...")
        }
        continue
      }

      if (parsed.event === "tool_call" && typeof parsed.data === "object" && parsed.data !== null) {
        const name = (parsed.data as { name?: unknown }).name
        const rawArgs = (parsed.data as { arguments?: unknown }).arguments
        const args =
          typeof rawArgs === "object" && rawArgs !== null
            ? (rawArgs as Record<string, unknown>)
            : {}

        if (typeof name === "string" && callbacks.onToolCall) {
          callbacks.onToolCall(name, args)
        }
        continue
      }

      if (parsed.event === "tool_result" && typeof parsed.data === "object" && parsed.data !== null) {
        const name = (parsed.data as { name?: unknown }).name
        const rawResult = (parsed.data as { result?: unknown }).result
        const result =
          typeof rawResult === "object" && rawResult !== null
            ? (rawResult as Record<string, unknown>)
            : {}

        if (typeof name === "string" && callbacks.onToolResult) {
          callbacks.onToolResult(name, result)
        }
        continue
      }

      if (parsed.event === "token" && typeof parsed.data === "object" && parsed.data !== null) {
        const token = (parsed.data as { token?: unknown }).token
        if (typeof token === "string") {
          callbacks.onToken(token)
        }
        continue
      }

      if (parsed.event === "error" && typeof parsed.data === "object" && parsed.data !== null) {
        const message = (parsed.data as { message?: unknown }).message
        throw new Error(typeof message === "string" ? message : "Stream error")
      }

      if (parsed.event === "done") {
        receivedDoneEvent = true
        callbacks.onDone()
      }
    }
  }

  const trailingBlock = buffer.trim()
  if (trailingBlock) {
    const parsed = parseStreamEvent(trailingBlock)
    if (parsed?.event === "done") {
      receivedDoneEvent = true
      callbacks.onDone()
    }
  }

  if (!receivedDoneEvent) {
    callbacks.onDone()
  }
}
