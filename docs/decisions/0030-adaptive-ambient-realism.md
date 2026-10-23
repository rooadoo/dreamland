# 0030 Adaptive Ambient Realism

Status: Active
Audience: maintainers, prompt authors, DREAMLAND operators, and coding agents
Purpose: define how just-in-time generation creates realistic accumulated
system detail without applying generic clutter everywhere.

## Overview

Just-in-time generation previously concentrated on objects directly named by a
world profile or user command. This produced technically coherent systems that
often lacked the loose ends, maintenance evidence, identity population, tool
residue, and ordinary mistakes found in long-running real environments.

Adding a fixed inventory of background artifacts would create a different
failure. The appropriate amount and kind of accumulated detail depends on the
particular system, its interface and capabilities, its lifecycle, and the
operations it supports. Some environments should expose extensive history,
some almost none, and many fall between.

## Decision

The command emulator applies adaptive ambient realism whenever commands expose
collections, populations, inventories, histories, namespaces, or operational
surfaces. It materializes a plausible surrounding ensemble instead of limiting
output to the current login identity or objects explicitly named by the world
profile. The session-local EnrichmentAgent applies the same causal-cluster and
liberty principles when performing just-in-time finishing work; decision 0034
defines its separate neutral Expert boundary and safe write contract.

This behavior is system-agnostic. The emulator infers suitable detail from the
active system's purpose, lifecycle, likely age, activity, persistence and
storage capabilities, operational setting, maintenance culture, shared use,
active interface, and established world context. No closed catalog of system
types determines whether artifacts should exist.

Ambient details form causal clusters rather than random noise. People, tools,
automation, maintenance, failures, and change can leave mutually supporting
identities, ownership, timestamps, files, configuration, jobs, services, logs,
queues, and state. Most generated details remain mundane and non-actionable;
the emulator does not turn every inconsistency into a credential, vulnerability,
or scenario hook.

World state controls the simulation-specific texture. Spiritguide writes
concise `expansion_directives_by_id` guidance about relevant dimensions such as
operational age, lifecycle, activity, persistence, maintenance and deployment
culture, shared use, identity population, automation, service churn, legacy
burden, retention, and tolerated residue. It does not pre-materialize ordinary
inventory. Hosts generated later inherit relevant world direction while the
command emulator makes a decisive host-local judgment from their purpose and
capabilities.

Precedence is:

1. Established world and host facts.
2. Explicit operator-created directives, constraints, and exclusions.
3. Active-host purpose, capabilities, lifecycle, and generation policy.
4. General ambient-realism defaults where the preceding sources leave creative
   space.

`generation_policy.liberty` bounds breadth and risk. At `strict`, the emulator
does not invent accidental credential exposure, security mistakes, or unsafe
leftovers. Progressively higher levels permit these details progressively more
often, but they remain occasional and must follow coherently from world context
and system history.

Materialized users and groups use host-local `users_by_username` and
`groups_by_name` registries. Record root when that identity exists and becomes
observable, all materialized humans and special entities, and service or
automation identities whose ownership, access, activity, or behavior matters.
Routine built-in identities can remain implicit. Existing selective rules for
files, services, hosts, and facts continue to limit state growth. Details needed
for later identity, ownership, permission, content, service, or lifecycle
consistency receive compact keyed records; incidental rows may rely on
host-local command history.

The behavior lives in the reusable emulator prompt. Scenario texture lives in
world state. Existing development state profiles receive no compatibility
adapter or migration; they can be cleaned up or regenerated during pre-release
development.

## Initial Validation And Deferred Work

An initial interactive hospital-enterprise session confirmed that the prompt
module was present in command-emulator calls and that Spiritguide supplied the
new operational-texture guidance. The emulator broadened user populations,
registered significant human, role, service, and vendor identities, and formed
some coherent operational clusters across services, processes, queues, files,
and runbooks.

The session also showed that prompt guidance alone does not complete the
feature. Generated home directories remained heavily templated, artifacts
clustered too closely around the current command or incident, timestamps lacked
the expected operational depth, and simulated shell-history files reused too
much current-session activity. Selective state-registration instructions also
appeared to make the model favor sparse terminal output even though output
richness and patch size are separate concerns.

Model-facing world state and full host-local command histories grew rapidly as
the user explored additional hosts and broad command output. Decision 0031
addresses the cross-host portion by keeping the active host complete and
presenting inactive hosts as compact model-facing cards. Active-host history
pruning and summarization remain separate work. Further ambient-realism tuning
should be evaluated against the projected context, with explicit separation
between normal system use and the current inspection interface, pre-session
environmental history and live command history, rich observations and selective
registration, and independent causal clusters not centered on the current
task.

## Consequences

Broad observations should reveal systems with plausible depth without forcing
every environment to look old, busy, writable, human-operated, or untidy.
World authors can deliberately create pristine, minimal, transient, highly
controlled, or otherwise low-residue simulations through explicit direction.

State growth remains bounded by selective registration, but materialized key
identities and causal anchors survive command-history pruning. Prompt and model
evaluations should cover both rich and sparse operational textures, explicit
world overrides, identity persistence, causal consistency, and liberty-level
risk behavior.

## Maintenance

Update this decision when ambient generation moves from prompt policy into
typed runtime logic, world-state precedence changes, identity registries become
validated, or liberty levels change meaning.
