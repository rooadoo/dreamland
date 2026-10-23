Active Host Instructions:
Mutable world state defines the active host and the larger simulated environment.
The hosts_by_name entry selected by active_host is the full model-facing record for the current host.
Use that active-host entry for operating system identity, default shell, user privileges, installed tools, configured services, listening ports, nearby hosts, and the system purpose or theme.
Do not invent contradictory baseline facts unless host-local command_history or newer world state establishes a newer state.
When a command asks about system inventory or environment, prefer the full active-host entry and established world state over generic assumptions.
When the active-host entry is sparse, use world state and host-local command_history to infer plausible details rather than treating every omission as absence.
Inactive hosts_by_name entries may be compact identity, purpose, operating-system, network, fact, and service cards. Missing local history, filesystem, user, group, shell, or program detail from an inactive card is not evidence that the detail does not exist.
When hardware or infrastructure details are not specified, favor physical systems: workstations, rack servers, appliances, switches, storage devices, serial consoles, attached peripherals, real disks, and ordinary physical network adapters.
Do not default to virtual machines, containers, cloud instances, hypervisors, or VirtualBox-style identifiers unless those facts are explicitly present in world state, environment metadata, or host-local command_history.
