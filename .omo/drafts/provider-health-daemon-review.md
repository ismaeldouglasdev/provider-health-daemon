---
slug: provider-health-daemon-review
status: drafting
intent: clear
pending-action: write .omo/plans/provider-health-daemon-review.md
approach: Comprehensive code review of provider-health-daemon to identify bugs, UI/UX improvements, usability enhancements, and new feature ideas. Will explore all 7 core modules and generate a decision-complete work plan.
---

# Draft: provider-health-daemon-review

## Components (topology ledger)
<!-- Lock the SHAPE before depth. One row per top-level component that can succeed or fail independently. -->
<!-- id | outcome (one line) | status: active|deferred | evidence path -->
daemon.py | Main entrypoint: proxy + log monitor + prober + audit | active | daemon.py:1-249
proxy_handler.py | HTTP proxy with health gate + prompt limiting | active | proxy_handler.py:1-383
health_registry.py | Health state CRUD + persistence + admin API | active | health_registry.py:1-232
error_parser.py | Parse HTTP/9router errors → cooldown decisions | active | error_parser.py:1-200
cooldown.py | Exponential backoff calculation | active | cooldown.py:1-130
config.py | Configuration (ports, paths, defaults) | active | config.py:1-23
run.sh | Process management (start/stop/status) | active | run.sh:1-80

## Open assumptions (announced defaults)
<!-- Record any default you adopt instead of asking, so the user can veto it at the gate. -->
<!-- assumption | adopted default | rationale | reversible? -->
Project uses prompt-limiter from hardcoded local path | Keep as-is (local dev workflow) | Hardcoded path in daemon.py:22, proxy_handler.py:19, config.py:19 - may break on other machines | Reversible: make configurable via env var
Health file at ~/.9router/health.json | Keep fixed location | Standard location for 9router integration | Reversible: already uses config.HEALTH_FILE
Log monitoring tails ~/.9router/logs/error.log | Keep as-is | 9router convention | Reversible: could add config option
Proxy runs on port 20131, forwards to 20128 | Keep defaults | Documented in README | Reversible: env vars already supported
Uses threading for concurrency | Keep threading | Simple, works for I/O-bound proxy | Reversible: could migrate to asyncio

## Findings (cited - path:lines)

## Decisions (with rationale)

## Scope IN

## Scope OUT (Must NOT have)

## Open questions

## Approval gate
status: drafting
<!-- When exploration is exhausted and unknowns are answered, set status: awaiting-approval. -->
<!-- That durable record is the loop guard: on a later turn read it and resume at the gate instead of re-running exploration. -->
