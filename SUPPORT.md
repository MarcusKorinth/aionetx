# Getting Support

Thanks for using aionetx. This document describes where to direct each type
of request so you reach the right channel quickly.

## Bugs

Open a GitHub issue:
<https://github.com/MarcusKorinth/aionetx/issues>

Please include:

- aionetx version (`pip show aionetx`)
- Python version and OS
- the event-loop policy if non-default (for example `WindowsSelectorEventLoopPolicy`)
- a minimal reproducer, expected behavior, and actual behavior
- relevant log output at `DEBUG` level for the `aionetx` logger (see
  [`docs/logging.md`](docs/logging.md))

## Security vulnerabilities

**Do not** open a public issue for security reports. Follow the private
reporting channel in [`SECURITY.md`](SECURITY.md).

## Questions and usage help

Start a thread in GitHub Discussions Q&A:
<https://github.com/MarcusKorinth/aionetx/discussions/categories/q-a>

Before asking, please check:

- [`README.md`](README.md) - core semantics and examples
- [`docs/architecture.md`](docs/architecture.md) - lifecycle and event model
- [`docs/platform_notes.md`](docs/platform_notes.md) - platform-specific
  behavior, especially if you are on Windows
- [`docs/timing_and_latency.md`](docs/timing_and_latency.md) - what aionetx
  does and does not guarantee about timing
- [`examples/`](examples/) - runnable end-to-end scripts

## Feature requests and design discussions

Use GitHub Discussions for early design conversations and usage-shape
questions. Open a GitHub issue once you have a concrete bug report or a
specific behavior change in mind.

## Commercial support

There is no commercial support offering at this time. aionetx is maintained
on a best-effort basis under the MIT license.
