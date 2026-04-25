# Contributing to aionetx

Thanks for contributing.

## Start here

1. read `README.md` for usage and scope
2. read `docs/architecture.md` for canonical semantics and invariants
3. keep changes minimal, explicit, and reviewable

By participating in this project you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Developer Certificate of Origin

All code contributions must be made with Developer Certificate of
Origin (DCO) sign-off. Add a `Signed-off-by` trailer to every commit:

```text
Signed-off-by: Your Name <your.email@example.com>
```

The usual Git command is:

```bash
git commit -s
```

By signing off, you assert that you have the right to submit the
contribution under the project license. Pull requests with unsigned
commits are not accepted once the repository DCO check is enabled.

## Project-specific expectations

These rules are critical and must be preserved:

- preserve lifecycle and event-ordering semantics
- keep the transport boundary strict:
  - no protocol parsing, framing, or business logic in transport code
- add or update tests when behavior changes
- update documentation when user-visible behavior or guarantees change

## Development workflow

### Branching strategy

- `main` should remain integration-ready and green
- no direct commits to `main`
- keep each pull request focused on one concern
- avoid mixing refactors and behavior changes unless the coupling is real and
  unavoidable

### Branch naming

Use the following pattern:

```text
<type>/<short-description>
```

Examples:

```text
feat/tcp-reconnect-backoff
fix/duplicate-close-event
refactor/event-dispatch
docs/readme-installation
test/reconnect-behavior
chore/repo-bootstrap
```

### Working process

1. create a branch from `main`
2. implement your change
3. write clean commits
4. ensure tests pass
5. open a pull request

## Commit message policy

This project uses Conventional Commits.

### Format

```text
<type>(<scope>): <summary>

[optional body]

[optional footer]
```

### Header rules

- use imperative mood, for example `add`, not `added`
- do not end the header with a period
- maximum 72 characters
- keep it concise and descriptive
- scope is recommended but optional

Examples:

```text
feat(factory): add multicast receiver creation helper
fix(tcp): prevent duplicate connection closed event
refactor(events): split dispatch and validation logic
docs(readme): clarify lifecycle model
```

### Allowed types

```text
feat      new functionality
fix       bug fix
refactor  internal restructuring without behavior change
docs      documentation changes
test      tests
build     build system or dependencies
ci        CI/CD changes
perf      performance improvements
chore     maintenance tasks
revert    revert previous commit
```

### Commit body guidelines

The commit body should explain context, not repeat the diff.

It should answer:

- why was this change necessary?
- what was changed conceptually?

Recommended structure:

```text
Why:
- describe the problem or motivation

What:
- describe the key changes
- mention important design decisions
```

### When a body is required

A commit body is required for:

- `feat`
- `fix`
- `refactor`
- any change affecting behavior, API, lifecycle, or semantics

A commit body is optional for:

- `docs`
- small `test` changes
- trivial `chore` or `ci` updates

### Readability

- header: max 72 characters
- body: keep paragraphs readable in GitHub and terminal tools; avoid
  very long lines, but wrap where it improves clarity rather than to
  satisfy a fixed column count

### Footer

Use footers for metadata when relevant:

```text
BREAKING CHANGE: description
Refs: #123
Closes: #123
```

## Pull requests

### General rules

- pull requests must be focused on a single concern
- avoid mixing refactoring and behavior changes
- prefer small to medium-sized pull requests
- ensure the change is reviewable

### PR title

PR titles should follow Conventional Commits:

```text
<type>(<scope>): <summary>
```

### PR expectations

Before opening a PR:

- tests pass
- documentation is updated if needed
- changes are cleanly structured

A good PR should clearly explain:

- what was changed
- why it was changed
- any important side effects or constraints

## Testing

- add tests for new behavior
- add regression tests for bug fixes
- ensure the relevant test slice passes before opening a pull request
- run `ruff check .` and `mypy src` for changes that affect source code or API

## Documentation

Documentation must be updated when:

- behavior changes
- APIs change
- guarantees or invariants change

## Versioning

This project follows Semantic Versioning:

- `fix` -> patch
- `feat` -> minor
- breaking changes -> major

## Final notes

- prefer clarity over cleverness
- prefer explicit design over implicit behavior
- prefer behaviorally strong tests over incidental implementation checks

Consistency in history and structure is critical for long-term
maintainability.
