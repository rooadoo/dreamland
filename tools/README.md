# Developer Tools Review

Status: Active
Audience: maintainers, API client authors, and operators
Purpose: identify reusable tools and support tied to the separate dreamland-ui project.

## Overview

This review covers every top-level Python tool, the Inspect AI harness, and both
World Access client smoke harnesses. Backend interfaces remain useful without
releasing the UI application. The current source cleanup removes superseded
contract snapshots and the pinned UI review kit while preserving the runtime
and reusable tool implementations.

## Details

### Tool dispositions

| Tool | Disposition | Why |
| --- | --- | --- |
| [emulation_json_annotations.py](emulation_json_annotations.py) | Keep | Generic offline JSON classification and trace annotation; no particular UI dependency. |
| [product_attendee_contract.py](product_attendee_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_attendee_live.py](product_attendee_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_attendee_live_contract.py](product_attendee_live_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_attendee_preview.py](product_attendee_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_attendee_security_contract.py](product_attendee_security_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_attendee_security_deployment.py](product_attendee_security_deployment.py) | Keep | Supervised backend API entry point with deadline and provider limits. |
| [product_attendee_security_deployment_canary.py](product_attendee_security_deployment_canary.py) | Keep | Backend deployment, isolation, cutoff, and recovery validation. |
| [product_attendee_security_deployment_contract.py](product_attendee_security_deployment_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_attendee_security_deployment_rehearsal.py](product_attendee_security_deployment_rehearsal.py) | Keep | Backend deployment, isolation, cutoff, and recovery validation. |
| [product_attendee_security_live.py](product_attendee_security_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_attendee_security_live_contract.py](product_attendee_security_live_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_attendee_security_live_identity_contract.py](product_attendee_security_live_identity_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_attendee_security_preview.py](product_attendee_security_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_contract.py](product_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_core_preview.py](product_core_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_forge_contract.py](product_forge_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_forge_live.py](product_forge_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_forge_preview.py](product_forge_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_forge_stage_contract.py](product_forge_stage_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_forge_stage_live.py](product_forge_stage_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_launch_selection_contract.py](product_launch_selection_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_launch_selection_live.py](product_launch_selection_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_launch_selection_live_contract.py](product_launch_selection_live_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_launch_selection_preview.py](product_launch_selection_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_lifecycle_contract.py](product_lifecycle_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_lifecycle_preview.py](product_lifecycle_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_session_contract.py](product_session_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_stage_contract.py](product_stage_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_stage_live.py](product_stage_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_stage_live_contract.py](product_stage_live_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_stage_preview.py](product_stage_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_world_access_contract.py](product_world_access_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_world_access_live.py](product_world_access_live.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_world_access_live_contract.py](product_world_access_live_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [product_world_access_preview.py](product_world_access_preview.py) | Keep | Explicit backend preview/live API target or shared transport used by later targets. |
| [product_xray_contract.py](product_xray_contract.py) | Keep | Backend schema/client generator or a shared base imported by later generators. |
| [ssh_lobby_presentation_preview.py](ssh_lobby_presentation_preview.py) | Keep | Provider-free SSH/terminal checkpoint; useful for testing the released SSH lobby. |
| [inspect_ai/](inspect_ai/README.md) | Keep | Reusable scenario evaluation through the client MCP interface. |
| [world_access_client_smoke/](world_access_client_smoke/run-smoke.mjs) | Keep | Compiles and exercises the provider-free generated TypeScript API client independently of any browser UI. |
| [world_access_live_client_smoke/](world_access_live_client_smoke/run-smoke.mjs) | Keep | Checks the live generated-client identity and authority boundary independently of any browser UI. |

### Generator dependencies

A generator that names an older contract version may still be a shared source
module. For example, `product_launch_selection_contract.py` loads the X-ray
generator, which loads the Forge/Stage generator, Stage generators, and session
core. The current security-deployment generator also loads the earlier live
security generator. Removing an obsolete generated snapshot does not make its
source builder removable. Existing manifest publication inputs also identify
these generator files.

### UI-specific support

The UI-specific deployment review kit, its pinned validator, dedicated kit
tests, handoff record, and acknowledgement document have been retired. Their
useful semantic response and probe cases now live in
[deployment companion tests](../tests/test_deployment_companion.py) and
[companion client tests](../tests/test_deployment_companion_client.py).

The canonical `contracts/backend-deployment/v0.1.0-rc.1` contract, packaged
artifacts, role credentials, metadata HTTP service, and companion client remain
functional backend interfaces. Their bytes, wire identities, and readiness
limits are unchanged. Historical UI-review metadata in the canonical manifest
does not require distributing a particular UI with the source release.

The phase-specific preview/live launchers could later share a smaller number of
entry points, but they own HTTP routing, authentication, events, launch handoff,
and provider/process lifecycle. That requires a separately reviewed refactor
with equivalent protocol and security coverage. Treating those files as UI-only
and deleting them would remove working API entry points.

### Running checks

Use the commands in each retained contract manifest and the owning tests in
[Testing](../docs/TESTING.md). Generated Python client checks run without the UI.
World Access TypeScript client checks require Node.js 20 or newer and:

```sh
npm ci --prefix tools/world_access_client_smoke
```

Live targets require explicit startup and provider credentials. Merely reviewing
or importing a tool does not authorize live provider calls or deployment.

## Maintenance

Update this review when retiring or consolidating a target. Check direct imports,
file-based dynamic imports, generated-client checks, manifest publication inputs,
and HTTP/API consumers before deleting a tool. Keep
[the contract inventory](../contracts/README.md) aligned.
