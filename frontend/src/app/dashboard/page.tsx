"use client"

import { useEffect, useState } from "react"
import { AssessmentCard } from "@/components/triage/assessment-card"
import { Play, Loader2, CheckCircle2, AlertCircle } from "lucide-react"

type Assessment = {
  id: string
  lat: number
  lon: number
  input_type: string
  photo_path: string | null
  severity: number
  damage_type: string
  structural_risk: string
  building_type: string
  recommended_action: string
  action_priority: number
  status: string
  created_at: string
}

type Upload = {
  id: string
  file_type: string
  original_filename: string
  saved_path: string
  lat: number
  lon: number
  status: string
  is_analyzed: boolean
  uploaded_at: string
}

function PendingUploadCard({
  upload,
  onAnalysisStarted
}: {
  upload: Upload
  onAnalysisStarted: (id: string) => void
}) {
  const [isTriggering, setIsTriggering] = useState(false)
  const [currentStatus, setCurrentStatus] = useState(upload.status)
  
  // Poll if processing
  useEffect(() => {
    let interval: NodeJS.Timeout
    if (currentStatus === "processing") {
      interval = setInterval(async () => {
        try {
          const res = await fetch(`http://localhost:8000/uploads/${upload.id}`)
          const json = await res.json()
          if (json.success && json.data.status !== "processing") {
            setCurrentStatus(json.data.status)
            if (json.data.status === "done") {
              onAnalysisStarted(upload.id) // This can trigger a refresh of the assessments list
            }
          }
        } catch (err) {
          console.error(err)
        }
      }, 5000)
    }
    return () => clearInterval(interval)
  }, [currentStatus, upload.id, onAnalysisStarted])

  const handleStart = async () => {
    setIsTriggering(true)
    try {
      const res = await fetch(`http://localhost:8000/uploads/${upload.id}/analyze`, {
        method: "POST"
      })
      const json = await res.json()
      if (json.success) {
        setCurrentStatus("processing")
      } else {
        alert(json.error)
      }
    } catch (err) {
      alert("Failed to start analysis")
    } finally {
      setIsTriggering(false)
    }
  }

  return (
    <div className="flex flex-col gap-3 rounded-xl border border-[#D3D1C7] bg-white p-4 shadow-sm">
      <div className="flex items-start justify-between">
        <div>
          <h3 className="text-sm font-semibold text-[#17352b]">{upload.id}</h3>
          <p className="mt-0.5 text-xs text-[#6b7280]">{upload.original_filename}</p>
        </div>
        <span className="rounded bg-gray-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-gray-600">
          {upload.file_type.replace(/_/g, " ")}
        </span>
      </div>

      <div className="mt-auto pt-2 border-t border-[#F1EFE8]">
        {currentStatus === "uploaded" || currentStatus === "failed" ? (
          <button
            onClick={handleStart}
            disabled={isTriggering}
            className="flex w-full items-center justify-center gap-2 rounded-lg bg-[#0F6E56] px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-[#0c5945] disabled:opacity-50"
          >
            {isTriggering ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
            {currentStatus === "failed" ? "Retry Analysis" : "Start Assessment"}
          </button>
        ) : currentStatus === "processing" ? (
          <div className="flex w-full items-center justify-center gap-2 rounded-lg bg-yellow-50 px-4 py-2 text-sm font-semibold text-yellow-700 border border-yellow-200">
            <Loader2 size={16} className="animate-spin" />
            Analyzing with Gemma-4...
            <div className="ml-2 h-1.5 w-16 overflow-hidden rounded-full bg-yellow-200">
              <div className="h-full w-full animate-pulse bg-yellow-500 rounded-full"></div>
            </div>
          </div>
        ) : currentStatus === "done" ? (
          <div className="flex w-full items-center justify-center gap-2 rounded-lg bg-green-50 px-4 py-2 text-sm font-semibold text-green-700 border border-green-200">
            <CheckCircle2 size={16} />
            Analysis Complete
          </div>
        ) : (
          <div className="flex w-full items-center justify-center gap-2 rounded-lg bg-gray-50 px-4 py-2 text-sm font-semibold text-gray-700 border border-gray-200">
             <AlertCircle size={16} />
             Unknown Status
          </div>
        )}
      </div>
    </div>
  )
}

export default function TriagePage() {
  const [assessments, setAssessments] = useState<Assessment[]>([])
  const [uploads, setUploads] = useState<Upload[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  
  // Refresh trigger when a new assessment completes
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    async function fetchData() {
      setIsLoading(true)
      try {
        const [assRes, upRes] = await Promise.all([
          fetch("http://localhost:8000/assessments?limit=50"),
          fetch("http://localhost:8000/uploads?is_analyzed=false&page_size=20")
        ])
        
        const assJson = await assRes.json()
        const upJson = await upRes.json()
        
        if (assJson.success && upJson.success) {
          setAssessments(assJson.data)
          // Also check status constraint. We filter out done ones from the pending uploads view just in case
          setUploads(upJson.data.uploads.filter((u: Upload) => u.status !== 'done'))
        } else {
          setError(assJson.error || upJson.error)
        }
      } catch (err: unknown) {
        if (err instanceof Error) {
          setError(err.message)
        } else {
          setError("An unknown error occurred")
        }
      } finally {
        setIsLoading(false)
      }
    }

    fetchData()
  }, [refreshKey])

  return (
    <main className="min-h-[calc(100dvh-49px)] w-full bg-[#FAFAF8] p-6 lg:p-10">
      <div className="mx-auto max-w-7xl">
        <header className="mb-10 border-b border-[#D3D1C7] pb-6">
          <h1 className="text-3xl font-bold tracking-tight text-[#17352b]">
            Triage Dashboard
          </h1>
          <p className="mt-2 text-sm text-[#6b7280]">
            Queue incoming physical intelligence and review structural damage assessments flagged by Gemma-4.
          </p>
        </header>

        {isLoading && !assessments.length && !uploads.length && (
          <div className="flex h-64 items-center justify-center">
            <div className="h-8 w-8 animate-spin rounded-full border-4 border-[#0F6E56] border-t-transparent" />
          </div>
        )}

        {error && (
          <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-600 mb-8">
            {error}
          </div>
        )}

        {!isLoading && !error && uploads.length > 0 && (
          <section className="mb-12">
            <div className="mb-4 flex items-center gap-2">
              <h2 className="text-xl font-bold text-[#17352b]">Pending Intelligence</h2>
              <span className="flex h-5 items-center justify-center rounded-full bg-blue-100 px-2 text-xs font-bold text-blue-800">
                {uploads.length} Action{uploads.length !== 1 && 's'} Required
              </span>
            </div>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {uploads.map((u) => (
                <PendingUploadCard 
                  key={u.id} 
                  upload={u} 
                  onAnalysisStarted={() => setRefreshKey(k => k + 1)} 
                />
              ))}
            </div>
          </section>
        )}

        <section>
          <div className="mb-4 flex items-center gap-2">
            <h2 className="text-xl font-bold text-[#17352b]">Completed Assessments</h2>
            <span className="flex h-5 items-center justify-center rounded-full bg-green-100 px-2 text-xs font-bold text-green-800">
              {assessments.length}
            </span>
          </div>

          {!isLoading && !error && assessments.length === 0 && (
             <div className="flex h-48 flex-col items-center justify-center rounded-xl border border-dashed border-[#D3D1C7] bg-white">
               <p className="font-medium text-[#17352b]">No assessments found</p>
               <p className="mt-1 text-sm text-[#6b7280]">
                 Trigger a pending upload above to generate reports.
               </p>
             </div>
          )}

          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {assessments.map((a) => (
              <AssessmentCard key={a.id} assessment={a} />
            ))}
          </div>
        </section>
      </div>
    </main>
  )
}
