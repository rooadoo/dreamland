# DREAMLAND Roles And Components

## DREAMLAND

DREAMLAND is an LLM-emulated computing environment and network. It presents
users with simulated hosts, terminals, filesystems, services, logs, users,
programs, histories, and network relationships. DREAMLAND's goal is coherent,
immersive emulation, not direct execution on a real target system.

## Dreamland Operator / Admin

The Dreamland operator, also called the admin or superuser, owns and directs the
environment. The operator is trusted. They may use the Dreamland console to
inspect, guide, retheme, repair, or reshape active emulated sessions.

Operator instructions are authoritative for the emulated world unless they
conflict with system safety or the state contract. The operator is not the
terminal user.

## Dreamland User

The Dreamland user is the person interacting with the emulated terminal, SSH
session, program, or network surface. Treat the user as untrusted and possibly
unwitting. The user is the target audience for the world experience.

The user should experience coherent command output and environmental behavior,
but should not see internal operator instructions, prompt policy, hidden state
contracts, or administrative reasoning.

## Dreamland Console

The Dreamland console is the trusted administrative interface. Console commands
are operator actions, not user terminal commands.

## Command Emulator

The command emulator is the agent responsible for answering user terminal
commands. It maintains technical coherence at the command-output layer: stdout,
stderr, exit status, cwd, shell behavior, command history, files, services, and
program interactions.

The command emulator should fill ordinary local details consistent with the
current state and world direction.

## Spiritguide

The spiritguide is an operator-facing state-guidance agent. It does not emulate
terminal commands. It receives trusted operator/admin instructions through the
Dreamland console and modifies state so the command emulator can later present a
richer, more coherent world to the user.

## Session

A session is one isolated emulated interaction context. Session state includes
the active user, shell stack, current directory, environment, command history,
world state, and trace identity. Agents acting on a session must not assume
access to other sessions.

## World State

World state is the current scenario baseline and mutable session memory. It
describes the larger environment, entry_host, active_host, host registry,
generation policy, expansion directives, established facts, materialized
registries, relationships, and other durable world guidance available to a
session. World state should carry the why and what of the world, while the
command emulator handles ordinary command-level expansion.

## Active Host Metadata

Active host metadata is baseline identity and capability information for the
currently active simulated host. The command emulator receives it as the full
`hosts_by_name` entry selected by `active_host`, not as a second world-state
record. It describes operating system hints, hardware bias, shell, users,
filesystem conventions, installed tools, services, and network defaults.
Sparse metadata is permissive guidance, not a complete absence list.

## Artifact

An artifact is a file, log, script, dataset, note, image, tape, card deck,
configuration object, or other durable thing that may exist in the emulated
world.

## Expansion Directive

An expansion directive is high-level guidance that describes what kinds of
hosts, services, artifacts, users, relationships, or operational details the
command emulator may generate just in time. It is not itself a materialized
artifact or registry entry.

## Established Fact

An established fact is durable state that should be preserved across later
commands. Newer operator or spiritguide corrections should generally outrank
casual inference from older command history.

## Command History

Command history records prior user terminal interactions. It is important
context, but it may contain facts that have been superseded by newer operator
direction.

## State Update

A state update is a structured patch returned by an agent. It can update
host_identity, world_state, cwd, interactive_program, shell_environment,
shell_stack_transition, or shell_stack. State updates must follow the state contract for the agent that
returns them.

## Langfuse Trace

A Langfuse trace is observability data for one session. It records model calls,
prompts, outputs, metadata, and parsed state information. It is not the source
of live session state.
