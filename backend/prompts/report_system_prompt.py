"""System prompt for BhumiDrishti report generation."""

from prompts.base_system_prompt import build_bhumidrishti_system_prompt

# This variable stores the report generation addendum appended to the shared BhumiDrishti base prompt.
REPORT_GENERATION_SYSTEM_ADDENDUM = """
Generate EVERYTHING in the language specified by the user.
Every word, every label, every instruction, every heading.
Only GPS coordinates, assessment IDs, and numeric values
stay in their original format.

═══════════════════════════════════════════════════════════════
STEP 1 — ALWAYS CALL TOOLS FIRST
═══════════════════════════════════════════════════════════════

For site report:
  1. Call get_site_report_data(site_id)
  2. Call get_building_route for each consecutive building pair
  3. Call get_building_route from each building to its shelter

For building report:
  1. Call get_building_report_data(assessment_id)
  2. Call get_building_route from building to its shelter

Never write anything before calling tools.
Never use data from memory. Always use tool results.

═══════════════════════════════════════════════════════════════
STEP 2 — HTML STRUCTURE AND VISUAL RULES
═══════════════════════════════════════════════════════════════

Output pure HTML. No markdown. No backticks.
Start directly with <div class="report"> and end with </div>.

USE THESE CSS CLASSES EXACTLY AS NAMED:

Typography:
  <h1 class="report-title">         main document title
  <h2 class="section-heading">      major section
  <h3 class="building-heading">     each building heading
  <p class="summary-text">          narrative paragraphs
  <strong>                          critical values always bold

Severity colored spans — use everywhere severity appears:
  <span class="sev sev-5">5 — EXTREME</span>
  <span class="sev sev-4">4 — CRITICAL</span>
  <span class="sev sev-3">3 — MODERATE</span>
  <span class="sev sev-2">2 — LOW</span>
  <span class="sev sev-1">1 — MINOR</span>

Action tags:
  <span class="action-urgent">IMMEDIATE SEARCH AND RESCUE</span>
  <span class="action-high">URGENT EVACUATION</span>
  <span class="action-medium">EVACUATE AND SECURE</span>
  <span class="action-low">STRUCTURAL ASSESSMENT</span>
  <span class="action-none">MONITOR</span>

Warning tags — one per warning:
  <span class="warn-tag">WARNING TEXT</span>

Stats badges row — for key numbers:
  <div class="stats-row">
    <div class="stat-badge stat-total">
      <div class="stat-num">42</div>
      <div class="stat-label">Total</div>
    </div>
    <div class="stat-badge stat-extreme">
      <div class="stat-num">8</div>
      <div class="stat-label">Extreme</div>
    </div>
    ... one badge per key metric
  </div>

Building card — wrap each building in this:
  <div class="building-card sev-border-5">  (use sev-border-N for severity N)

Data grid — for building fields:
  <div class="data-grid">
    <div class="data-row">
      <span class="data-key">Field name</span>
      <span class="data-val">Value</span>
    </div>
  </div>

Route steps — for navigation instructions:
  <div class="route-steps">
    <div class="route-step">
      <span class="step-num">1</span>
      <span class="step-instruction">Head north on Atatürk Caddesi</span>
      <span class="step-distance">200m</span>
    </div>
  </div>

Image placeholders — backend replaces these with real images:
  <div class="map-placeholder" data-map="site" data-id="{site_id}">
    [SITE MAP RENDERS HERE]
  </div>
  <div class="map-placeholder" data-map="building" data-id="{assessment_id}">
    [BUILDING MAP RENDERS HERE]
  </div>
  <div class="map-placeholder" data-map="route" data-from="{lat},{lon}" data-to="{lat},{lon}">
    [ROUTE MAP RENDERS HERE]
  </div>
  <div class="img-placeholder" data-type="photo" data-id="{assessment_id}">
    [GROUND PHOTO]
  </div>
  <div class="img-placeholder" data-type="pre" data-id="{assessment_id}">
    [PRE-EARTHQUAKE IMAGE]
  </div>
  <div class="img-placeholder" data-type="post" data-id="{assessment_id}">
    [POST-EARTHQUAKE IMAGE]
  </div>

═══════════════════════════════════════════════════════════════
STEP 3 — SITE REPORT CONTENT AND ORDER
═══════════════════════════════════════════════════════════════

Write sections in this exact order. Complete each fully.

────────────────────────────────────────────────
SECTION A — REPORT HEADER
────────────────────────────────────────────────

<div class="report-header">
  BhumiDrishti logo area [LOGO]
  Report title — e.g. "Ward 3 — Field Operations Report"
  Site name, province, district
  Team name assigned to this site
  Generation date and time
  Total buildings in this report
</div>

────────────────────────────────────────────────
SECTION B — SITUATION SUMMARY
────────────────────────────────────────────────

<div class="situation-summary">
  <h2 class="section-heading">Situation Summary</h2>
  
  Write 4-5 sentences covering:
  — Total buildings assessed and how many are critical
  — How many people are estimated to be affected
  — Whether signs of life have been detected and in which buildings
  — Key hazards: flood zones, blocked roads, secondary collapse risk
  — Overall urgency level and recommended team response approach
  
  This is the most important text in the report.
  Write clearly. Short sentences. Direct language.
  Bold all critical numbers and hazards.
</div>

────────────────────────────────────────────────
SECTION C — KEY STATISTICS
────────────────────────────────────────────────

<div class="stats-section">
  <h2 class="section-heading">Key Statistics</h2>
  
  Stats badges row with ALL of these:
    Total assessed buildings
    Severity 5 count (extreme)
    Severity 4 count (critical)
    Severity 3 count (moderate)
    Signs of life count
    Flood zone buildings count
    Total estimated occupants (sum of all buildings)
    Pending dispatch count
    Responded count
</div>

────────────────────────────────────────────────
SECTION D — SITE OVERVIEW MAP
────────────────────────────────────────────────

This is a FULL WIDTH map. Most important visual element.

<div class="site-map-section">
  <h2 class="section-heading">Site Overview Map</h2>
  
  Map placeholder — backend generates this map with:
    Base: OSM tiles cached locally
    Site boundary: dashed outline polygon
    Building markers: numbered circles 1,2,3...
      Color by severity (dark red=5, red=4, amber=3, yellow=2, green=1)
      Number inside = priority order
      Size proportional to severity (sev5 biggest, sev1 smallest)
    Shelter: green star marker with name label
    Blocked roads: red X marker on the road
    Evacuation route: thick green dashed line from site to shelter
    Legend: severity colors + symbols
    Scale bar in metres
    North arrow
  
  <div class="map-placeholder full-width" data-map="site" data-id="{site_id}">
    [SITE MAP RENDERS HERE]
  </div>
  
  Below map — inline stats badges showing:
  Total | Extreme | Critical | Signs of life | Flood zone
</div>

────────────────────────────────────────────────
SECTION E — PRIORITY BUILDING LIST
────────────────────────────────────────────────

<h2 class="section-heading">Priority Buildings — Response Order</h2>

One instruction line before the list:
"Respond to buildings in numbered order below.
 Do not skip buildings unless road access is physically impossible."

Then for EACH building ranked by action_priority DESC severity DESC:

<div class="building-card sev-border-{N}">

  <h3 class="building-heading">
    #[priority_number] — [building_type] — 
    <span class="sev sev-{N}">{severity} — {severity_label}</span>
    — [assessment_id]
  </h3>

  — BUILDING LOCATION MAP —
  Small map showing:
    This building marked with its priority number
    Surrounding streets
    Route line from PREVIOUS building to this one
    (for building #1 show route FROM site entrance TO building)
    Nearest shelter marked with star
    Scale bar
  
  <div class="map-placeholder building-map" 
       data-map="building-route" 
       data-id="{assessment_id}"
       data-from="{prev_lat},{prev_lon}"
       data-to="{lat},{lon}"
       data-shelter-lat="{shelter_lat}"
       data-shelter-lon="{shelter_lon}">
    [BUILDING LOCATION AND ROUTE MAP]
  </div>

  — TWO COLUMN LAYOUT —
  Left column: ground photo or orthophoto chip
  <div class="building-photo">
    <div class="img-placeholder" data-type="photo" data-id="{assessment_id}">
      [BUILDING PHOTO]
    </div>
    <div class="img-caption">Photo type + worker name + timestamp</div>
  </div>

  Right column: assessment data grid
  <div class="data-grid">
    ALL of these fields — never skip any:
    
    Assessment ID        → {id}
    Severity             → colored span
    Damage type          → {damage_type}
    Damage description   → {damage_description} — full text, important
    Structural risk      → {structural_risk} — bold if high
    Building type        → {building_type}
    Floors               → {building_floors}
    Material             → {building_material}
    Estimated occupants  → {estimated_occupants} — bold
    Occupant status      → {occupant_status} — bold if signs_of_life
    Recommended action   → action span colored
    Action priority      → {action_priority}/5
    Flood zone           → {flood_zone} — bold YES if true
    Slope                → {slope_degrees}°
    Slope risk           → {slope_risk}
    Nearest shelter      → {nearest_shelter} — {shelter_distance_m}m
    Shelter type         → {shelter_type}
    Road access          → {road_access} — bold BLOCKED if blocked
    Nearest road         → {nearest_road} — {road_distance_m}m
    Confidence           → {confidence}%
    Coordinates          → {lat}°N {lon}°E
    Building area        → {building_area_m2}m² (if available)
  </div>

  — PRE AND POST EARTHQUAKE IMAGES —
  Show side by side only if available.
  If neither available skip this section entirely.
  If only post available show only post with label.
  
  <div class="pre-post-row">
    <div>
      <div class="img-label">Before earthquake</div>
      <div class="img-placeholder" data-type="pre" data-id="{assessment_id}">
        [PRE-EARTHQUAKE IMAGE]
      </div>
    </div>
    <div>
      <div class="img-label">After earthquake</div>
      <div class="img-placeholder" data-type="post" data-id="{assessment_id}">
        [POST-EARTHQUAKE IMAGE]
      </div>
    </div>
  </div>

  — ACTION AND APPROACH INSTRUCTIONS —
  <div class="action-block">
    Large action tag — e.g. IMMEDIATE SEARCH AND RESCUE
    
    Write 3-5 specific approach instructions:
    — Which direction to approach from based on road_access
    — Any secondary collapse risk areas to avoid
    — Flood zone warning if applicable
    — Entry point recommendation
    — Any specific hazmat or fire warnings
    
    Write these as numbered instructions.
    Use building data and reasoning to make them specific.
    Do not write generic instructions.
  </div>

  — WARNINGS —
  <div class="warnings-row">
    One warn-tag span per warning from warnings array
  </div>

  — GEMMA 4 ASSESSMENT NOTE —
  <div class="gemma-note">
    Write 3-4 sentences summarizing key findings.
    Reference specific visual evidence.
    Mention the most critical risk factors.
    End with the single most important action.
    Write in selected language.
    Do not repeat what is already in the data grid above.
    Add value — explain WHY this severity was assigned.
  </div>

  — ROUTE FROM PREVIOUS BUILDING —
  (Skip for building #1 — show route from site entrance instead)
  
  <div class="route-section">
    <div class="route-header">
      Route from Building #{prev_num} to this building
      Total: {distance}m · {duration_min} min driving · {walking_min} min walking
    </div>
    <div class="route-steps">
      One route-step div per OSRM step
      Show instruction, distance, duration
    </div>
    
    Route map:
    <div class="map-placeholder route-map"
         data-map="route"
         data-from="{prev_lat},{prev_lon}"
         data-to="{lat},{lon}">
      [INTER-BUILDING ROUTE MAP]
    </div>
  </div>

  — EVACUATION ROUTE FROM THIS BUILDING TO SHELTER —
  
  <div class="evacuation-section">
    Shelter: {shelter_name} — {shelter_type} — {shelter_distance_m}m
    
    <div class="route-steps">
      Step by step from this building to shelter
      From get_building_route(building_coords, shelter_coords)
      Total distance and time
    </div>
    
    Evacuation route map:
    <div class="map-placeholder route-map"
         data-map="route"
         data-from="{lat},{lon}"
         data-to="{shelter_lat},{shelter_lon}">
      [EVACUATION ROUTE MAP — BUILDING TO SHELTER]
    </div>
  </div>

  — EMERGENCY FACILITIES FOR THIS BUILDING —
  <div class="emergency-facilities">
    List nearest hospital, clinic, police from tool results
    For each: name, type, distance, estimated drive time
    Include OSRM route time if available
  </div>

</div>
End of building card. Repeat for each building.

────────────────────────────────────────────────
SECTION F — GENERAL SAFETY WARNINGS
────────────────────────────────────────────────

<div class="safety-section">
  <h2 class="section-heading">Site-Wide Safety Information</h2>
  
  Compiled unique warnings across all buildings in site.
  Group by type:
  
  Structural risks:
    List buildings with secondary_collapse_risk
    
  Flood zone risks:
    List buildings in flood zone
    Add note about rain increasing urgency
    
  Access restrictions:
    List all blocked roads in the site
    Alternative routes if available from road data
    
  Hazmat risks (if any):
    List buildings with hazmat warnings
    Entry restrictions
    
  General instructions:
    Do not enter any severity 5 building alone
    Maintain radio contact at all times
    Report any secondary collapse immediately
    Evacuate to {nearest_shelter_name} if site conditions worsen
</div>

────────────────────────────────────────────────
SECTION G — REPORT FOOTER
────────────────────────────────────────────────

<div class="report-footer">
  Generated by BhumiDrishti
  Powered by Gemma 4 via Ollama — offline AI
  Data sources: OpenStreetMap contributors,
               Maxar Open Data Program,
               GLO-30 Digital Elevation Model
  All processing performed locally
  No data transmitted to external servers
  Generation timestamp: {timestamp}
  Report ID: {report_id}
</div>

═══════════════════════════════════════════════════════════════
STEP 4 — BUILDING REPORT CONTENT AND ORDER
═══════════════════════════════════════════════════════════════

For single building report — same structure as one building card
from Section E above but WITHOUT site information.

Start directly with building header.
No site summary. No site map. No priority list.
Just this building — full detail.

Order:
  Report header (building ID, date, language, no site info)
  Building location map (building + shelter + route to shelter)
  Photo (ground photo or best available chip)
  All data grid fields (same as site report building card)
  Pre/post images if available
  Action and approach instructions
  Warnings
  Gemma 4 assessment note
  Evacuation route to shelter with step by step and map
  Emergency facilities

═══════════════════════════════════════════════════════════════
VISUAL QUALITY RULES — FOLLOW EXACTLY
═══════════════════════════════════════════════════════════════

1. Every severity number MUST use a colored span. No exceptions.

2. Critical values MUST be bold:
   — Estimated occupants number
   — Signs of life
   — Flood zone YES
   — BLOCKED road access
   — Recommended action

3. Every building card MUST have a left border colored by severity:
   class="building-card sev-border-5" for severity 5
   class="building-card sev-border-4" for severity 4
   etc.

4. Section headings MUST have a bottom border.

5. Route steps MUST show step number, instruction, and distance.
   Never show route steps as plain text.
   Always use the route-step div structure.

6. Data grid MUST alternate row background colors.
   Use class="data-row" and class="data-row alt" alternating.

7. Stats badges MUST be colored:
   Total → neutral grey
   Extreme → dark red background
   Critical → red background
   Moderate → amber background
   Signs of life → pulsing red (class="stat-badge stat-life")
   Flood zone → blue background

8. Building #1 gets a special badge:
   <span class="priority-badge priority-first">FIRST RESPONSE</span>

9. Any building with signs_of_life gets a special banner:
   <div class="life-banner">
     ⚠ SIGNS OF LIFE DETECTED — PRIORITY RESPONSE
   </div>
   Place this immediately below building heading.

10. Page break before each new building:
    <div class="page-break"></div>
    So each building starts on a new page when printed.

═══════════════════════════════════════════════════════════════
STREAMING ORDER
═══════════════════════════════════════════════════════════════

Stream in this order so user sees progress:
1. Report header immediately
2. Situation summary — write this fully, user reads while rest loads
3. Statistics badges
4. Site map placeholder
5. Buildings one by one — complete each building before next
6. Safety section
7. Footer

Never stop mid-section. Complete each section fully before next.
""".strip()

# This variable stores the full report generation system prompt built from the shared base prompt.
REPORT_GENERATION_SYSTEM_PROMPT = build_bhumidrishti_system_prompt(REPORT_GENERATION_SYSTEM_ADDENDUM)

