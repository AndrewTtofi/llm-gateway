# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately via
[GitHub's private vulnerability reporting](https://github.com/AndrewTtofi/llm-gateway/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You'll get an
acknowledgement within a few days. This is a personal project, so there's no bounty.

## Scope

In scope: the gateway code in `app/`, its configuration handling, and the Docker setup.
Things like leaking provider API keys or prompt content, bypassing auth or rate limits
(once those land in Phase 4), or making the gateway call arbitrary URLs.

## Handling secrets

- Provider keys live only in `.env` (git-ignored) and are read from environment variables.
- The gateway never returns provider error text that could contain key fragments or
  account IDs to clients (see `docs/decisions/0002-streaming-errors-and-disconnects.md`).
- CI uses no secrets.
