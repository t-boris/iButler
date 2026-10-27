"""Tested Mermaid templates for the display's diagram cards (ambient 0.9.0).

One source, three uses: Butler.app renders every template for real in its layout checks, the agent's playbook
(``docs/display-diagrams.md`` in its workspace) is generated from the same file, and ``--check`` proves an installed
playbook still contains every template verbatim. Nothing here talks to a model or a network.

    python -m butler_ambient.diagram_templates --markdown            # the playbook text
    python -m butler_ambient.diagram_templates --check <playbook.md>  # exit 1 when a template is missing or changed
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PATH = Path(__file__).parent / "assets" / "diagram_templates.json"
EXAMPLE = Path(__file__).parent / "assets" / "drawing_example.svg"

# Diagram types the bundled Mermaid draws. A template must start with one of them (after an optional %%{init}%% line).
SUPPORTED = ("flowchart", "sequenceDiagram", "stateDiagram-v2", "erDiagram", "timeline", "xychart-beta", "quadrantChart", "mindmap", "C4Context")

HEADER = """# Схемы на дисплее Butler (карточка `diagram`)

Файл создан автоматически из `diagram_templates.json` приложения Butler; не правь его руками.
Каждый шаблон ниже отрисован в настоящем окне Butler.app при проверке сборки: он разбирается и рисуется. Бери шаблон, меняй только подписи и узлы,
остальное (цветовые строки `classDef`, `UpdateElementStyle`, `%%{init}%%`) оставляй: они дают схеме цвета консоли.

## Правила
- Одна схема — одна мысль; 5–15 узлов. Больше — раздели на две карточки или сгруппируй узлы в `subgraph`.
- Подписи короткие, на языке владельца. Архитектура по умолчанию — шаблон `architecture`.
- В этих местах русский (и любой не-ASCII) текст ОБЯЗАТЕЛЬНО в кавычках, иначе схема не разберётся: подпись связи в `erDiagram` (`: "создаёт"`), подписи `x-axis [...]` в `xychart-beta`, оси и точки в `quadrantChart`. Заголовок `title` в `quadrantChart` без кавычек.
- Не поддерживается: `architecture-beta`, `block-beta`, `kanban`, `packet-beta`; клики по узлам (`click`) не работают. Не используй их.
- Исходник до 8000 символов. Если Mermaid не разобрал схему, владелец увидит текст ошибки и исходник; тебе это не сообщается, поэтому держись шаблонов.
- Дорисовка той же схемы — поле `replaces` с id прежней карточки, а не новая карточка.
"""


DRAWING_RULES = """
## Рисунок `svg`: когда Mermaid не хватает
Свой рисунок для инфографики, нестандартной раскладки или иллюстрации: карточка `{"kind":"svg","svg":"<svg …>…</svg>"}`. Схемы из узлов и стрелок лучше делать Mermaid; `svg` нужен для всего остального.
- Обязателен `viewBox` (например `0 0 800 450`). Ширину и высоту корня не задавай: размер выбирает панель.
- Разрешены только: `svg g defs linearGradient radialGradient stop rect circle ellipse line polyline polygon path text tspan title desc marker`. Скриптов, `style`, `class`, ссылок, картинок, `use`, анимации нет. Если что-то из этого есть, инструмент ответит «Card refused: …» с названием лишнего элемента или атрибута.
- Фон прозрачный: окно тёмное (#0b1410). Палитра (заливка / обводка / текст): основное #143d2a / #7fbf92 / #d6ffe0; внешний мир #14263d / #6fa8dc / #d6e8ff; хранилище #2a1440 / #b38cdc / #ecdcff; внимание #3d2a14 / #e0a458 / #ffe9c9. Текст и фигуры без `fill` панель красит в #d6ffe0.
- Стрелки: `<marker>` в `<defs>` и `marker-end="url(#arrow)"`. Ссылаться можно только на элементы этого же рисунка, вида `url(#id)`.
- Шрифт 14–22, подписи короткие, русский текст пиши как есть. Подпись под рисунком даёшь полем `markdown`.

Пример (проверенный: проходит очистку и рисуется):

```svg
{example}
```
"""


def load(path=PATH):
    templates = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(templates, list) or not templates:
        raise ValueError("no templates")
    return templates


def kind_of(source):
    """The Mermaid diagram type of a template: its first line that is not an init directive."""
    for line in source.splitlines():
        if line.strip() and not line.lstrip().startswith("%%"):
            return line.split()[0]
    return ""


def markdown(templates=None):
    templates = load() if templates is None else templates
    parts = [HEADER]
    for item in templates:
        parts.append(f"## {item['id']}\n{item['use']}\n\n```mermaid\n{item['mermaid'].rstrip()}\n```\n")
    parts.append(DRAWING_RULES.replace("{example}", EXAMPLE.read_text(encoding="utf-8").rstrip()))
    return "\n".join(parts)


def missing(text, templates=None):
    """Ids of the templates whose exact source is not inside ``text``."""
    templates = load() if templates is None else templates
    absent = [item["id"] for item in templates if item["mermaid"].rstrip() not in text]
    return absent + (["drawing-example"] if EXAMPLE.read_text(encoding="utf-8").rstrip() not in text else [])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--markdown", action="store_true", help="print the agent playbook")
    group.add_argument("--check", metavar="PLAYBOOK", help="verify that an installed playbook holds every template verbatim")
    args = parser.parse_args(argv)
    if args.markdown:
        sys.stdout.write(markdown())
        return 0
    absent = missing(Path(args.check).read_text(encoding="utf-8"))
    print("playbook holds every template" if not absent else "missing or changed: " + ", ".join(absent))
    return 1 if absent else 0


if __name__ == "__main__":
    sys.exit(main())
