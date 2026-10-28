---
type: feature
id: butler-recordings-browse-what-butler-captured-and-concluded
title: "Butler Recordings: browse what Butler captured and concluded"
status: implemented
owner: Boris Tsekinovsky
created: 2026-10-26
provenance: Created by an AI agent through MarkView from Owner request 2026-10-25 (voice, during a Google Meet); store inventory of ~/.local/share/butler-ambient on 2026-10-25
understanding:
  Problem: known
  Target Users: known
  Primary Workflow: known
  Permissions: known
  Failure Scenarios: known
  Data Model: known
  Notifications: n/a
  Security: known
  Analytics: n/a
  Dependencies: known
  Acceptance Criteria: known
understanding_notes:
  Problem: Владельцу нужен доступ к сохранённым материалам и выводам Butler.
  Target Users: Раздел предназначен для подтверждённого владельца.
  Primary Workflow: Выбраны группировка по сессиям, голосовой переход ко времени и ручная отметка текущей встречи.
  Permissions: Действуют существующие правила видимости, записи и согласия.
  Failure Scenarios: Требуется показывать интервалы пропусков с причинами; существующая история уже показывает несколько типов пропусков.
  Data Model: Транскрипты и аудио/видео отмеченных встреч хранятся 30 дней; медиакнопки действуют на отдельный файл.
  Notifications: Уведомления не входят в описанную область задачи.
  Security: Материалы остаются локальными и доступны только владельцу по существующим правилам.
  Analytics: Аналитика использования не предусмотрена.
  Dependencies: Существуют история наблюдений и локальные операции сохранения и удаления медиа; текущая история показывает только факты.
  Acceptance Criteria: Все критерии реализованы и связаны с автоматическими проверками; результаты полной проверки релиза и CI перечислены в implementation/verification.md.
questions_left: 0
---

# Butler Recordings: browse what Butler captured and concluded

## Idea

The owner has no place in Butler.app to look back at what Butler captured: audio, video, images and the conclusions drawn from them. Today raw camera, microphone, system-audio and screen media sit in a temporary spool (512 MiB by default, now configurable, 24 h maximum). Each original is deleted as soon as local analysis has produced its facts, except audio and video originals of detected meetings that the owner manually marks for retention, which are retained locally for 30 days. What stays is a set of observations with structured facts (activity, topic, person, appearance), 51 preserved key frames, generated images and the in-memory display history. Transcripts of the owner's speech and every detected call are retained locally for 30 days, subject to existing capture and participant-consent gates. The analysis prompt forbids quoting speech. The owner asked on 2026-10-25, during a Google Meet, for a place in the app to see recordings, audio, video and pictures. Scope: a "Записи" section in the Butler panel. It shows a timeline by day and source, the facts of each observation, key frames and preserved originals (playable when present), generated images, and preserve and delete controls for individual media files. Everything is local only and follows the existing visibility rules. Out of scope until the owner decides: sending any of this to the cloud agent.

## Problem

The owner needs a local view of the media and speech Butler retained, together with the conclusions it derived and honest explanations for missing intervals or originals.

## Scope

Implemented in ambient 0.10.0: owner-only Recordings, expandable day/source sessions, observation facts and transcripts, media viewing/playback, individual file controls, marked-meeting audio/video retention, generated images/current Display history, and local voice time navigation. Existing source/capture/consent controls remain authoritative. Recorded content is not exported to the cloud agent. Full requirement-to-code/test mapping and release evidence are in [implementation verification](implementation/verification.md).

## Owner follow-up — 2026-10-28

DEC-008/Q-007/Q-008 add persistent owner-gated UI history, the Components screenshot repair, truthful Back Burner plan/receipt reporting, future nightly activation and two confirmed daytime catch-up runs. REQ-009–REQ-011 and I-7–I-9 are implemented. Release 0.11.0 uses the full local and CI gates before normal deployment.

## Latest Display decision — 2026-10-28

The owner explicitly rejected the recurring identity-blocked Display. DEC-009 removes biometric/observation checks for local Display visibility, selection, controls and Clear. Persistent history remains. Recordings keeps its independent policy. This supersedes the earlier Display-gate choice in Q-007 and DEC-008. Release 0.11.1 verifies visibility under denied gates and failed UI polls.
