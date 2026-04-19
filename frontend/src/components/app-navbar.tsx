import Link from "next/link"
import Image from "next/image"
import fullLogo from "@/app/logos/bhumidrishti_logo_full.svg"

// This variable defines the primary model required by the app.
const TARGET_MODEL = "gemma4:26b"

// This variable defines backend base URL used by server-side navbar checks.
const BACKEND_API_URL =
  process.env.BACKEND_INTERNAL_URL ??
  process.env.NEXT_PUBLIC_API_URL ??
  "http://localhost:8000"

const navItems: Array<{ href: string; label: string }> = [
  { href: "/map", label: "Field map" },
  { href: "/dashboard", label: "Dashboard" },
  { href: "/reports", label: "Reports" },
]

type ModelHealthResponse = {
  success: boolean
  data?: {
    model?: string
    model_available?: boolean
  }
  error?: string | null
}

// This function checks backend health endpoint for Gemma model availability.
async function getNavbarModelStatus(): Promise<{ label: string; className: string }> {
  try {
    const response = await fetch(`${BACKEND_API_URL}/health/model`, {
      cache: "no-store",
    })

    if (!response.ok) {
      return {
        label: "Gemma 4 - backend down",
        className: "border-[#A32D2D] bg-[#A32D2D] text-white",
      }
    }

    const payload = (await response.json()) as ModelHealthResponse
    const isAvailable = payload.success && payload.data?.model === TARGET_MODEL && payload.data?.model_available

    if (isAvailable) {
      return {
        label: "Gemma 4 - online",
        className: "border-[#0b5f4b] bg-[#0c614d] text-[#E1F5EE]",
      }
    }

    return {
      label: "Gemma 4 - model missing",
      className: "border-[#EF9F27] bg-[#EF9F27] text-white",
    }
  } catch {
    return {
      label: "Gemma 4 - backend down",
      className: "border-[#A32D2D] bg-[#A32D2D] text-white",
    }
  }
}

// This component renders the global navbar with runtime model health state.
export async function AppNavbar() {
  const modelStatus = await getNavbarModelStatus()

  return (
    <header className="sticky top-0 z-50 border-b border-[#0a5d49] bg-[#0F6E56] text-white">
      <nav className="mx-auto flex h-12 w-full max-w-screen-2xl items-center justify-between gap-4 px-4 sm:px-6">
        <Link href="/assessment" className="inline-flex items-center">
          <Image src={fullLogo} alt="BhumiDrishti" className="h-9 w-auto" priority />
        </Link>

        <div className="hidden items-center gap-1 md:flex">
          {navItems.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className="rounded-md px-3 py-1.5 text-xs font-medium text-[#E1F5EE] transition-colors hover:bg-[#0c614d] hover:text-white"
            >
              {item.label}
            </Link>
          ))}
        </div>

        <div className="flex items-center gap-3">
          <Link
            href="/assessment"
            className="rounded-md border border-[#E1F5EE] bg-[#E1F5EE] px-3 py-1.5 text-xs font-semibold text-[#0F6E56] transition-colors hover:bg-white"
          >
            New Assessment
          </Link>
          <div
            className={`rounded-full border px-2.5 py-1 text-[10px] font-medium sm:text-xs ${modelStatus.className}`}
          >
            {modelStatus.label}
          </div>
        </div>
      </nav>
    </header>
  )
}
