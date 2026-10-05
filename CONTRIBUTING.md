# Contributing

Thanks for your interest! This is a learning-focused personal project (see
[PLAN.md](PLAN.md)), so the bar is: small, tested, and easy to review.

## Before you start

- For anything bigger than a typo or small fix, **open an issue first** so we can agree
  on the approach.
- Security problems: see [SECURITY.md](SECURITY.md) — don't open a public issue.

## Setup

Requirements: Python 3.14, Docker + Docker Compose, optionally [Ollama](https://ollama.com).

```bash
git clone https://github.com/AndrewTtofi/llm-gateway && cd llm-gateway
python3.14 -m venv .venv && source .venv/bin/activate
make install                  # dev deps from the hashed lockfile
cp .env.example .env          # provider keys are optional for development
make up                       # full stack (migrates the database on start)
make key name=me              # a gateway API key for local requests
make test && make lint        # unit tests use in-memory stores; no stack needed
```

## Rules of the codebase

These come from [CLAUDE.md](CLAUDE.md) and are enforced in review:

- **No model names, provider URLs or prices in `app/`.** They live in `config/`.
- Every provider adapter implements `app/providers/base.py::ProviderAdapter`; the
  internal format is OpenAI chat-completions.
- Async on the request path, no blocking calls.
- Never log or return API keys, auth headers or full prompts.
- Errors to clients use OpenAI's shape: `{"error": {"message", "type", "code"}}`.
- **New behaviour needs a test.** Mock providers (`respx` for httpx,
  `httpx2.MockTransport` for the Anthropic SDK). Tests that call paid APIs are marked
  `@pytest.mark.live` and never run in CI.
- Non-obvious decisions get an ADR in `docs/decisions/` (copy `0000-template.md`).

## Dependencies

Edit `requirements.in` / `requirements-dev.in`, then `make lock` to regenerate the
pinned, hashed `requirements*.txt`. Commit both. CI fails if they're out of sync.

## Pull requests

1. Branch from `main` (`feat/…`, `fix/…`, `docs/…`).
2. Use [Conventional Commits](https://www.conventionalcommits.org/):
   `feat(router): add circuit breaker`, `fix(stream): …`, `docs: …`.
3. `make test` and `make lint` must pass; CI runs both.
4. Add an entry under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md).
5. Describe *why*, not just what. If the change touches streaming, provider formats or
   token accounting, explain the non-obvious part — that's the point of the project.

`main` is protected: changes land only through pull requests that pass CI and are
reviewed by the maintainer. PRs are squash-merged.

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
