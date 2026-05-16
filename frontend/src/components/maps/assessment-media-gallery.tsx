"use client"

// This variable holds the backend base URL used to build absolute media links.
const BACKEND_PUBLIC_URL = (process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000").replace(/\/$/, "")

// This function converts a backend-relative media path to a full URL.
function toMediaUrl(rawPath: unknown): string | null {
  if (typeof rawPath !== "string") {
    return null
  }
  const trimmed = rawPath.trim()
  if (!trimmed) {
    return null
  }
  if (trimmed.startsWith("http://") || trimmed.startsWith("https://")) {
    return trimmed
  }
  return `${BACKEND_PUBLIC_URL}/media/${trimmed.replace(/^\/+/, "")}`
}

// This function detects whether a media URL looks like a playable video.
function looksLikeVideo(url: string): boolean {
  const lower = url.toLowerCase()
  return /\.(mp4|webm|mov|m4v|ogg)(\?|$)/.test(lower)
}

// This function parses drone_frames JSONB which may arrive as array or JSON string.
function parseDroneFrames(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
  }
  if (typeof raw === "string" && raw.trim().length > 0) {
    try {
      const parsed: unknown = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      }
    } catch {
      return []
    }
  }
  return []
}

// This type defines props for the inline media gallery in the info sidebar.
type AssessmentMediaGalleryProps = {
  properties: Record<string, unknown>
}

// This component renders all available media for an assessment (orthophoto, photo, video, frames).
export function AssessmentMediaGallery({ properties }: AssessmentMediaGalleryProps) {
  const preChipUrl = toMediaUrl(properties.pre_chip_path)
  const postChipUrl = toMediaUrl(properties.chip_path)
  const droneFrameUrls = parseDroneFrames(properties.drone_frames)
    .map(toMediaUrl)
    .filter((url): url is string => Boolean(url))

  // When input_type is "video", the video file path is stored in photo_path (video_path
  // is always NULL in the DB). Route accordingly so the player gets the real URL.
  const isVideoInput = String(properties.input_type ?? "") === "video"
  const photoUrl     = isVideoInput ? null : toMediaUrl(properties.photo_path)
  const videoUrl     = isVideoInput
    ? toMediaUrl(properties.photo_path)
    : toMediaUrl(properties.video_path)

  // Avoid showing the same image twice when the ground photo equals chip_path.
  const photoDistinctFromPost = photoUrl && photoUrl !== postChipUrl ? photoUrl : null

  const hasOrtho = Boolean(preChipUrl || postChipUrl)
  const hasPhoto = Boolean(photoDistinctFromPost)
  const hasVideo = Boolean(videoUrl)
  const hasFrames = droneFrameUrls.length > 0

  if (!hasOrtho && !hasPhoto && !hasVideo && !hasFrames) {
    return (
      <div className="rounded-md border border-dashed border-[#D3D1C7] bg-[#FAFAF8] px-3 py-4 text-center text-[11px] text-[#6b7280]">
        No media available for this assessment.
      </div>
    )
  }

  return (
    <div className="space-y-3">
      {hasOrtho && (
        <MediaSection title="Orthophoto chips">
          <div className="grid grid-cols-2 gap-2">
            <ImageTile url={preChipUrl} label="Pre-earthquake" emptyLabel="No pre image" />
            <ImageTile url={postChipUrl} label="Post-earthquake" emptyLabel="No post image" />
          </div>
        </MediaSection>
      )}

      {hasPhoto && (
        <MediaSection title="Ground photo">
          <ImageTile url={photoDistinctFromPost} label="Field photo" />
        </MediaSection>
      )}

      {hasVideo && (
        <MediaSection title="Video">
          {videoUrl && looksLikeVideo(videoUrl) ? (
            <video
              controls
              preload="metadata"
              className="w-full rounded-md border border-[#D3D1C7] bg-black"
              src={videoUrl}
            />
          ) : (
            videoUrl && (
              <a
                href={videoUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-[11px] font-semibold text-[#0F6E56] underline"
              >
                Open video ↗
              </a>
            )
          )}
        </MediaSection>
      )}

      {hasFrames && (
        <MediaSection title={`Drone frames (${droneFrameUrls.length})`}>
          <div className="grid grid-cols-3 gap-2">
            {droneFrameUrls.map((url, idx) => (
              <ImageTile
                key={`${url}-${idx}`}
                url={url}
                label={`Frame ${idx + 1}`}
                compact
              />
            ))}
          </div>
        </MediaSection>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Small subcomponents
// ---------------------------------------------------------------------------

type MediaSectionProps = {
  title: string
  children: React.ReactNode
}

function MediaSection({ title, children }: MediaSectionProps) {
  return (
    <div>
      <div className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-[#6b7280]">
        {title}
      </div>
      {children}
    </div>
  )
}

type ImageTileProps = {
  url: string | null
  label: string
  emptyLabel?: string
  compact?: boolean
}

function ImageTile({ url, label, emptyLabel, compact }: ImageTileProps) {
  if (!url) {
    return (
      <div className="flex aspect-video items-center justify-center rounded-md border border-dashed border-[#D3D1C7] bg-[#FAFAF8] text-[10px] text-[#9ca3af]">
        {emptyLabel ?? "No image"}
      </div>
    )
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      title={`Open ${label} in new tab`}
      className="group block overflow-hidden rounded-md border border-[#D3D1C7] bg-white"
    >
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={url}
        alt={label}
        loading="lazy"
        className={`${compact ? "aspect-square" : "aspect-video"} w-full object-cover transition-transform group-hover:scale-105`}
      />
      <div className="px-2 py-1 text-[10px] font-medium text-[#17352b]">{label}</div>
    </a>
  )
}
