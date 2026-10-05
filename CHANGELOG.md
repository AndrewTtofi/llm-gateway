# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) ·
Versioning: [SemVer](https://semver.org/spec/v2.0.0.html).

Version plan: each completed phase bumps the minor version
(Phase 1 → 0.1.0, Phase 2 → 0.2.0 … Phase 7 → 1.0.0).

## [Unreleased]

### Added
- Project scaffold: build plan, CLAUDE.md, SESSION.md, Claude Code agents and skills
- Docker Compose stack: gateway, Redis, Postgres, Prometheus, Grafana
- Config-driven model registry (`config/models.yaml`), pricing and limits files
- `/healthz` endpoint and config loader skeleton
- CI workflow (lint + tests)
- API tests for `/healthz`, `/v1/models`, `/admin/reload`

### Changed
- Errors use OpenAI's `{"error": {...}}` shape
- CI lint step also runs mypy

### Security
- `/admin/reload` compares the admin key in constant time

<!--
Sections to use under each version:
### Added      new features
### Changed    changes to existing behaviour
### Deprecated soon-to-be removed
### Removed
### Fixed      bug fixes
### Security   vulnerabilities

## [0.1.0] - YYYY-MM-DD  (Phase 1 — pass-through proxy)
-->
