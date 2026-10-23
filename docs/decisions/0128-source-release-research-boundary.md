# Source Release Research Boundary

Status: Active
Audience: maintainers, contributors, and coding agents
Purpose: separate published research evidence from the software source release.

## Overview

Research evidence is published separately from DREAMLAND source. The source
release carries the runtime, examples, development tools, and regression tests.

## Details

Exclude study datasets, captured traces and observations, reports, grading
results, historical experiment snapshots, presentation output, and scripts
dedicated to collecting, parsing, grading, or presenting those studies.

Retain reusable tools with legitimate generic use, including the Inspect AI
harness and offline JSON classification and annotation utilities. Preserve
their tests and usage documentation, along with runtime logging, tracing,
structured-failure capture, and MCP model controls.

Bundled starter worlds remain runtime examples. Their distribution metadata
records file and world-state integrity hashes without research scores or links
to removed study archives. Clear research source-session identifiers from the
distributed examples while preserving their simulation content. Keep integrity
hashes aligned with the distributed bytes and leave operator-owned worlds intact.

## Maintenance

Apply this boundary when adding tools, examples, documentation, or release
assets. Keep research publications separate without removing generic runtime
or developer functionality solely because a study used it.
