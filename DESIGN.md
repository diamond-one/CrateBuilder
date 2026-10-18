---
name: CrateBuilder
description: A Grunge sampler workspace for generating, hearing and shaping local sample kits.
colors:
  background: "#302e2b"
  panel: "#393631"
  inset: "#252522"
  text: "#e7e2d9"
  muted: "#c0b9ab"
  disabled: "#a69f93"
  control: "#4c4842"
  control-hover: "#605a51"
  border: "#767268"
  core-pad: "#403d38"
  creative-pad: "#4c4842"
  action: "#e1b1a4"
  action-hover: "#f0c9be"
  on-action: "#392a27"
  selected: "#a9433e"
  on-selected: "#fff3df"
  warning: "#e5bd79"
typography:
  display:
    fontFamily: "Oswald"
    fontSize: "30px"
    fontWeight: 700
  headline:
    fontFamily: "Oswald"
    fontSize: "22px"
    fontWeight: 700
  section:
    fontFamily: "Oswald"
    fontSize: "20px"
    fontWeight: 700
  title:
    fontFamily: "Segoe UI"
    fontSize: "20px"
    fontWeight: 700
  body:
    fontFamily: "Segoe UI"
    fontSize: "13px"
    fontWeight: 400
  label:
    fontFamily: "Segoe UI"
    fontSize: "12px"
    fontWeight: 400
  small:
    fontFamily: "Segoe UI"
    fontSize: "11px"
    fontWeight: 400
  technical:
    fontFamily: "Consolas"
    fontSize: "12px"
    fontWeight: 400
rounded:
  square: "0px"
  working: "1px"
spacing:
  tight: "4px"
  small: "8px"
  control: "12px"
  column: "16px"
  workspace: "18px"
  header: "22px"
  dialog: "24px"
components:
  button-primary:
    backgroundColor: "{colors.action}"
    textColor: "{colors.on-action}"
    rounded: "{rounded.working}"
    height: "38px"
  button-primary-hover:
    backgroundColor: "{colors.action-hover}"
    textColor: "{colors.on-action}"
  button-secondary:
    backgroundColor: "{colors.control}"
    textColor: "{colors.text}"
    rounded: "{rounded.working}"
    height: "34px"
    typography: "{typography.label}"
  button-secondary-hover:
    backgroundColor: "{colors.control-hover}"
    textColor: "{colors.text}"
  input:
    backgroundColor: "{colors.inset}"
    textColor: "{colors.text}"
    rounded: "{rounded.working}"
    typography: "{typography.body}"
  panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.working}"
  pad-core:
    backgroundColor: "{colors.core-pad}"
    textColor: "{colors.text}"
    rounded: "{rounded.working}"
    typography: "{typography.label}"
  pad-creative:
    backgroundColor: "{colors.creative-pad}"
    textColor: "{colors.text}"
    rounded: "{rounded.working}"
    typography: "{typography.label}"
  pad-inspected:
    backgroundColor: "{colors.selected}"
    textColor: "{colors.on-selected}"
    rounded: "{rounded.working}"
  navigation-active:
    backgroundColor: "{colors.selected}"
    textColor: "{colors.text}"
    rounded: "{rounded.working}"
---

# Design System: CrateBuilder

## Overview

**Creative North Star: "Grunge sampler workbench"**

The selected gallery Grunge direction gives a working sampler surface charcoal material, warm lettering and worn red inspection states. CrateBuilder is a Windows CustomTkinter desktop application. Its visual vocabulary supports making and hearing a kit repeatedly, with the physical pad layout as the central instrument.

Oswald gives short display headings their identity; Segoe UI keeps filenames, choices and controls readable. Broken rules supply worn character around working regions, while text and waveforms sit on quiet solid surfaces. The implementation sources are `cratebuilder/desk_theme.py`, `cratebuilder/assets/grunge.json`, `cratebuilder/workspace.py` and the corresponding widget methods in `cratebuilder/ui.py`.

**Key Characteristics:**
- Warm charcoal layers with restrained broken-line detail.
- Worn red inspection states and pale warm action buttons.
- Labelled working controls around a physical four-column pad grid.
- Explicit distinction between hearing an alternative and keeping it.

## Colors

The palette is earthy and low-chroma, with warm lettering over dark surfaces. Frontmatter values are normative; identical control and creative-pad values intentionally retain separate roles.

### Primary

- **Warm action:** `action` marks Generate, Use & lock sound and Send to SP, with dark `on-action` lettering and `action-hover` feedback.
- **Worn red:** `selected` identifies the inspected pad and active navigation. Pads and banks use `on-selected`; workspace tabs retain the theme's normal warm text.

### Secondary

- **Amber warning:** `warning` highlights substitutions, validation messages and pad warnings. Text and warning marks accompany the color.

### Neutral

- **Workbench charcoal:** `background` holds the window; `panel` groups controls; `inset` grounds the header, waveform, fields and delivery strip.
- **Working surfaces:** `control` and `control-hover` distinguish actions. Lower core pads use `core-pad`; upper creative pads use `creative-pad`.
- **Warm lettering:** `text` is the normal foreground; `muted` supports descriptions and metadata. `disabled` is the inherited disabled lettering.
- **Worn rule:** `border` outlines pads and fields and draws broken separators.

**The State Has Words Rule.** Preserve visible Select, Lock, LOCKED, PICK, warning marks and MIDI preview status; color alone must not carry these distinctions.

## Typography

**Display Font:** bundled Oswald, bold. **Working Font:** Segoe UI on Windows. **Technical Font:** Consolas for the SP destination and pad-map report.

The frontmatter records CustomTkinter pixel sizes before user or OS scaling. It does not impose browser line-height or tracking on native widgets. The display role is the app name; smaller Oswald roles serve section headings. The transfer dialog also uses an observed intermediate heading size (26px). The selected sound title uses Segoe UI bold and wraps in the inspector. Body and label roles serve instructions, filenames, navigation and controls; primary buttons use bold label text. The small role serves compact Select/Lock controls and technical captions.

**The Working Type Rule.** Reserve Oswald for short display headings. Keep pad labels, filenames, buttons and working text in Segoe UI.

## Layout

The main native window opens at 1440×960 and has a minimum of 1200×760. Header, bottom delivery strip and status/progress row surround a three-column work area. The left Kit/Library/More rail is approximately 228px; the right scrolling inspector is approximately 270px. The middle column expands with four equal pad columns. Outer body padding uses the workspace spacing token; columns use the column token.

The pad grid preserves physical sampler orientation: upper physical rows appear above lower rows. Bank buttons A–D and generation scope sit above it. An 8-pad setup shows two kit group bars; a 16-pad setup hides those bars. The grid stretches with the window and has no mobile reflow. Sidebar and inspector content scroll independently.

The main delivery row remains outside the scrolling work area. Its export label follows the sampler; Send to SP appears for the SP target. The SP transfer dialog opens at 880×800, minimum 760×660, with Close and Send below the scrolling form. Audio & MIDI opens at 690×610, minimum 620×540, with its own persistent action row. These are native window constraints, not CSS breakpoints.

## Elevation & Depth

Depth comes from solid tonal layering and thin borders. The workspace uses no decorative drop shadows, translucent cards or textured plates behind working text. The inset and panel surfaces define grouping. The worn divider is a five-pixel-high native canvas with a one-pixel dashed line. OS window chrome and native dialogs are outside these surface tokens.

## Shapes

Working frames, buttons, fields, menus and pads use almost-square corners. Header and delivery strips use square corners. Pads have a thin outline at rest and a stronger outline for inspection or generation selection. Native sliders, switches and scrollbars retain the gallery theme's rounded mechanical parts; that silhouette does not extend to action buttons or panels.

## Components

### Action buttons

Pale, almost-square controls distinguish actions that advance the current task. The shared helper gives ordinary buttons a 34px height; major workspace actions use 38px. Compact utility controls can be smaller. Shared buttons and pad buttons receive a two-pixel action-colored keyboard focus border and Return activation. This is an implemented keyboard treatment, not a claim of complete assistive-technology coverage.

### Pad grid

Each pad combines hardware number, role and shortened filename with separate Select and Lock controls below. The inspected pad has worn red fill and warm cream lettering. Generation-selected pads have a stronger action outline and PICK marker; inspection and generation selection remain independent. Locked and warning states have visible text or marks. MIDI feedback briefly flashes a pad without changing inspection or selection.

### Sound inspector

Current pad title and waveform precede **Hear original pad**. **Try another sound** groups the alternative menu, count, Previous/Next and **Use & lock sound**. Choosing or stepping through an alternative only auditions it and updates preview feedback. Keeping it replaces the inspected assignment, locks it and saves it. Favourite, Hide sound and Clear pad act on the assigned sound or pad, not the merely previewed alternative. Source, features, reason and warnings remain lower in the inspector.

### Inputs and navigation

Entries have an inset fill and two-pixel border; option menus use the control surface. The selected workspace tab uses worn red. Bank buttons retain textual locked status independently of active bank color. Long forms and secondary windows use the same palette.

### Delivery and connections

The main export strip states destination audio format and current MIDI route. **Send to SP…** opens a destination review: read the project, choose a bank, inspect the pad map and send prepared copies. Replacing occupied pads requires an explicit replacement choice and a backup folder. The final Send button names the chosen bank when ready. Helper path and port selection stay under **Connection options…**, collapsed initially. Audio endpoints and MIDI remapping similarly stay behind **Advanced · all drivers & MIDI mapping**.

### Feedback

Status copy, a thin progress bar and cancellation express background work. MIDI preview has visible OFF, starting and ON states. The workspace has no decorative entrance animation. Preserve the short pad playback flash as functional feedback.

## Do's and Don'ts

### Do:

- **Do** use the gallery Grunge palette and bundled Oswald for short headings.
- **Do** keep working labels and filenames in Segoe UI on quiet solid surfaces.
- **Do** preserve physical pad mapping and separate inspection, selection and protection states.
- **Do** distinguish preview feedback from the explicit Use & lock action.
- **Do** keep main delivery actions and transfer-dialog final actions outside scrolling content.

### Don't:

- **Don't** add texture behind working text or replace tonal grouping with decorative shadows.
- **Don't** silently apply an alternative merely because the user heard it.
- **Don't** hide hardware destination or overwrite consequences inside connection options.
- **Don't** describe illustrative sidecar snippets as the native implementation or accessibility verification.
