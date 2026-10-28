---
type: feature
id: butler-recordings-browse-what-butler-captured-and-concluded
title: "Butler Recordings: browse what Butler captured and concluded"
status: implementing
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
  Acceptance Criteria: partial
understanding_notes:
  Problem: Владельцу нужен доступ к сохранённым материалам и выводам Butler.
  Target Users: Раздел предназначен для подтверждённого владельца.
  Primary Workflow: Выбраны группировка по сессиям, голосовой переход ко времени и ручная отметка текущей встречи.
  Permissions: Действуют существующие правила видимости, записи и согласия.
  Failure Scenarios: Требуется показывать интервалы пропусков с причинами; существующая история уже показывает несколько типов пропусков.
  Data Model: Транскрипты и аудио отмеченных встреч хранятся 30 дней; детали реализации остаются за разработчиком.
  Notifications: Уведомления не входят в описанную область задачи.
  Security: Материалы остаются локальными и доступны только владельцу по существующим правилам.
  Analytics: Аналитика использования не предусмотрена.
  Dependencies: Существуют история наблюдений и локальные операции сохранения и удаления медиа; текущая история показывает только факты.
  Acceptance Criteria: Критерии для двух уточнённых требований зафиксированы; реализация остальной спецификации ещё не подтверждена.
questions_left: 0
---

# Butler Recordings: browse what Butler captured and concluded

## Idea

The owner has no place in Butler.app to look back at what Butler captured: audio, video, images and the conclusions drawn from them. Today raw camera, microphone, system-audio and screen media sit in a temporary spool (512 MiB by default, now configurable, 24 h maximum). Each original is deleted as soon as local analysis has produced its facts, except audio and video originals of detected meetings that the owner manually marks for retention, which are retained locally for 30 days. What stays is a set of observations with structured facts (activity, topic, person, appearance), 51 preserved key frames, generated images and the in-memory display history. Transcripts of the owner's speech and every detected call are retained locally for 30 days, subject to existing capture and participant-consent gates. The analysis prompt forbids quoting speech. The owner asked on 2026-10-25, during a Google Meet, for a place in the app to see recordings, audio, video and pictures. Scope: a "Записи" section in the Butler panel. It shows a timeline by day and source, the facts of each observation, key frames and preserved originals (playable when present), generated images, and preserve and delete controls for individual media files. Everything is local only and follows the existing visibility rules. Out of scope until the owner decides: sending any of this to the cloud agent.

## Problem

## Scope
