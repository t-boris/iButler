#!/usr/bin/env python3
"""Reapply the reviewed ingest-only patch to the installed WhatsApp extension.

Validate every target before writes. An unfamiliar extension layout is an error;
the caller must leave WhatsApp disabled until reviewed. No gateway restart here.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import sys


HERE = Path(__file__).resolve().parent
MARKER = "/* butler-ingest-only: 2026-10-14 */"
IMPORT = 'import { bufferWhatsAppInboxMessage, guardIngestOnlySocket } from "./butler-ingest-only.mjs";\n'


def replace_once(text, old, new):
    if new in text:
        return text
    if text.count(old) != 1:
        raise ValueError(f"Unexpected WhatsApp extension layout: {old[:70]!r}")
    return text.replace(old, new, 1)


def prepare_patch(dist):
    monitors = [p for p in dist.glob("monitor-*.js")
                if "function createWebOnMessageHandler(params)" in p.read_text()]
    sessions = [p for p in dist.glob("session-*.js")
                if "async function createWaSocket(" in p.read_text()]
    if len(monitors) != 1 or len(sessions) != 1:
        raise ValueError("Expected one WhatsApp monitor and socket factory")
    monitor, session = monitors[0], sessions[0]
    text = monitor.read_text()
    text = replace_once(text, '\treturn async (msg) => {\n\t\tconst cfg = params.loadConfig?.() ?? params.cfg;',
                        '\treturn async (msg) => {\n\t\t' + MARKER + '\n'
                        '\t\tawait bufferWhatsAppInboxMessage(msg);\n'
                        '\t\treturn; // Never route personal chats to a conversational agent.\n'
                        '\t\tconst cfg = params.loadConfig?.() ?? params.cfg;')
    # Preserve the existing ghost patch while also handling a fresh extension.
    old = '\tconst presence = options.selfChatMode ? "unavailable" : "available";'
    ghost = '\tconst presence = "unavailable"; /* wa-ghost: never broadcast online presence (Boris 2026-06-04) */'
    text = replace_once(text, old, ghost)
    if not text.startswith(IMPORT):
        text = IMPORT + text
    socket = session.read_text()
    if '\t\tmarkOnlineOnConnect: false,' not in socket:
        raise ValueError("WhatsApp socket must not mark the account online")
    socket = replace_once(socket, '\tsock.ev.on("creds.update",',
                          '\t' + MARKER + '\n\tguardIngestOnlySocket(sock);\n\tsock.ev.on("creds.update",')
    if not socket.startswith(IMPORT):
        socket = IMPORT + socket
    return {monitor: text, session: socket}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path.home() / ".openclaw/extensions/whatsapp/dist")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    patches = prepare_patch(args.dist)
    if args.check:
        if any(p.read_text() != text for p, text in patches.items()):
            raise ValueError("WhatsApp ingest-only patch is missing; leave channel disabled")
        for source, dest in [(HERE / "lib/ingest-only.mjs", args.dist / "butler-ingest-only.mjs"),
                             (HERE / "lib/write-inbox.py", args.dist / "write-inbox.py")]:
            if not dest.exists() or source.read_bytes() != dest.read_bytes():
                raise ValueError("WhatsApp inbox helper is missing or outdated")
        print("WhatsApp ingest-only patch verified")
        return
    for text in patches.values():
        subprocess.run(["/Users/boris/.nvm/versions/node/v22.22.3/bin/node", "--input-type=module", "--check"],
                       input=text, text=True, check=True)
    for source, dest in [(HERE / "lib/ingest-only.mjs", args.dist / "butler-ingest-only.mjs"),
                         (HERE / "lib/write-inbox.py", args.dist / "write-inbox.py")]:
        shutil.copy2(source, dest)
    for path, text in patches.items():
        if path.read_text() == text:
            continue
        backup = path.with_suffix(path.suffix + ".pre-ingest-only-2026-10-14.bak")
        if not backup.exists():
            shutil.copy2(path, backup)
        tmp = path.with_suffix(path.suffix + ".ingest-only.tmp")
        tmp.write_text(text)
        tmp.replace(path)
    print("WhatsApp ingest-only patch installed; restart gateway after config validation")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
