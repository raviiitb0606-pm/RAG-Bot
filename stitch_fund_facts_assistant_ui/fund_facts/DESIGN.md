---
name: Fund Facts
colors:
  surface: '#111319'
  surface-dim: '#111319'
  surface-bright: '#36393f'
  surface-container-lowest: '#0b0e13'
  surface-container-low: '#191c21'
  surface-container: '#1d2025'
  surface-container-high: '#272a30'
  surface-container-highest: '#32353a'
  on-surface: '#e1e2e9'
  on-surface-variant: '#bbcac2'
  inverse-surface: '#e1e2e9'
  inverse-on-surface: '#2e3036'
  outline: '#85948d'
  outline-variant: '#3c4a44'
  surface-tint: '#3edeb1'
  primary: '#56f1c2'
  on-primary: '#003829'
  primary-container: '#2dd4a7'
  on-primary-container: '#005742'
  inverse-primary: '#006c52'
  secondary: '#fcbb49'
  on-secondary: '#432c00'
  secondary-container: '#bf8612'
  on-secondary-container: '#3a2600'
  tertiary: '#d0d9e9'
  on-tertiary: '#28313d'
  tertiary-container: '#b4bdcc'
  on-tertiary-container: '#434c59'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#63fccc'
  primary-fixed-dim: '#3edeb1'
  on-primary-fixed: '#002117'
  on-primary-fixed-variant: '#00513d'
  secondary-fixed: '#ffdead'
  secondary-fixed-dim: '#fcbb49'
  on-secondary-fixed: '#281900'
  on-secondary-fixed-variant: '#604100'
  tertiary-fixed: '#dae3f3'
  tertiary-fixed-dim: '#bec7d7'
  on-tertiary-fixed: '#131c28'
  on-tertiary-fixed-variant: '#3e4754'
  background: '#111319'
  on-background: '#e1e2e9'
  surface-variant: '#32353a'
typography:
  headline-lg:
    fontFamily: Inter
    fontSize: 1.75rem
    fontWeight: '600'
    lineHeight: 2.25rem
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Inter
    fontSize: 1.25rem
    fontWeight: '600'
    lineHeight: 1.75rem
    letterSpacing: -0.015em
  headline-sm:
    fontFamily: Inter
    fontSize: 1rem
    fontWeight: '600'
    lineHeight: 1.5rem
    letterSpacing: -0.01em
  body-lg:
    fontFamily: Inter
    fontSize: 1rem
    fontWeight: '400'
    lineHeight: 1.6rem
    letterSpacing: 0em
  body-md:
    fontFamily: Inter
    fontSize: 0.875rem
    fontWeight: '400'
    lineHeight: 1.45rem
    letterSpacing: 0em
  body-sm:
    fontFamily: Inter
    fontSize: 0.75rem
    fontWeight: '400'
    lineHeight: 1.25rem
    letterSpacing: 0.01em
  label-lg:
    fontFamily: Inter
    fontSize: 0.875rem
    fontWeight: '500'
    lineHeight: 1.25rem
    letterSpacing: 0.01em
  label-md:
    fontFamily: Inter
    fontSize: 0.75rem
    fontWeight: '500'
    lineHeight: 1rem
    letterSpacing: 0.02em
  label-sm:
    fontFamily: Inter
    fontSize: 0.6875rem
    fontWeight: '500'
    lineHeight: 0.875rem
    letterSpacing: 0.03em
  data-tabular-md:
    fontFamily: Inter
    fontSize: 0.875rem
    fontWeight: '500'
    lineHeight: 1.25rem
    letterSpacing: 0.02em
  data-tabular-lg:
    fontFamily: Inter
    fontSize: 1.125rem
    fontWeight: '600'
    lineHeight: 1.5rem
    letterSpacing: 0.01em
rounded:
  sm: 0.25rem
  DEFAULT: 0.5rem
  md: 0.75rem
  lg: 1rem
  xl: 1.5rem
  full: 9999px
spacing:
  gutter: 1rem
  gutter-desktop: 1.5rem
  margin: 1rem
  margin-desktop: 2rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.75rem
  space-lg: 1.25rem
  space-xl: 2rem
---

## Brand & Style

The design system establishes a high-precision, zero-noise analytical environment engineered specifically for factual asset research and fund compliance inquiries. Drawing from modern financial terminal aesthetics and understated corporate minimalism, it trades consumer fintech flash and aggressive gradients for clarity, absolute legibility, and visual quietude.

### Personality & Tone
- **Objective & Rigorous:** Free of decorative embellishments, speculative data visuals, or gamified motifs. Every pixel reinforces authoritative data delivery.
- **Calm & Glare-Free:** Designed for prolonged analytical sessions, utilizing deep blue-grey surfaces and carefully managed contrast tiers to reduce ocular strain.
- **Audience:** Institutional and self-directed retail investors seeking direct, verifiable metrics and facts across selected funds, without conversational fluff or unsolicited financial advisory overhead.

## Colors

The palette relies on structural darkness with surgical, low-saturation accents to preserve the boundary between conversational elements, compliance mandates, and primary controls.

### Color Roles & Semantics
- **Base Canvas (`#0F1217`):** The foundational near-black blue-grey backplane for the main viewport and pinned composer base.
- **Elevated Surfaces:**
  - `surface-container-low` (`#171B22`): Sidebar container, persistent top bar, inactive panels.
  - `surface-container` (`#1E232C`): Assistant response cards, input fields, interactive modules.
  - `surface-container-high` (`#262C37`): Hover states, active scheme selections, modal overlays.
- **Structural Borders (`#2A303A`):** 1px hairline dividers separating panes, cards, and input boundaries without creating visual fragmentation.
- **Primary Teal Accent (`#2DD4A7`):** Reserved strictly for actionable intent: submit buttons, active states, focus indicators, and verified external source anchors.
- **Compliance Amber (`#F5B544`):** Restricted solely to regulatory notices, disclaimer banners, and non-advisory guardrails. Backed by tinted amber fill (`#261C0D`) and hairline border (`#4D3818`).
- **Typography Contrast Hierarchy:**
  - High Contrast (`#E6E9EF`): Headers, scheme names, numerical data points, active query text.
  - Medium Contrast (`#9AA3B2`): Labels, timestamps, secondary parameters, assistant responses.
  - Muted Contrast (`#6B7480`): Input placeholders, subtle source citations, inactive counters.

## Typography

The typography is built around clean structural density and tabular readability. All metrics—specifically currency symbols (`₹`), percentages (`%`), dates, NAV figures, and asset allocations—must enforce OpenType tabular figures (`font-feature-settings: "tnum" 1, "cv05" 1`).

- **Numerical Alignment:** Never use proportional numerals for financial figures or comparison tables.
- **Hierarchy Enforcement:** Headers use tighter negative tracking (`-0.02em` to `-0.01em`) to maintain solidity against dark backdrops. Captions, timestamps, and compliance indicators take slight positive letter-spacing (`0.02em` to `0.03em`) to remain sharp at small point sizes.

## Layout & Spacing

The layout adopts a fixed dual-pane desktop framework transitioning into an adaptive stacked view for tablet and mobile viewports.

### Grid & Layout Rules
- **Desktop (≥ 1024px):** Fixed-width persistent navigation sidebar (280px), central fluid chat and thread pane (maximum width 840px, centered), and pinned bottom composer with strict vertical clearances.
- **Mobile / Tablet (< 1024px):** Sidebar collapses into a slide-over drawer; thread pane occupies 100% viewport width minus safe margins (`1rem`).
- **Composer Docking:** The input composer is pinned rigidly above bottom viewport boundaries, floating with `1.25rem` spacing or docked flush over a subtle gradient bleed into `#0F1217`.

## Elevation & Depth

This system avoids blurred colored drop-shadows and neon glows, relying on systematic tonal stepping and hairline boundary definition.

- **Level 0 (Canvas):** `#0F1217` base background.
- **Level 1 (Side Panels & Persistents):** `#171B22` bounded by `1px solid #2A303A`.
- **Level 2 (Cards & Active Elements):** `#1E232C` with `1px solid #2A303A`. On interactive hover, the surface increments to `#262C37` while the border shifts to `#3B4452`.
- **Level 3 (Modals & Overlays):** `#1E232C` elevated by an ambient occlusion shadow: `0 12px 32px -4px rgba(0, 0, 0, 0.65)`, backed by a `rgba(7, 9, 12, 0.8)` backdrop scrim.
- **Focus Rings:** Non-offset, high-precision focus: `0 0 0 1px #0F1217, 0 0 0 3px rgba(45, 212, 167, 0.4)`.

## Shapes

The geometric framework balances technical rigor with structural comfort.

- **Base Radius (12px / `rounded-lg`):** Standard for response cards, informational callouts, modals, and container wrappers.
- **Input & Controls (8px / `rounded`):** Text fields, buttons, and user interaction triggers.
- **Pills & Status Chips (Full Pill / `rounded-full`):** Scheme filters (Large Cap, Mid Cap, etc.), example queries, and metadata badges.

## Components

### Persistent Disclaimer Banner
- **Placement:** Affixed directly beneath the primary header or pinned globally at the canvas top.
- **Structure:** Full-width strip or constrained container (`#261C0D`), bounded by a `1px` border (`#4D3818`).
- **Typography:** `label-md` using Amber `#F5B544`.
- **Content:** Prepended with a restrained warning icon (`⚠`), reading: *"Facts-only. No investment advice."*

### Sidebar Thread Items
- **Inactive:** Transparent background, text color `#9AA3B2`, padding `0.5rem 0.75rem`, rounded to 8px.
- **Active:** Surface `#1E232C`, text color `#E6E9EF`, border `1px solid #2A303A`.
- **Hover:** Surface `#171B22`, text `#E6E9EF`.

### Scheme Pills & Filter Chips
- **Scope:** 5 HDFC schemes (*Large Cap, Mid Cap, Small Cap, Multi Cap, Silver ETF FoF*).
- **Styling:** Fully rounded pill (`rounded-full`), padding `0.375rem 0.875rem`, font `label-md`.
- **Resting:** Background `#171B22`, border `1px solid #2A303A`, text `#9AA3B2`.
- **Active / Selected:** Background `rgba(45, 212, 167, 0.12)`, border `1px solid #2DD4A7`, text `#2DD4A7`.

### Message Stream
- **User Message Bubble:**
  - Right-aligned or self-contained flush right; surface `#1E232C`, border `1px solid #2A303A`.
  - Text: `#E6E9EF` (`body-md`).
- **Assistant Card:**
  - Left-aligned, full central width; surface `#171B22`, border `1px solid #2A303A`, corner radius `12px`, padding `1.25rem`.
  - Body: `#E6E9EF` (`body-md`), with strict tabular metric treatment for numbers.
  - Footer Meta: Separated by a `1px solid #2A303A` hairline; contains source link (teal `#2DD4A7` with external arrow icon) and timestamp reading *"Last updated from sources: 25 Oct 2026"* in muted `#6B7480` (`body-sm`).

### Refusal Notice Card
- **Styling:** Background `#171B22`, border `1px solid #2A303A`, left accent border `3px solid #6B7480`.
- **Icon:** Neutral info glyph in `#9AA3B2`.
- **Text:** Factual explanation of system scope limitations without apologies or conversational hedging.

### Scheme Clarification Card
- **Context:** Rendered when a query lacks unambiguous scheme association.
- **Layout:** Card surface `#1E232C`, presenting prompt text *"Select a scheme to view verified data:"* followed by an inline wrapped cluster of the 5 scheme pills.

### PII Alert Modal
- **Backdrop:** Scrim `#07090C` at 80% opacity.
- **Container:** Surface `#1E232C`, border `1px solid #4D3818`, radius `12px`, padding `1.5rem`.
- **Header:** Amber `#F5B544` alert title with direct instruction to remove sensitive personal or financial identifiers.

### Pinned Composer
- **Container:** Anchored to bottom, floating or docked with blur `#0F1217` backdrop.
- **Input Field:** Inset surface `#171B22`, border `1px solid #2A303A`, focus ring in teal `#2DD4A7`. Text `#E6E9EF`, placeholder `#6B7480`.
- **Footer Controls:** Tabular character counter (`label-sm`, `#6B7480`) paired with helper warning text (*"Provides factual data points only."*).
- **Submit Action:** Square 36px icon button; background `#2DD4A7`, icon `#0F1217`. Disabled state: `#1E232C` with `#6B7480` glyph.

### Loading State
- **Indicator:** Three inline horizontal dots (4px diameter) pulsing in opacity (`0.3` to `1.0`) with `#2DD4A7` tint across 1.2s cycles, maintaining exact vertical alignment with incoming text lines.