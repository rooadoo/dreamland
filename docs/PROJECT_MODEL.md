# Project Model

Status: Active
Audience: maintainers, coding agents, DREAMLAND operators
Purpose: record the larger product model that should guide implementation.

## Overview

DREAMLAND is the basis for an LLM-emulated computing environment and network.
The current SSH server is only the first access path into that environment. The
intended experience is a coherent, immersive world where users interact with
simulated hosts, users, files, services, programs, logs, histories, and network
relationships as if they were part of a carefully crafted real system.
DREAMLAND may represent any operator-defined computing context. Illustrative
settings include enterprise, home or small-office, research or scientific,
industrial or operational-technology, embedded, historical, fictional,
speculative, deliberately ambitious, and even absurd environments. These
examples are not a closed taxonomy. Enterprise scale and conventions are one
scenario family, not a default assumption for all generated systems.

The operator guides the world. LLMs provide creative simulation and role play,
while DREAMLAND supplies structured state, boundaries, observability, and
configuration so the illusion remains coherent within a session and across
state that an operator explicitly saves for later use.

## Product Intent

DREAMLAND should make a user believe they are operating inside a real computing
environment, while still being transparent to maintainers and operators that the
environment is generated and controlled.

Product quality has an explicit priority order:

1. Build a deep, rich, engaging world.
2. Preserve internal coherence and continuity as that world accumulates.
3. Keep the result believable and plausible in its real-world context.
4. Pursue exact factual realism where doing so does not flatten the experience.

These priorities do not permit contradiction of established facts or operator
constraints. They govern the ordinary underspecified space between those
boundaries: when several believable details fit, DREAMLAND should choose and
persist a coherent realization instead of withholding detail merely because no
single answer is uniquely provable. Safe application governs mutation authority,
conflict handling, and preservation of newer state; it is not a proof-only rule
for creative world building.

Default profiles should favor physical systems when infrastructure details are
underspecified. Virtual machines and cloud/container environments remain valid
operator-configured scenarios, but the default DREAMLAND experience should use
its generative freedom to simulate physical workstations, servers, appliances,
peripherals, and networks that are harder to stage with ordinary VM tooling.

The project currently has research status. Plausible applications include:

- training and scenario practice;
- information security exercises and simulation;
- entertainment and interactive fiction built from computing interfaces;
- research into believable LLM-driven systems.

## Current Runtime Model

The current implementation exposes one simulated host through SSH. A real
Paramiko transport handles authentication, channels, shell sessions, exec
requests, and terminal bytes. Commands do not run on a host shell. They become
structured command contexts sent to local built-ins or a LangGraph-backed LLM
agent.

The simulated environment is described by a required state profile. The
profile's `world_state` defines a static `entry_host`, an initial mutable
`active_host`, and a top-level host registry where the starting system is just
one host among other systems discovered or generated during the simulation.
Each session starts from that state profile so generated artifacts, host
metadata, and established environment facts can evolve in runtime state without
mutating the packaged baseline. Mutable world-state collections use keyed
objects so the model and future injector agents can commit to named services,
hosts, files, and facts without replacing unrelated entries. The active shell
state is tracked through session state, shell frames, shell environment
variables, interactive programs, command history, connection metadata, and
world state. Model output is parsed through a structured result envelope so
terminal output and state changes are separate.

Runtime world state is currently isolated and temporal per session. A session
does not silently share model-generated state with another session, and
background agents do not mutate the source profile. The session-local
EnrichmentAgent is a required part of the runtime: automatic scheduling is
enabled by default, and its default `safe` apply mode permits only validated,
bounded, additive host-local patches. Operators may deliberately disable it or
select `shadow` mode for controlled comparison and tuning, but those are
explicit overrides rather than the normal product posture. An explicit
state-profile save is the current mechanism for carrying selected state into a
later session. A deliberately shared persistent world remains a separate future
feature with its own identity, lifecycle, visibility, and conflict requirements.

## Future Runtime Model

Future DREAMLAND networks should support dynamically configurable, deeply
immersive environments rather than a single toy network. The long-range model
includes:

- multiple hosts with stable identities, services, users, files, and trust
  relationships;
- configurable world and scenario seeds supplied by the operator;
- stateful programs and protocol entry points beyond SSH;
- generated but internally consistent logs, inventories, services, and network
  observations;
- operator observability for model calls, state transitions, failures,
  interruptions, and token/resource usage;
- optional tracing integrations that remain easy to disable for distribution.

## Design Principles

- Believability matters. Outputs should feel like ordinary system behavior, not
  explanations of a simulation.
- State must be explicit where consistency matters. The LLM can invent details,
  but DREAMLAND should own session state, stack transitions, world-state
  updates, metadata, and durable scenario boundaries.
- World building should be decisive and additive. When several plausible
  details fit, the model should choose one, record it, and continue building the
  environment around that choice instead of repeatedly surfacing uncertainty.
- Per-call and per-agent limits should control latency, cost, and safe mutation,
  not become a global ceiling on world depth. Repeated just-in-time work may add
  further coherent layers as the user approaches them.
- A scenario normally defines the starting region of a larger connected world.
  World building should establish external networks with further onward
  connections and an eventual usable path to the simulated Internet or a
  similarly vast setting-appropriate network. Exploration should continue
  beyond the initial seed, with few network-level dead ends. Explicit operator
  isolation and established access constraints remain authoritative.
- Fictional network identities should distinguish organizations. Give private
  networks distinct private address allocations, draw fictional public IPv4
  addresses from the approved simulation pool, and use `.bsides` for invented
  domains. Persist ownership and allocation choices as world facts so continued
  exploration preserves them. As new systems become observable, retain who
  operates them, what they do, and why they connect to their neighbors; their
  operational evidence should express those identities. See the addressing
  policy in
  [State Profiles](STATE_PROFILES.md#fictional-network-addressing).
- Physical-world scenarios should place fictional facilities and organizations
  in real geography. Persist site locations and host/site relationships, and
  let geography inform ordinary operational evidence as exploration continues.
  Headquarters, service territory, and hosting location may differ. Explicit
  operator geography and deliberately fictional settings remain authoritative.
- Realism should reflect accumulated causes rather than generic clutter. Infer
  operational texture from each system's purpose, lifecycle, capabilities,
  activity, and world context, then materialize coherent evidence of people,
  tools, maintenance, automation, failures, and change only where it fits.
- Configuration should guide creativity. Device profiles and future network
  profiles should constrain the world without scripting every response.
- Observability is part of the product. Operators need to inspect prompts,
  model output, state deltas, token usage, failures, interruptions, and traces.
- Entry points are replaceable. SSH is current infrastructure, not the product
  boundary.

## Maintenance

Update this document whenever the product direction, simulation scope, or
operator model changes. New feature docs should refer back to this model and
state whether they implement current SSH-host behavior or the broader
LLM-emulated network direction.
