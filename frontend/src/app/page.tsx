import { redirect } from "next/navigation"

// This page sets field map as the default entry route for the app.
export default function Home() {
  redirect("/map")
}
