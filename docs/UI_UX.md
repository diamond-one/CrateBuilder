# CrateBuilder desktop interaction decisions

The primary workflow is **generate a kit, audition pads, replace individual sounds**. The native Windows / CustomTkinter workspace keeps the physical pad grid central, shaping controls to its left and the inspected sound to its right. The selected gallery Grunge system is documented in [DESIGN.md](../DESIGN.md); its source is `desk_theme.py` and `assets/grunge.json`.

## Working surface

- **Header:** Open crate, Save crate, Audio & MIDI, MIDI preview status/toggle and Stop audio remain above the work area.
- **Kit:** selection count, Select all/Clear selection, Packs & working crates, active-source summary, Drum character, Surprise, Tempo/BPM, sampler and kit size. The left rail scrolls independently.
- **Library:** sample folders, indexing, folder tags, NI import, collection browsing and scan notes. A fresh installation starts here; a successful library update returns to Kit.
- **More:** role substitutes, Complete to 16, Refresh upper 8, export naming and the folder-to-MPC converter. Crate files remain in the header; sampler and kit size remain in Kit.
- **Center:** banks A–D, independent Lock bank, bank summary, explicit generation scope and one primary Generate action above the four-column physical pad grid. Play groove and Drums only sit below it.
- **Inspector:** current sound, waveform and Hear original pad; an explicit alternative-preview region; Use & lock sound; assigned-sound marks and metadata.
- **Delivery:** export format, current MIDI route, Export WAVs/Export MPC and SP Send remain below the work area. Send to SP appears for the SP sampler target.

The main window opens at 1440×960 with a 1200×760 minimum. The center stretches while the left rail and right inspector retain working widths and scroll independently. There is no mobile or web layout. Worn red identifies inspection and active navigation; pale warm buttons identify primary actions. Oswald serves display headings; Segoe UI serves working controls and filenames.

## Hear, then keep

Clicking a pad inspects and auditions its assigned sound. Space hears that pad again; Escape stops audio. The alternative menu and Previous/Next controls audition candidates, as do Left/Right outside text fields. They show **Trying:** feedback and the candidate reason without replacing the pad assignment, its lock state or its assigned-sound waveform/title.

**Hear original pad** returns to the assigned sound. **Use & lock sound** commits the alternative to the inspected pad, locks it, records its use and saves the crate. Status explains that the pad must be unlocked to regenerate it later. A locked bank blocks explicit replacement. Favourite/Hide and Clear pad refer to the assigned sound or current pad, not a candidate heard only as a preview.

## Selection and protection

Inspection and generation selection are independent. **Select** or Ctrl-click adds a pad to the generation set; a count and scope label describe what Generate will change. Current bank is the default, with Selected pads, Upper 8 and Whole crate as deliberate alternatives. Selection clears on bank navigation. Hardware MIDI flashes pads without changing selection.

Individual Lock protects that pad during generation. Lock bank is a separate persistent flag; unlocking a bank retains individual locks. Session snapshots copy protection flags. Generation changes only eligible requested assignments and retains outside assignments; a failed build preserves selection. Clear pad creates an unlocked empty assignment; locking the empty pad keeps generation from filling it.

## Audio and MIDI

MIDI preview has explicit OFF, starting and ON states; Off remains available during other work and invalidates pending connections. Bank audio loading runs independently of generation and alternative searches. The delivery strip describes the current preview route.

**Audio & MIDI** first presents audio output and controller. SP outputs are first. The simplified list prefers WASAPI when Windows exposes an endpoint through multiple drivers. Truncated MME names merge only when exactly one better-driver endpoint matches; ambiguous endpoints remain distinct. **Advanced · all drivers & MIDI mapping** starts collapsed and reveals the complete driver list, channel, note offset, learn and reset controls. Save settings and Cancel remain outside the scrolling form.

## SP destination review

**Send to SP…** opens a review dialog and automatically attempts a read-only connection check. The normal path is to choose a destination bank, inspect the project and pad map, and send. Local WAV preparation starts after a valid destination plan; a separate manual prepare step is not part of the visible path.

**Connection options…** starts collapsed. It contains the transfer helper path and automatic/manual port choice. Changing connection invalidates the previous destination plan and requires a new check. Destination bank, occupied-pad consequences and backup location remain in the main form. Replacing occupied pads requires the explicit replacement choice and a backup folder.

The form scrolls above a persistent bottom row containing Close and Send. Send names the target bank when ready and stays disabled until preparation and overwrite requirements are satisfied. Closing during transfer requests a stop after the current pad; completed pads remain recorded in the transfer journal. The dialog does not change the SP's selected project.

## Large collections and NI expansions

Library handles maintenance; Kit exposes the active-source summary and Packs & working crates. Importing indexes the archive separately from active drum-pack analysis and preserves installed NI structure. Source/filter changes retain the current kit.

The collection browser uses background paged queries, capped at 200 rows, with debounced search and stale-result rejection. Pack/category labels, result counts and filenames identify sounds. Audition, Use, Favourite, Hide and Export remain below scrolling metadata. Keyboard selection, Space/Enter audition, Ctrl-Enter use, Escape close and Ctrl-/Shift-selection support repeated browsing.

Favourites, hidden flags and recent use are local metadata. Hide explains its effect on future generation while preserving the current assignment. Library export uses explicit selected rows and a destination/name prompt before making verified, organised copies. Unsupported pad categories remain browseable with a reason beside the disabled action.

## Guidance and evidence boundary

Installed UX skills informed grouping, direct labels, explicit preview/commit behavior, persistent actions and visible state. Earlier UI/UX Pro Max searches returned website marketing patterns for desktop queries; those were not used as a desktop template. No web framework or HTML detector applies to this native application.

The behavior above is extracted from `workspace.py`, `ui.py`, `desk_theme.py`, `assets/grunge.json` and the existing collection browser. Automated regressions and the separate final screenshot review provide their own evidence; this document does not claim a new hardware transfer, acoustic listening check, screen-reader test or accessibility certification. Shared buttons implement a keyboard focus border and Return activation; that is narrower than full keyboard or assistive-technology verification.
