# CrateBuilder workspace

Mode: Operate. Platform: native Windows / CustomTkinter. Build path: code-led.

THESIS: Generate a kit, hear its pads and replace individual sounds with minimal
navigation. The user explicitly chose this primary workflow.

OWN-WORLD: The user explicitly selected Grunge from the existing theme gallery.
Its charcoal, warm paper lettering, worn red and Oswald heading voice are binding.
DESIGN.md and cratebuilder/desk_theme.py record the extracted palette. Use quiet
broken rules at section boundaries, with solid working surfaces and readable
Segoe UI filenames and controls. Do not introduce a competing visual direction.

STORY: Choose sources and character, generate a bank, hear and inspect a pad,
audition alternatives without changing it, explicitly keep a replacement, then
export to the chosen sampler. Library maintenance stays in its own tab.

FIRST VIEWPORT: Header with crate files and audio/MIDI controls; a scrolling
Kit/Library/More rail at left; persistent A–D bank controls, generation scope and
a physical 4×4 pad grid in the middle; selected sound and replacement controls
in the right inspector; delivery actions and status at the bottom. Empty first
run begins in Library. An existing crate opens into the working Kit view.

FORM: Three-column working screen with a flexible central pad grid, fixed-width
side panels and persistent delivery actions. Preserve distinct inspected,
multi-selected, locked, missing and warning states. Technical SP connection
options collapse while the destination and final Send action remain visible.
Reference seed: seed:21ccd2ddb3539b43; the explicit user-selected gallery direction
takes precedence over alternative visual worlds. No image comp was approved or
required for this code-led native working screen.

Quality bar: Faithful gallery palette and heading voice; readable text; known
sampler pad layout; no silent assignment while auditioning; protected pads remain
protected; long-running work stays responsive; important actions remain reachable
at the documented minimum window size. Regression tests and native captures are
the implementation evidence. The desktop screenshot is generated demo content,
not a supplied NI kit or a claim of hardware transfer verification.
