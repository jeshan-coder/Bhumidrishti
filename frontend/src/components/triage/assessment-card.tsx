"use client"

import { AlertTriangle, MapPin, Activity } from "lucide-react"

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

export function AssessmentCard({ assessment }: { assessment: Assessment }) {
  // Determine severity color
  const getSeverityStyles = (severity: number) => {
    switch (severity) {
      case 5: return "bg-red-500 text-white"
      case 4: return "bg-orange-500 text-white"
      case 3: return "bg-yellow-500 text-white"
      case 2: return "bg-blue-500 text-white"
      default: return "bg-green-500 text-white"
    }
  }

  // Fallback map thumbnail placeholder
  const thumbnailOrPlaceholder = assessment.photo_path
    ? `http://localhost:8000/static/${assessment.photo_path}`
    : "https://via.placeholder.com/300x200?text=No+Image"

  return (
    <div className="group relative flex flex-col overflow-hidden rounded-xl border border-[#D3D1C7] bg-white shadow-sm transition-all hover:shadow-md hover:border-[#0F6E56]">
      <div className="relative h-48 w-full overflow-hidden bg-gray-100">
        {/* We would replace this image with our actual API static host for files. Here we mock parsing if the system doesn't expose it */}
        <img
          src={assessment.photo_path ? `http://localhost:8000/static/${assessment.photo_path}` : "https://via.placeholder.com/300x200?text=No+Photo"}
          alt="Assessment Photo"
          className="h-full w-full object-cover transition-transform duration-500 group-hover:scale-105"
          onError={(e) => {
            (e.target as HTMLImageElement).src = "https://via.placeholder.com/300x200?text=Image+Not+Found"
          }}
        />
        <div className="absolute right-3 top-3 flex shadow-sm opacity-90 rounded-full border border-white/20 overflow-hidden backdrop-blur-md">
          <span className={`px-2.5 py-1 text-xs font-bold ${getSeverityStyles(assessment.severity)} shadow-sm`}>
            Severity {assessment.severity}
          </span>
        </div>
      </div>

      <div className="flex flex-1 flex-col p-4">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-base font-semibold text-[#17352b] group-hover:text-[#0F6E56] transition-colors">
              {assessment.id}
            </h3>
            <span className="inline-flex items-center gap-1 mt-1 text-xs font-medium text-[#6b7280]">
              <MapPin size={12} />
              {assessment.lat.toFixed(4)}, {assessment.lon.toFixed(4)}
            </span>
          </div>
          <span className="rounded bg-gray-100 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-gray-600">
            {assessment.status}
          </span>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3 text-xs border-y border-[#F1EFE8] py-3 my-3">
          <div className="flex flex-col gap-1">
            <span className="text-[#8c8a81]">Damage Type</span>
            <span className="font-medium text-[#17352b] capitalize">{assessment.damage_type.replace(/_/g, " ")}</span>
          </div>
          <div className="flex flex-col gap-1">
            <span className="text-[#8c8a81]">Structural Risk</span>
            <span className="flex items-center gap-1 font-medium text-[#17352b] capitalize">
              <Activity size={12} className={assessment.structural_risk === 'high' ? 'text-red-500' : 'text-blue-500'} />
              {assessment.structural_risk}
            </span>
          </div>
        </div>

        <div className="mt-auto">
          <p className="text-xs font-medium text-[#8c8a81] uppercase tracking-wide">Recommended Action</p>
          <div className="mt-1 flex items-center gap-1.5">
            <AlertTriangle size={14} className={assessment.action_priority >= 4 ? "text-red-500" : "text-yellow-500"} />
            <p className="text-sm font-semibold capitalize text-[#17352b]">{assessment.recommended_action.replace(/_/g, " ")}</p>
          </div>
        </div>
      </div>
    </div>
  )
}
