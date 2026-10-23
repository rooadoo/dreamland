# DREAMLAND

DREAMLAND is an LLM-emulated computing environment. The current runtime exposes
that environment through a real SSH transport, but SSH is only the first entry
point. The larger goal is a believable, operator-guided computing network made
of simulated systems, users, services, files, programs, and history.

The project uses the creative behavior of LLMs to role play varied computing
environments—including enterprise, home, research, industrial, and fictional
settings—while keeping enough structured state to make sessions coherent. It
is currently a research project with potential uses in training, information
security, and entertainment.

## Current Slice

- Paramiko-backed, password-authenticated SSH server with an Ubuntu-like
  default profile.
- Optional [native SSH lobby](docs/SSH_LOBBY.md) with a short agreement,
  curated worlds, fresh isolated visits, and public report references.
- Three bundled [starter worlds](lobby/README.md#selected-worlds): Morrowbasket
  Markets, Common Ground Colonyship, and the Muirbank Alpha offshore facility.
- Command-agent boundary with deterministic and LangGraph-backed agents.
- Structured command results for terminal output and state updates.
- World profiles for entry-host metadata, broader environment context, mutable
  host state, and prompt modules for behavior.
- A session-local EnrichmentAgent that runs automatically and uses deterministic
  safe-apply guards before adding bounded background detail.
- Local Dreamland console for logs, Langfuse inspection, session state diffs,
  and process control.
- Configurable logging, output safeguards, model controls, and optional tracing.

## Setup

Use a project-local virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Copy the sanitized config template and fill in local values:

```sh
cp dreamland.example.ini dreamland.ini
```

`dreamland.ini` is ignored by git because it can contain local API keys.

## Run

Provide the required attendee SSH password through the process environment. Do
not put it in `dreamland.ini`:

```sh
export DREAMLAND_SSH_PASSWORD='replace-with-a-local-secret'
```

Start the default interactive process:

```sh
.venv/bin/python -m dreamland
```

The process starts the SSH server and opens the local Dreamland console. In
another terminal, connect over SSH:

```sh
ssh -p 2222 test@127.0.0.1
```

The first run creates a dedicated SSH host key in the operator's state
directory, and later runs by that service user reuse it. Preserve or mount that
location for service deployments; see `docs/OPERATIONS.md` for the path
override, fingerprint, backup, and intentional-rotation procedures.

Any nonempty SSH username is accepted with the configured password and becomes
an untrusted session label. SSH exposes simulated shell and exec sessions only;
operator commands remain in the local console and admin interfaces.

Automatic EnrichmentAgent work is enabled in `safe` mode by default. It can
make background provider calls and apply validated additive detail while a
session is active. Operators who need an explicit comparison or a
no-background-call run can select `shadow` mode or disable enrichment in
`dreamland.ini`.

For service-style operation without the local console:

```sh
.venv/bin/python -m dreamland --daemon
```

## Test

```sh
.venv/bin/python -m unittest
```

Development quality checks:

```sh
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff format --check .
.venv/bin/ruff check .
.venv/bin/mypy
```

Live provider tests are opt-in and use the configured model reference:

```sh
DREAMLAND_LIVE_PROVIDER_TESTS=1 .venv/bin/python -m unittest tests.test_live_agent
```

## Documentation

Start with the project model, then use the topic docs as needed:

- [Documentation Index](docs/INDEX.md)
- [Documentation System](docs/DOCUMENTATION.md)
- [Project Model](docs/PROJECT_MODEL.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Docker Operator Console](docs/OPERATIONS.md#docker-operator-console): connect to the running SSH backend and adopt updated model defaults.
- [Beta SSH Deployment](docs/BETA_SSH_DEPLOYMENT_PLAN.md) and [Companion API Handoff](docs/BACKEND_DEPLOYMENT_UI_HANDOFF.md)
- [Native SSH Lobby](docs/SSH_LOBBY.md)
- [Private Product Session API](docs/PRODUCT_SESSION_API.md)
- [World Forge](docs/WORLD_FORGE.md)
- [EnrichmentAgent](docs/ENRICHMENT_AGENT.md)
- [Agent State Contract](docs/AGENT_STATE.md)
- [State Profiles, World State, And Host Metadata](docs/STATE_PROFILES.md)
- [Testing Strategy](docs/TESTING.md)
- [Development History](docs/DEVELOPMENT_HISTORY.md)
- [Roadmap](ROADMAP.md)

## License

DREAMLAND code and associated project documentation are available under the
[MIT License](LICENSE). Published BSides Augusta presentation sources and
exports are available under [CC BY 4.0](LICENSING.md). Third-party assets retain
their original terms; see [LICENSING.md](LICENSING.md) for the complete
boundary and attribution guidance.
