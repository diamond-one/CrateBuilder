# CrateBuilder 1.1 validation

Verified 18 October 2026.

- **229 automated tests passed.** This includes the existing working app's
  library, matching, varied generation, session, MIDI, SP transfer and MPC
  program tests, plus five new workspace checks.
- New checks cover the selected gallery palette and working-text contrast,
  visible generation/export/MIDI actions at 1200×760, 1440×960 and 1600×1000,
  audition without assignment changes, explicit replacement and lock behavior,
  preservation across bank navigation, 8-pad grouping, and fixed SP dialog
  actions with connection details collapsed by default.
- The desktop workflow smoke check passed selected generation, independent
  bank locks, isolated session snapshots, simplified and advanced audio output
  choices, and three window sizes.
- Packaged diagnostics use generated audio to check indexing, inherited folder
  tags, variation, SP WAV output, preview rendering, NI collection workflows,
  personal marks, verified collection copies, native dependencies, the bundled
  SP helper and desktop dialog construction. No physical transfer is performed.
- All **14 packaged diagnostic checks passed** on the clean final executable.
  The embedded workspace bytecode was also checked against the current source
  after invalidating an earlier stale packaging cache.
- Native captures from that build cover the loaded 1440×960 workspace,
  Audio & MIDI settings and connected SP destination screen. The destination
  check read the current project; no destination bank was selected and no
  samples were sent. The README image is an actual window capture. Smaller
  layouts were checked programmatically rather than claimed as native captures.
- The separate finish reviewer returned **ship** with no material fixes for
  those three captured screens and the pinned Grunge/native working-screen
  brief. This verdict does not extend to hardware listening or transfers.

The GitHub baseline predates the user's supplied working app. This update also
publishes those existing catalogue, NI collection, 16-pad, live MIDI and reviewed
SP transfer features so the repository represents the redesigned working app.

Physical MIDI/audio listening, SP writes and MPC/JJOS program loading were not
retested for this release. The screenshots use private generated demonstration
audio; no sample packs, personal catalogue, sessions, or device settings are
included in the repository or release. This is not a screen-reader accessibility
certification. Source samples are not modified.
