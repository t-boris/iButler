# Butler Core

Durable tasks, shared authorization and reliable delivery for iButler.

Butler Core is the local Python daemon behind the assistant's execution path. It records
work before acknowledging it, evaluates policy, tracks operations separately from message
delivery, and exposes a Unix-socket API through a CLI and a small Python client.

[Application overview](../../README.md) · [Architecture](../../docs/README.md) ·
[Verification](VERIFICATION.md) · [Changelog](CHANGELOG.md)

## Responsibilities

- Persist tasks, events, operations, approvals and delivery state in SQLite.
- Admit requests with stable idempotency keys and recover accounting after restarts.
- Apply persona grants, disclosure rules and parameter-bound approvals.
- Run integration adapters with bounded retries and explicit uncertain outcomes.
- Expose task status, correlated diagnostics, configuration versions and backup operations.
- Connect OpenClaw message and turn lifecycle events through the bundled bridge.

## Install

From this directory, using Python 3.11 or newer:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
butler-core --help
```

The base package uses the Python standard library at runtime. The optional `system1`
extra adds ONNX Runtime and tokenizers:

```sh
python -m pip install -e '.[system1]'
```

The [OpenClaw bridge](bridge/package.json) runs in the existing OpenClaw Node.js
environment. Integration adapters also depend on the owner's configured tools and private
workspace; installing the Python package alone does not provision those accounts.

## Run and inspect

The default state directory is `~/.local/share/butler-core`. For a development instance,
use a separate state directory:

```sh
export BUTLER_CORE_STATE="$PWD/.local-state"
butler-core daemon
```

Keep that process running and use another terminal with the same state setting:

```sh
export BUTLER_CORE_STATE="$PWD/.local-state"
butler-core ping
butler-core doctor
butler-core status --limit 20
butler-core operations --limit 20
butler-core deliveries --limit 20
butler-core config versions
```

Use `butler-core <command> --help` for exact parameters. `BUTLER_CORE_SOCKET` or the global
`--socket` option selects a different Unix socket. Configure the deployment's personas,
grants and adapters before submitting real operations.

| Command group | Purpose |
| --- | --- |
| `submit`, `execute`, `operation`, `status` | Submit work and inspect its progress |
| `approve`, `reject`, `revoke`, `cancel`, `resolve` | Owner authorization and operation control |
| `deliver`, `notify`, `deliveries` | Queue and inspect delivery |
| `config` | Propose, preview, activate and roll back configuration |
| `trace`, `metrics`, `doctor` | Diagnostics and health |
| `backup`, `admin` | Backup and offline administration |

## Modules

| Area | Source |
| --- | --- |
| Versioned contracts and retained examples | [contracts/](butler_core/contracts/) |
| SQLite persistence, tasks and events | [store.py](butler_core/store.py), [tasks.py](butler_core/tasks.py), [events.py](butler_core/events.py) |
| Policy and approvals | [policy.py](butler_core/policy.py), [approvals.py](butler_core/approvals.py) |
| Operations, routing and delivery | [operations.py](butler_core/operations.py), [routing.py](butler_core/routing.py), [delivery.py](butler_core/delivery.py) |
| External integrations | [adapters/](butler_core/adapters/) |
| Configuration, diagnostics and backups | [config.py](butler_core/config.py), [diagnostics.py](butler_core/diagnostics.py), [backup.py](butler_core/backup.py) |
| Service composition and socket interface | [service.py](butler_core/service.py), [server.py](butler_core/server.py), [client.py](butler_core/client.py) |
| OpenClaw message and lifecycle bridge | [bridge/](bridge/) |

The boundary and contract tests in [tests/](tests/) check the separation between these
modules. Integration tests use controlled adapters or compatibility fixtures where
available.

## Tests and build

```sh
make test
node --test bridge/test/*.test.js
```

Some compatibility tests expect the installed private workspace; see
[VERIFICATION.md](VERIFICATION.md) for the recorded environments and coverage.
`make verify` additionally uses deployment-specific interpreters and writes local evidence.

Native macOS builds require Xcode command-line tools and the Python wheel build tools:

```sh
python -m pip install --upgrade setuptools wheel
make build PYTHON="$PWD/.venv/bin/python"
```

The build creates the Swift launcher and Python wheel under `build/`.
[deploy.py](deploy.py) manages the configured private installation; review its settings
before deploying.

## Local files

`.local-state/`, virtual environments, bytecode, `build/`, `*.egg-info/`,
`bridge/node_modules/` and generated `evidence/` are local artifacts and are ignored.
The source tree retains the contracts, fixtures, bridge code and native launcher needed
to rebuild them.
