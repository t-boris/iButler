"""Coverage-aware approximate time accounting without double counting modalities."""
from __future__ import annotations

import math
from .analysis import ACTIVITIES, export_observations


def summarize(store, start, end, *, local=False, expire=True):
    if not math.isfinite(start) or not math.isfinite(end) or not start < end or end - start > 366 * 86400:
        raise ValueError("select a positive period of at most 366 days")
    rows = store.history(start, end) if expire else store.history(start, end, expire=False)
    edges = {start, end}
    for row in rows:
        edges.update((max(start, row["start"]), min(end, row["end"])))
        if row["source"] == "screen":
            edges.add(min(end, row["start"] + 60))
    edges = sorted(edges)
    durations = {key: 0.0 for key in ACTIVITIES}
    gaps = []
    covered = 0.0
    priorities = {"screen": 4, "camera": 3, "microphone": 2, "system-audio": 1}
    for a, b in zip(edges, edges[1:]):
        candidates = [r for r in rows if r["start"] <= a and r["end"] >= b]
        if not candidates:
            if gaps and gaps[-1]["end"] == a:
                gaps[-1]["end"] = b
            else:
                gaps.append({"start": a, "end": b})
            continue
        covered += b - a
        # A screenshot can inform at most the next minute, only inside actually
        # observed intervals. It never fills a camera/audio gap.
        candidates.extend(r for r in rows if r["source"] == "screen" and r["start"] <= a < r["start"] + 60)
        candidates.sort(key=lambda r: priorities[r["source"]], reverse=True)
        activities = [f.get("activity", "unknown") for r in candidates for f in r["facts"]
                      if f["kind"] == "activity" and f["confidence"] >= 0.5]
        activity = next((x for x in activities if x != "unknown"), "unknown")
        durations[activity] += b - a
    events = store.execute("SELECT start,end,source,reason FROM events WHERE start<? AND (end IS NULL OR end>?)",
                           (end, start))
    result = {"start": start, "end": end, "covered_seconds": covered,
              "coverage_fraction": covered / (end - start), "approximate_seconds": durations,
              "gaps": gaps, "source_events": events,
              "work_topics": sorted({f["topic"] for r in rows for f in r["facts"] if f.get("topic", "unknown") != "unknown"}),
              "uncertainty": "Approximate model inferences within observed intervals; gaps are unassigned.",
              "evidence": export_observations(rows)}
    if local:
        result["local_observations"] = rows
    return result


def summary_text(summary):
    activities = {"work": "работа", "browsing": "просмотр сайтов", "youtube": "видео", "reading": "чтение",
                  "rest": "отдых", "entertainment": "развлечения", "unknown": "не определено"}
    topics = {"software development": "разработка", "writing": "тексты", "communications": "общение",
              "research": "исследования", "planning": "планирование", "finance": "финансы", "unknown": "не определено"}
    minutes = ", ".join(f"{activities.get(key, key)}: ~{value / 60:.1f} мин" for key, value in summary["approximate_seconds"].items() if value)
    sources = ", ".join(x["observation_id"] for x in summary["evidence"][:8]) or "нет"
    work_topics = ", ".join(topics.get(topic, topic) for topic in summary['work_topics']) or "не определены"
    return (f"Дневная сводка Butler\n{minutes or 'Распознанных занятий нет.'}\n"
            f"Возможные рабочие темы: {work_topics}.\n"
            f"Охват наблюдений: {summary['coverage_fraction']:.0%}; "
            f"не наблюдалось: ~{(summary['end'] - summary['start'] - summary['covered_seconds']) / 60:.1f} мин.\n"
            f"Это приблизительная оценка модели; промежутки без наблюдений не распределены.\nНаблюдения: {sources}")
