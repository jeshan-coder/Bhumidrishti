import Link from "next/link"
import Image from "next/image"
import fullLogo from "@/app/logos/bhumidrishti_logo_full.svg"

const navItems: Array<{ href: string; label: string }> = [
  { href: "/assessment", label: "New assessment" },
  { href: "/map", label: "Field map" },
  { href: "/dashboard", label: "Dashboard" },
  { href: "/reports", label: "Reports" },
]

export function AppNavbar() {
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

        <div className="rounded-full border border-[#0b5f4b] bg-[#0c614d] px-2.5 py-1 text-[10px] font-medium text-[#E1F5EE] sm:text-xs">
          Gemma 4 - offline
        </div>
      </nav>
    </header>
  )
}
