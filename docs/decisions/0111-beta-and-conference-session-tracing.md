# 0111 Beta and Conference Session Tracing

Status: Active
Audience: backend maintainers, deployment operators, project owner
Purpose: record why deployed tracing is enabled for the beta and conference
profiles, and why the runtime environment no longer forces it off.

## Overview

The expedited beta path puts the SSH backend in front of outside testers before
the Phase 2 runtimes exist. Live sessions are the only source of evidence about
real command behavior, model latency, and failure shape, and the owner collects
that evidence under the user agreement. The Phase 1 foundation deliberately
refused all tracing, so the deployed profiles that now serve real users needed
an explicit decision rather than a silent relaxation.

Beta and conference both serve real users and both collect session traces.
Prerelease keeps tracing disabled: it predates this data-collection decision,
its evidence was private operator rehearsal, and it is not expected to be used
again. See the [beta deployment plan](../BETA_SSH_DEPLOYMENT_PLAN.md).

## Details

`LANGFUSE_ENABLED_PROFILES` in
[configuration.py](../../src/dreamland/deployment/configuration.py) is the
single authority for which profiles may trace. Initialization writes
`[langfuse] enabled` from that set, `configure-ssh` prompts for the credentials
only for those profiles, and validation reports a profile-specific stage.
Enabling tracing for a profile outside that set still fails closed, and a
profile inside it must supply a complete public key, secret key, and host before
`show`, `validate`, or `serve-ssh` accept the deployment.

Deployed tracing requires a separate Langfuse project from development and
evaluation work. Traces carry attendee commands and model replies, so the
project boundary is the retention and access boundary. Credentials are stored
with the other deployment secrets and never appear in arguments or logs.

The runtime wrapper previously exported `LANGFUSE_ENABLED=false`
unconditionally. Because the settings loader prefers the environment over the
INI, that export silently discarded the deployment's own configuration: a beta
deployment would validate as traced and then run untraced. The wrapper now
exports the override only for profiles that must not trace, and omits it
otherwise so the deployment INI is authoritative. LangChain's own
`LANGCHAIN_TRACING_V2` and `LANGSMITH_TRACING` remain forced off for every
profile; they are unrelated callback paths, not this project's tracing.

`configure-ssh` validates the deployment before collecting secrets, and that
check cannot require the Langfuse credentials it exists to collect. A freshly
initialized beta or conference deployment therefore validates with the tracing
completeness gate relaxed for that command only. Every other entry point keeps
the gate, so an incomplete traced deployment cannot start SSH.

## Maintenance

Widen `LANGFUSE_ENABLED_PROFILES` only with a recorded decision and a matching
user-agreement position; adding a profile changes what is collected from real
users. Keep the [beta deployment plan](../BETA_SSH_DEPLOYMENT_PLAN.md) and
[Operations](../OPERATIONS.md) aligned when profile
tracing, credential prompts, or the runtime environment allowlist change.
