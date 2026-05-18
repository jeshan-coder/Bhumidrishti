import { promises as fs } from "node:fs"
import path from "node:path"
import { NextRequest } from "next/server"

const TILE_FILE_NAME = "turkey.pmtiles"
const TILE_FILE_PATH = path.resolve(process.cwd(), "..", "data", "tiles_data", TILE_FILE_NAME)

function parseRangeHeader(rangeHeader: string | null, fileSize: number) {
  if (!rangeHeader || !rangeHeader.startsWith("bytes=")) {
    return null
  }

  const rawRange = rangeHeader.replace("bytes=", "")
  const [startText, endText] = rawRange.split("-")

  const start = Number.parseInt(startText, 10)
  const end = endText ? Number.parseInt(endText, 10) : fileSize - 1

  if (Number.isNaN(start) || Number.isNaN(end) || start < 0 || end < start || end >= fileSize) {
    return null
  }

  return { start, end }
}

async function readByteRange(start: number, end: number) {
  const fileHandle = await fs.open(TILE_FILE_PATH, "r")
  try {
    const byteLength = end - start + 1
    const buffer = Buffer.alloc(byteLength)
    await fileHandle.read(buffer, 0, byteLength, start)
    return buffer
  } finally {
    await fileHandle.close()
  }
}

export async function GET(request: NextRequest, context: { params: { tileFile: string } }) {
  const requestedFile = context.params.tileFile
  if (requestedFile !== TILE_FILE_NAME) {
    return new Response("Not found", { status: 404 })
  }

  let stats
  try {
    stats = await fs.stat(TILE_FILE_PATH)
  } catch {
    return new Response("PMTiles file not found", { status: 404 })
  }

  const range = parseRangeHeader(request.headers.get("range"), stats.size)

  if (!range) {
    const fullBuffer = await fs.readFile(TILE_FILE_PATH)
    return new Response(fullBuffer, {
      status: 200,
      headers: {
        "Content-Type": "application/octet-stream",
        "Content-Length": String(stats.size),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
      },
    })
  }

  const partialBuffer = await readByteRange(range.start, range.end)
  return new Response(partialBuffer, {
    status: 206,
    headers: {
      "Content-Type": "application/octet-stream",
      "Content-Length": String(range.end - range.start + 1),
      "Content-Range": `bytes ${range.start}-${range.end}/${stats.size}`,
      "Accept-Ranges": "bytes",
      "Cache-Control": "public, max-age=86400",
    },
  })
}
