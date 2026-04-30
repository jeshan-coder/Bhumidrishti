import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import "maplibre-gl/dist/maplibre-gl.css";
import { cn } from "@/lib/utils";
import { AppNavbar } from "@/components/app-navbar";
import { Toaster } from "sonner";

const inter = Inter({ subsets: ["latin"], variable: "--font-sans" });

export const metadata: Metadata = {
  title: "BhumiDrishti - Offline Disaster Assessment",
  description: "Offline-first disaster damage assessment platform",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" className={cn("font-sans antialiased", inter.variable)}>
      <body>
        <AppNavbar />
        {children}
        <Toaster richColors position="top-right" />
      </body>
    </html>
  );
}
