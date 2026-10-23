# 0121 World-Owned Initial-Login Banners

Status: Active
Audience: backend maintainers, prompt authors, DREAMLAND operators
Purpose: explain saved host login presentation, model-context boundaries, and
strict state-profile compatibility after the Ubuntu/Debian entry discrepancy.

## Overview

Transport-level greetings and server-personality labels can contradict the
selected world. Initial presentation must use saved world facts so users and
model instructions receive a coherent host identity.

The world now owns initial-login presentation. Spiritguide authors a host-local
`login_banner` once when creating or repairing a world, and the initial shell
reads it directly from saved state. Host OS and shell remain world facts. The
real transport identifies itself neutrally as `SSH-2.0-DREAMLAND`.

## Details

### Authoring and runtime ownership

State-profile schema version 2 requires registered `entry_host` and initial
`active_host` references and a `login_banner` string on each corresponding host
record. An empty string deliberately requests silence. Text must be valid UTF-8
and at most 16,384 bytes; LF, CR, and tab are permitted, while other category-`Cc`
control characters are rejected. Additional supplied host banners receive the
same validation. Spiritguide must return this contract, and detached operations
validate before successful save or dry-run completion. Validation never repairs
missing presentation by assuming Ubuntu.

The effective initial host selected by system identity or an exact shell frame
must also resolve to a banner-bearing host. A session saved while exploring a
new peer therefore needs Spiritguide to author that peer's banner first;
discovery alone does not require one. Forge validator revision 2 records this
stricter contract in generated-world validation receipts.

The initialized active host supplies the text. Direct SSH interactive entry and
each new lobby world visit display it before the first prompt with no model call
and no dependency on enrichment. This first-entry display is not appended to
command history. Exec requests remain command responses. Later simulated SSH
logins, including reentry into the starting host, are generated through normal
command emulation. The saved banner does not become a reusable runtime login
handler for every host transition.

Authoritative state, checkpoints, saved profiles, and Spiritguide's context
retain the banner. Command-emulator and enrichment prompt projections omit the
field, including detailed active-host metadata. Ordinary files independently
authored in the simulated filesystem, such as `/etc/motd`, keep their normal
visibility. This confines the excluded content to the initial presentation
artifact rather than hiding inspectable host data.

The redundant `ServerProfile.name` and model-facing `profile:` identity are
removed. They did not select emulation behavior, and an OS-flavored label could
contradict the active host. Saved `StateProfile.name` remains the useful
operational scenario identity for tracing and failure records. Spiritguide does
not invent a second platform-label field.

### Compatibility and provenance

This is an intentional compatibility break. Version-1 state profiles are
rejected; the runtime does not convert them, infer a greeting, or replace local
operator data. Operators repair old JSON offline, including host references
and banners, or generate a new valid world. Updating a package or deployment
image does not rewrite mounted/operator-owned profile storage. A local version-1
copy still shadows a bundled version-2 resource until explicitly repaired or
replaced.

All four package profiles and the 57 active local profiles present in this
checkout were repaired. The local files are ignored runtime data and are not
part of the distributable Git change. `colonyship3`'s dangling entry reference
was resolved by rekeying its already-authored `bridge.aetheria` record and
aligning the active reference. `colonyship4` and `colonyship5` now begin on their
authored entry hosts rather than placeholder hosts; their other records remain
preserved. `colonyship2` retains its two valid distinct entry/active references
and receives a separate banner for each host.

Historical evaluation sources, frozen profiles, old failure capsules, and
session indexes remain original evidence. The three runnable lobby examples
are identified as schema-2 derivatives in both
[lobby provenance](../../lobby/provenance.json) and the packaged deployment
provenance. Archived source hashes stay unchanged; current local/package file
and world-state hashes record the derivatives. The only world-state additions
to these three evaluated baselines are their authored host banners. Their
published grades continue to describe the archived worlds rather than a new
banner evaluation. Default/world profile OS identities are preserved.

Removing the redundant command-context profile field also changes the complete
`agent_core.py` source fingerprint used by offline JSON annotations. A scoped
[compatibility audit](../EMULATION_JSON_ANNOTATIONS.md#world-owned-login-presentation-runtime-compatibility-audit)
verified unchanged parser, output-coercion, rejection, and result-codec behavior
before adding the exact source digest to the supported set. Historical hashes
and JSON classification policy remain unchanged.

### Validation

Automated checks belong at the profile, Spiritguide, context-projection, shell,
lobby, and tracing/failure owning layers. Real SSH tests are reserved for
protocol identification and transport behavior. The manual terminal/model
checkpoints are in [Testing](../TESTING.md#initial-login-banner-checkpoints).
The [state-profile reference](../STATE_PROFILES.md#saved-initial-login-banners)
documents offline repair and deployment limitations. Live terminal and model
results must be recorded separately from deterministic regression results.

## Maintenance

Keep this decision aligned with `login_banner.py`, `state_profiles.py`,
`spiritguide_profiles.py`, `prompts/spiritguide.md`, `ssh_sessions.py`, command
and enrichment context projection, and tracing identity. Update the contract,
provenance, and operator repair guidance together if saved presentation or
profile schema changes. Preserve archived evaluation artifacts when preparing
new runnable derivatives.
