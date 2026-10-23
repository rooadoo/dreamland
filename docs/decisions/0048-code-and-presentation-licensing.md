# 0048 Code And Presentation Licensing

Status: Active
Audience: maintainers, contributors, presentation collaborators, and users
Purpose: record the licensing boundary between DREAMLAND software and its
published BSides Augusta presentation materials.

## Overview

DREAMLAND includes software and project documentation as well as presentation
materials created for BSides Augusta. These works have different reuse needs:
the project should support broad software use and contribution, while the
presentation should support sharing and adaptation with attribution.

## Decision

DREAMLAND source code and associated project documentation are licensed under
the MIT License. The copyright notice names "DREAMLAND contributors" and uses
2026 as the initial copyright year. Python package metadata uses the SPDX
license expression `MIT` and includes the root `LICENSE` file in distribution
metadata.

Published BSides Augusta presentation sources and exports are licensed under
the Creative Commons Attribution 4.0 International License (CC BY 4.0). Their
attribution party is "DREAMLAND contributors." Reusers must provide
attribution, identify and link to CC BY 4.0 where practical, and indicate
whether they made changes.

Third-party assets are outside these grants unless an asset or nearby notice
explicitly states otherwise. Their original copyright and license terms remain
in force. The project must not imply that MIT or CC BY 4.0 relicenses a
third-party work.

The root `LICENSE` contains the MIT terms for the software distribution.
`LICENSING.md` explains the complete boundary, presentation attribution, and
third-party exclusion. Presentation-specific notices may repeat the CC BY 4.0
grant but must remain consistent with that guide.

## Alternatives Considered

Applying one license to every repository artifact was rejected because a
software license is not the clearest grant for presentation reuse and a
Creative Commons license is not recommended for software. Leaving the project
without explicit grants was rejected because copyright alone does not give
users the intended rights to use, modify, or redistribute the work.

Treating included third-party assets as covered by the project licenses was
rejected because DREAMLAND contributors cannot grant rights they do not hold.

## Consequences

Users may use, modify, and distribute DREAMLAND software under the permissive
MIT terms. Presentation audiences may share and adapt the published BSides
Augusta materials under CC BY 4.0 with attribution. Maintainers must preserve
the relevant notices and identify any third-party asset terms before
publication or distribution.

This decision does not select a security-reporting process, contribution
policy, code of conduct, or support commitment. Those policies require
separate decisions before corresponding files are added.

## Maintenance

Update this decision, `LICENSE`, `LICENSING.md`, `README.md`, package metadata,
and presentation notices together when the copyright party, year, license,
presentation scope, attribution guidance, or third-party asset policy changes.
