# CrateBuilder

<!-- impeccable:product-schema 1 -->

## Platform

Windows desktop. Existing Python / CustomTkinter application.

## Users and purpose

Music makers using local sample collections with an SP-404MKII or MPC1000.
The user confirmed the primary workflow: generate a kit, audition pads, and
replace individual sounds. Kits should be musically useful and surprising.

## Operating context

Four persistent banks, A–D, with either one 16-pad kit per bank or two 8-pad kits.
The upper eight of a 16-pad kit hold percussion and foley. Hardware pad numbering
depends on the chosen sampler. Computer audio can be triggered through MIDI.

## Capabilities and constraints

Preserve existing matching, variation, folder tagging, library indexing, NI sample
collection browsing, source filters, session persistence, bank/pad locks, selected
generation, WAV and MPC program export, and reviewed SP USB transfer.
Original samples stay in place. Native instruments and kit processing are not
rendered. Background work must not block the interface. This is a desktop app,
not a website or a replacement audio engine.

## Brand commitments

Apply the Grunge direction selected from the existing theme gallery, including
its charcoal, warm lettering, worn red and Oswald heading font. The README uses
the requested ADHD writing skill: short setup, bounded steps and direct actions.

## Evidence

The working source supplied by the user is newer than the GitHub baseline.
Existing regression tests and generated audio fixtures support verification.
No new physical sampler transfer or listening verification is assumed.
