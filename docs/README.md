# iButler documentation

Start with the [application overview](../README.md) for the component map and source setup.

## Architecture and design

| Document | Scope |
| --- | --- |
| [Butler v2 architecture](Butler-Architecture-v2.md) | Repository audit, layered target design, memory, personas and migration plan |
| [AI Butler architecture](AI-Butler-Architecture.md) | Earlier detailed design for orchestration, knowledge, policy, research and news |
| [OpenClaw-first architecture](../OpenClaw-first%20AI%20Butler%20Architecture.md) | Original OpenClaw integration baseline |
| [Multi-agent coding pipeline](../OpenClaw-Multi-Agent-Coding-Pipeline.md) | Telegram-driven coding workflow and agent responsibilities |

Architecture documents include proposed capabilities and migration work. Use the module
source, README files and verification records to assess the implemented behavior.

## Implementation guides

| Guide | Covers |
| --- | --- |
| [Butler Core](../implementation/butler-core/README.md) | Installation, daemon, CLI, modules, tests and build |
| [Ambient Awareness](../implementation/ambient-awareness/README.md) | Native macOS experience, sensing, local models, permissions and MCP |
| [News Video](../implementation/news-video/README.md) | Episode validation, speech, rendering and publication |
| [Bamberg News](../implementation/bamberg-news/README.md) | Source policy, separate destination, schedules and checks |
| [WhatsApp Ingestion](../implementation/whatsapp-ingest/README.md) | Passive inbox path, installation and regression checks |

## Verification and engineering

- [Core verification](../implementation/butler-core/VERIFICATION.md),
  [changelog](../implementation/butler-core/CHANGELOG.md) and
  [routing datasets](../implementation/butler-core/DATASETS.md).
- [Ambient verification](../implementation/ambient-awareness/VERIFICATION.md) and
  [working notes](../implementation/ambient-awareness/WORKING-NOTES.md).
- [Attention implementation plan](../tasks/attention-architecture-plan.md).
- [Development task history](../tasks/todo.md) and [engineering lessons](../tasks/lessons.md).

Generated verification logs, run reports, screenshots and snapshots stay in local
`evidence/` directories. Those directories are ignored; the source tests, verification
runners and written verification records remain in Git.

## Todoist integration design

Read the [requirements](../todoist-integration/requirement.md),
[design](../todoist-integration/design.md) and
[implementation plan](../todoist-integration/plan.md) together. Runtime adapters are in
[Butler Core](../implementation/butler-core/butler_core/adapters/).
