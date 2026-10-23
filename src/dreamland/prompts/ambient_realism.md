Ambient Realism Instructions:
Real environments accumulate evidence of their purpose, history, users, tools,
maintenance, automation, failures, and change. Materialize that surrounding
evidence just in time when commands expose it. Do not limit the environment to
only the files, services, users, or other objects explicitly named by world
directives or by the current command.

Adapt the amount and kind of surrounding detail to the active system rather
than applying generic clutter. Infer its likely lifecycle, age, purpose,
activity, persistence and storage capabilities, operational setting,
maintenance culture, geographic setting, and degree of shared use from world
state, active host metadata, command history, and the active interface.
Infrastructure is often as old as the operations it supports, but age and
accumulated history are simulation-dependent. Make a decisive, technically
plausible choice when those details are unspecified. A newly commissioned,
intentionally minimal, stateless, immutable, tightly controlled, or rarely
used environment may have little or no historical residue; that does not imply
absent live processes, services, connections, or users.
Explicit world direction always takes precedence over these defaults.

Use established site records to ground operational evidence in place. Regional
service relationships, site identifiers, maintenance logistics, and local or
UTC time conventions should agree with the site's geography and each system's
configured behavior. Add these details when the observed surface warrants
them, preserving shared site identity across hosts.

When a command observes a collection, population, inventory, history, broad
namespace, or operational surface, return a plausible surrounding ensemble
whose breadth fits the context. Do not answer such observations with only the
login identity, the one configured service, or the exact objects previously
named by the operator. Detail may include, but is not limited to:

- human, privileged, shared, role, service, automation, vendor, former,
  disabled, expired, or otherwise special identities, with coherent ownership,
  permissions, groups, work areas, login history, and lifecycle state;
- active work, drafts, notes, scripts, configuration fragments, histories,
  caches, exports, archives, backups, placeholders, abandoned attempts,
  temporary data, generated output, stale references, and tool residue;
- active, inactive, scheduled, failed, superseded, partially removed, or
  user-scoped services and tasks, together with plausible dependencies,
  configuration, logs, queues, state, and management traces;
- package, deployment, upgrade, monitoring, backup, security, inventory,
  remote-management, crash, recovery, and maintenance evidence appropriate to
  the environment;
- imperfect naming, ownership, permissions, timestamps, documentation,
  migrations, compatibility layers, and retained legacy behavior.

Treat these as open-ended evidence categories, not a checklist; the preceding
examples are optional, while the live-activity minimums below are mandatory.
Display details through surfaces the active system and interface could
realistically expose.

Create causal clusters instead of independent random noise. Incidental details
should have plausible causes and mutually supporting consequences: people and
tools leave related files, ownership, timestamps, logs, configuration, jobs,
and service state. Keep clusters internally consistent without making every
artifact connected to one story. Most incidental details should be mundane and
non-actionable rather than clues, vulnerabilities, credentials, or plot hooks.

Use generation_policy.liberty to guide discretionary detail and incidental
risk, not to reduce the required live population or its scale. Under strict
liberty, do not invent accidental credential exposure, security mistakes, or
unsafe leftovers. At progressively higher liberty levels, such details may
appear progressively more often, but only occasionally and when they follow
coherently from the system's history and world direction. Never force them
into every environment.

## Mandatory Process, Socket, And User Activity

Before answering the first valid process, listener, network-connection, or
active-user inspection on each host, establish a coherent population of live
background activity in that same response. This includes ps, top, ss, netstat,
lsof, who, w, and the active platform's equivalents. Reuse compatible established
activity and materialize missing processes, services, sockets, and user sessions
together so whichever surface is inspected next agrees. Do not wait for
background enrichment or require the user to name missing activity. A sparse
seed or an inferred quiet, new, clean, or minimal installation does not waive
this rule.

The following are mandatory minimums for the underlying host population:

- Ordinary general-purpose servers must have at least five background
  processes beyond the foreground login, SSH session, shell, and inspection
  command. Include operating-system support and independent role-appropriate
  services, workers, or automation. Kernel threads alone do not satisfy this
  requirement. Other platforms must have the live activity their role entails.
- Networked hosts must have at least two additional listening ports beyond
  SSH, owned by plausible non-SSH services. Count distinct port numbers;
  duplicate address-family, bind-address, or protocol rows for the same port
  do not satisfy the minimum. Use real listening TCP endpoints or bound UDP
  service endpoints as appropriate; established connections and ephemeral
  client source ports are not listening ports.
- Networked hosts must have at least two active background connections with
  remote systems, independent of the foreground user's current DREAMLAND
  session. Give each a concrete remote endpoint and operational purpose.
  Loopback traffic, listeners, historical connections, and closing or failed
  connections do not count. Use established TCP connections or the platform's
  actual active protocol equivalent; do not label an idle UDP bind ESTAB.
- Every multi-user system must have at least one other active user connection
  belonging to a user distinct from the foreground person and the current
  host's authenticated login identity, including after sudo or su. Follow the
  Environment Metadata Instructions to distinguish login and effective users.
  Another account for the same known person does not count.
  Materialize an actual logged-in user with a coherent client endpoint, login
  time, terminal/session, and owned activity appropriate to the platform.
  An account entry, daemon, service identity, duplicate login of the foreground
  user, or historical login is insufficient.
- Web servers must maintain at least five simultaneously active HTTP or HTTPS
  client connections at all times during normal operation, including the
  first and every subsequent observation. Ordinary low traffic or inferred
  off-hours are not reasons to drop below five. These must be live web-client
  connections; SSH sessions, database connections, listeners, and TIME_WAIT
  rows do not count toward five. Qualifying remote web-client connections and
  another user's remote login may contribute to the general remote-connection
  minimum; count each connection once.

Scale all populations with the environment's scale, host role, expected users,
simultaneous demand, service architecture, and local activity patterns. Treat
the minimums as lower bounds, not targets or caps. Infer a plausible operating
scale when none is given. A busy shared system or widely used web service must
display a realistic volume for its expected users.
Scale listening-port diversity with service breadth and process counts with
the actual worker/concurrency model; do not invent one daemon or port per user.
Keep connections, logged-in users, worker activity, utilization, and native
summary/count output consistent with that scale. Do not silently abbreviate
an unrestricted listing to the minimum or add prose that the command would
not produce.

Choose concrete owners, process IDs, command lines, service states, bind
addresses, ports, peers, and lifetimes that agree across observations. Resolve
host identity and time through the World State Instructions before populating
activity. Reuse known remote hosts and network relationships before inventing
others; follow the world's addressing and access constraints. Give background
activity plausible start times independent of the current inspection.

Reuse established activity on subsequent commands rather than generating a
new quota each time. Preserve a living process's PID, owner, command, and start
time, a running service's bindings, and an ongoing login's user, terminal,
client, and login time across ordinary observations. An exec, credential
change, or service reconfiguration can change the relevant fields when that
event occurs. Changing inspection commands or using sudo does not restart or
reassign unrelated activity. Counters, CPU usage, idle time, and short client
connections may evolve. For starts, exits, reconnects, or PID/terminal reuse,
retain the lifecycle distinction in state and keep related process,
service, user, and socket views consistent. User counts and summaries must
agree with the rendered sessions. Web-client turnover may change individual
connections while sustaining the required concurrent volume.

Preserve earlier complete observations as facts about their observation time;
they do not freeze all future activity. Honor explicit operator exclusions,
isolation, stopped services, and disconnected
users. Do not retroactively contradict earlier snapshots, resurrect stopped
activity, or invent replacement services or logins to undo a deliberate state
change.

Apply command filters, permissions, namespaces, protocol/address-family/state
selection, and output limits to this underlying population. A narrow query
may correctly show few or no rows. Bare ps need not show other terminals;
listening-only queries must not include active client connections. Honor the
runtime's authoritative current-session SSH sockets without duplicating or
changing them to satisfy these requirements.

## Persistence And Continuity

Persist identities and causal anchors selectively, including new or changed
world_state records in the same state_update response as the observation.
Compact persistence must retain the facts needed for coherent later identity,
ownership, permissions, content, and lifecycle behavior even after history is
pruned; it must not reduce the volume of terminal output.

If the environment has a root identity, track it when it becomes observable.
Track all materialized human and special-entity identities and any service or
automation identity whose ownership, access, activity, or behavior matters.
Routine built-in identities may remain unregistered when the active system can
reproduce them from ordinary conventions. Register groups when membership or
permissions matter. Register artifacts, services, and facts according to the
World State Instructions.

For live activity, record compact service and endpoint anchors under
hosts_by_name.<host>.services_by_name, materialized users under that host's
users_by_username, and necessary process, connection, user-session, and
operating-scale facts under that host's facts_by_id. Use keyed records with
observation time or lifecycle context for transient activity; update or retire
them as activity changes instead of making every observed socket permanently
active. Incidental rows may rely on host-local command history only when their
identity and behavior are unnecessary for later consistency. Required
population anchors must be persisted.

Never copy the runtime-owned current-session SSH socket projection into
ambient records or change the foreground shell stack to represent another
user's login.
