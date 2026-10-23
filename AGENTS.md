# Repository Instructions

## Project Purpose

- Treat `docs/PROJECT_MODEL.md` as the authoritative product direction.
  DREAMLAND is an LLM-emulated computing network; SSH is only the current entry
  point.
- Before starting a new feature, take a moment to read the project and
  understand its purpose and design.

## GitHub Coordination

- The remote GitHub repository is the coordination point between agents.
- The `main` branch is currently an acceptable common location for syncing
  project updates.
- Do not push changes until they are tested and working.
- All changes and coordination must take place through the human or through
  commits merged to `main` in the GitHub repo.
- Develop new features within feature branches.
- Branch new feature branches from `main` unless explicitly overridden.
- Do not create or use feature worktrees under `/tmp` or other temporary
  locations. If a separate worktree is needed to protect dirty work, discuss it
  first and use an agreed durable path that the user can run and inspect.
- When the user indicates a feature is done, merge it into `main`.
- Before starting new work from `main`, check for unmerged branches with
  `git branch --no-merged main`. If any branch name or recent commit suggests a
  bugfix or completed feature that may affect the requested work, inspect it and
  call it out before proceeding.
- Do not assume a newer `main` contains every prior fix. Verify ancestry with
  commands such as `git branch --contains COMMIT` or `git merge-base
  --is-ancestor COMMIT main` when a previous fix is relevant.
- If you discover an unmerged fix branch while working, either incorporate the
  fix deliberately in the current branch or ask whether the branch should be
  merged first. Avoid reintroducing behavior that was already fixed elsewhere.

## Feature Development

- When starting a new feature, do not start edits until you discuss your plan
  and get approval to proceed.
- When developing a new feature, prefer a careful phased approach with manually
  tested checkpoints along the way. When manual testing is practical, provide a
  short series of steps to check each phase or checkpoint.
- Do not use `...` in place of `pass`.
- As features are added, continue logging critical lifecycle, command,
  model-call, failure, interruption, and resource-usage events. Keep logging
  levels and event types configurable.
- Prefer tests at the owning layer. Use real SSH/Paramiko tests only when the
  transport behavior is what is being validated.

## Documentation

- Follow `docs/DOCUMENTATION.md` when adding or updating documentation. Durable
  docs under `docs/` should use the documented status, audience, purpose,
  overview, details, and maintenance schema unless there is a clear reason not
  to.
- Update docs in the same change as behavior, configuration, prompt, device
  profile, operational, or test-policy changes. Keep `README.md` concise and
  move detailed references into `docs/`.
- Before committing a feature or meaningful behavior change, review the
  decisions made since the previous commit and record durable architecture,
  product, prompt, configuration, operational, or process decisions under
  `docs/decisions/` when they explain why the change exists. When practical,
  cite the related implementation commit or commits in the decision record so
  future maintainers can inspect the code changes tied to the decision.
- Keep scenario facts in device profiles or future network configuration, not
  in reusable behavior prompts. Keep behavior and state-contract instructions in
  prompt modules and runtime docs.
