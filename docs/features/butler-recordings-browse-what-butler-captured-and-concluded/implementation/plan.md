---
type: plan
feature: butler-recordings-browse-what-butler-captured-and-concluded
title: "Butler Recordings: local browsing and retention"
issues:
  - id: I-1
    status: in_progress
    title: Build the recordings timeline and gap display
    summary: Add a Recordings section to the Butler panel with a day-grouped, source-filterable timeline. Group consecutive observations into expandable activity or call sessions, show time ranges and content indicators, and display non-selectable gaps with their reasons.
    requirements: [REQ-001, REQ-006]
    decisions: [DEC-001]
  - id: I-2
    status: in_progress
    title: Show observation details and available media
    summary: Add observation details showing source, time, stored facts and any retained transcript. Display available key frames, generated images and image originals; play available audio and video originals; and indicate when an original is unavailable.
    requirements: [REQ-002, REQ-003]
    decisions: [DEC-001]
  - id: I-3
    status: in_progress
    title: Retain and expire local speech transcripts
    summary: Implement local transcription and 30-day retention for the owner's speech and detected calls, subject to existing capture and participant-consent controls. Expire retained transcripts after the specified period.
    requirements: [REQ-007]
    decisions: [DEC-001, DEC-002]
  - id: I-4
    status: in_progress
    title: Manage individual media and marked-meeting retention
    summary: Provide individual media-file preserve and delete controls, with deletion limited to the selected file and associated records following their own retention policies. Allow the owner to manually mark a current detected meeting for local retention of microphone audio, system audio and video originals for 30 days, subject to capture and participant-consent gates.
    requirements: [REQ-004]
    decisions: [DEC-003, DEC-005, DEC-006]
  - id: I-5
    status: in_progress
    title: Enforce local and owner-only access
    summary: Enforce local-only, verified-owner visibility for recordings and associated content under existing observation rules. Apply verified-owner access to generated images and Display history in both Recordings and Display, and ensure existing pause and participant-consent controls govern capture and retention.
    requirements: [REQ-005]
    decisions: [DEC-001, DEC-004]
  - id: I-6
    status: in_progress
    title: Navigate to recordings by local voice command
    summary: Add a local voice command that opens Recordings at a requested time. Ensure the navigation request and recording content are not sent to the cloud agent.
    requirements: [REQ-008, REQ-005]
    decisions: [DEC-001]
updated: 2026-10-28
---

# Implementation plan — Butler Recordings: browse what Butler captured and concluded

## I-1: Build the recordings timeline and gap display

Add a Recordings section to the Butler panel with a day-grouped, source-filterable timeline. Group consecutive observations into expandable activity or call sessions, show time ranges and content indicators, and display non-selectable gaps with their reasons.

Requirements: REQ-001, REQ-006
Decisions: DEC-001

## I-2: Show observation details and available media

Add observation details showing source, time, stored facts and any retained transcript. Display available key frames, generated images and image originals; play available audio and video originals; and indicate when an original is unavailable.

Requirements: REQ-002, REQ-003
Decisions: DEC-001

## I-3: Retain and expire local speech transcripts

Implement local transcription and 30-day retention for the owner's speech and detected calls, subject to existing capture and participant-consent controls. Expire retained transcripts after the specified period.

Requirements: REQ-007
Decisions: DEC-001, DEC-002

## I-4: Manage individual media and marked-meeting retention

Provide individual media-file preserve and delete controls, with deletion limited to the selected file and associated records following their own retention policies. Allow the owner to manually mark a current detected meeting for local retention of microphone audio, system audio and video originals for 30 days, subject to capture and participant-consent gates.

Requirements: REQ-004
Decisions: DEC-003, DEC-005, DEC-006

## I-5: Enforce local and owner-only access

Enforce local-only, verified-owner visibility for recordings and associated content under existing observation rules. Apply verified-owner access to generated images and Display history in both Recordings and Display, and ensure existing pause and participant-consent controls govern capture and retention.

Requirements: REQ-005
Decisions: DEC-001, DEC-004

## I-6: Navigate to recordings by local voice command

Add a local voice command that opens Recordings at a requested time. Ensure the navigation request and recording content are not sent to the cloud agent.

Requirements: REQ-008, REQ-005
Decisions: DEC-001

## Progress — 2026-10-27

I-1–I-6 are implemented and undergoing full release verification. Focused regressions cover session/source/day grouping, child pagination, independent media actions, fixed 30-day expiry including preserved meeting copies, restart/new-call mark reset, owner denial, generated images/Display history, MCP content stripping, local voice routing and a real FFmpeg playback derivative. The native panel compiles with warnings as errors, and the 0.10.0 app/wheel build passes. Initial core suite: 402 tests passed; targeted ambient regressions: 223 tests passed. No request for the owner to repeat answered product questions was needed.
