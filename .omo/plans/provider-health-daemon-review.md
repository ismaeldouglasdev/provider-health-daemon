# provider-health-daemon-review - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** <fill last - deliverables in human terms, 1-2 sentences>

**Why this approach:** <fill last - the one or two load-bearing decisions and why>

**What it will NOT do:** <fill last - 1-3 plain lines mirroring Must NOT have>

**Effort:** <Quick | Short | Medium | Large | XL>
**Risk:** <Low | Medium | High> - <one-line driver>
**Decisions to sanity-check:** <fill last - the few choices worth a human glance>

Your next move: <fill - e.g. approve, or run a high-accuracy review>. Full execution detail follows below.

---

> TL;DR (machine): <1 line - effort, risk, deliverables>

## Scope
### Must have
- Fix critical thread safety and data corruption bugs
- Implement atomic file writes to prevent health.json corruption
- Fix model-specific health marking (current bug marks entire provider healthy)
- Fix exponential backoff overflow issues
- Implement streaming response support for SSE
- Add proper signal handling and graceful shutdown
- Add comprehensive admin endpoints for observability
- Add structured logging with file rotation
- Add configuration validation
- Implement debounced saves to reduce I/O
- Add circuit breaker pattern for fast failure
- Add health-aware routing alternatives
- Add metrics persistence and Prometheus export
- Add CLI admin interface
- Add config file support
- Add TTL cleanup for stale entries
- Improve error handling and provider detection
- Add startup health checks
- Improve prompt limiting with semantic preservation

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No UI changes (this is a daemon, no web UI)
- No breaking API changes (maintain backward compatibility)
- No new dependencies (keep stdlib and prompt-limiter only)
- No async/await migration (keep threading model)
- No distributed tracing across services
- No user authentication or authorization
- No external monitoring integrations
- No automatic provider discovery (manual config only)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: TDD + pytest framework
- Evidence: .omo/evidence/task-<N>-provider-health-daemon-review.json

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit
- [ ] F2. Code quality review
- [ ] F3. Real manual QA
- [ ] F4. Scope fidelity

## Commit strategy

## Success criteria
