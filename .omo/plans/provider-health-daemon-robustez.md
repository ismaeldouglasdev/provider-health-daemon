# provider-health-daemon-robustez - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** Um daemon de health mais robusto: não corrompe mais o arquivo de estado se cair, não vaza memória nos clientes SSE, reusa conexões TCP com o 9router (menos overhead), desliga graciosamente com SIGTERM (sem threads deixadas pra trás), endpoints admin protegidos por chave de API, logs em arquivo com rotação automática, métricas no formato Prometheus, e uma suíte de testes automatizados rodando no GitHub Actions.

**Why this approach:** 9 melhorias independentes, cada uma com seu próprio teste e commit — fáceis de reverter individualmente. Tudo usando only stdlib (exceto pytest no CI), sem adicionar dependências pesadas no runtime.

**What it will NOT do:** Não vai migrar pra async/await, não vai adicionar FastAPI/Flask, não vai containerizar, não vai trocar SSE por WebSocket, não vai adicionar rate limiting por cliente, não vai adicionar banco de dados.

**Effort:** Medium (9 todos, ~3 waves)
**Risk:** Low — todas as mudanças são incrementais e reversíveis
**Decisions I made for you:** pytest como framework de teste (padrão Python), GitHub Actions como CI (já usa GitHub), API key simples (sem JWT), escrita atômica com .tmp + rename, keep-alive via urllib.request.build_opener, log file em ~/.9router/health-daemon.log, Prometheus /metrics sem lib externa.

Your next move: Aprovar este plano. Se quiser, posso rodar uma revisão de alta acurácia (Momus) antes de liberar pra execução.

---

> TL;DR (machine): <1 line - effort, risk, deliverables>

## Scope
### Must have
### Must NOT have (guardrails, anti-slop, scope boundaries)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: <TDD | tests-after | none> + framework
- Evidence: .omo/evidence/task-<N>-provider-health-daemon-robustez.<ext>

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.

## Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1. Thread safety | — | 6, 9 | 2, 3, 4, 5, 8 |
| 2. Graceful shutdown | — | — | 1, 3, 4, 5, 8 |
| 3. Connection pooling | — | — | 1, 2, 4, 5, 8 |
| 4. SSE cleanup | — | — | 1, 2, 3, 5, 8 |
| 5. Config centralization | — | 6, 7, 8, 9 | 1, 2, 3, 4 |
| 6. Test suite + CI | 1, 5 | — | overnada na wave 3 |
| 7. API key auth | 5 | — | overnada na wave 3 |
| 8. File logging | 5 | — | overnada na wave 2 |
| 9. Prometheus /metrics | 1, 5 | — | overnada na wave 3 |

> Waves: Wave 1 = 1,2,3,4,5 (5 tasks paralelos). Wave 2 = 8 (precisa de 5). Wave 3 = 6,7,9 (precisam de 1+5).

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [ ] 1. Thread safety + atomic writes no HealthRegistry
  What to do: Adicionar `threading.Lock` no HealthRegistry (`health_registry.py`), proteger todo acesso a `_data` (leitura/escrita) com `with self._lock`. Substituir `write_text` por escrita atômica: escrever em `.tmp`, depois `os.replace()`. Garantir que `_save()` seja chamada com o lock adquirido.
  Must NOT do: Não mudar a API pública do HealthRegistry. Não adicionar dependências externas. Não mudar o formato do JSON persistido.
  Parallelization: Wave 1 | Blocked by: none | Blocks: 6, 9
  References: `health_registry.py:24-45,94-101,163,217`, cooldown.py (usa CooldownCalculator sem lock)
  Acceptance criteria: `python3 -c "from health_registry import HealthRegistry; r=HealthRegistry('/tmp/test_health.json'); r.mark_healthy('test'); assert r.is_provider_healthy('test')"`
  QA scenarios: happy (mark_healthy + is_provider_healthy), failure (concurrent mark_error de 2 threads, verificar sem corrupção). Evidence `.omo/evidence/task-1-provider-health-daemon-robustez.txt`
  Commit: Y | feat: thread safety + atomic writes in HealthRegistry

- [ ] 2. Graceful shutdown no daemon
  What to do: Adicionar `threading.Event` como shutdown flag. Registrar handler para SIGTERM/SIGINT que seta o event. Modificar loops infinitos (`monitor_logs`, `monitor_access_log`, `alerter_loop`, `audit_loop`, `metrics_snapshot_loop`) para checar `shutdown_event.is_set()` no lugar de `while True`. No `main()`, após `server.run()` retornar, chamar `dashboard.stop()` e aguardar threads com `thread.join(timeout=5)`. Os threads devem ser non-daemon (daemon=False).
  Must NOT do: Não mudar o comportamento de requisições em andamento (requests em voo terminam naturalmente). Não usar `os._exit()` ou `sys.exit()`. Não introduzir async/await.
  Parallelization: Wave 1 | Blocked by: none | Blocks: none
  References: `daemon.py:126-131,137-231,239-315,320-338,366-376,343-422`
  Acceptance criteria: Iniciar daemon em background (`python3 daemon.py &`), capturar PID (`$!`), enviar SIGTERM (`kill <PID>`), verificar que `ps -p <PID>` retorna exit 1 (processo não existe) dentro de 3s. Logs mostram sequência de shutdown ordenada.
  QA scenarios: happy (SIGTERM → processo termina em <2s, logs mostram "shutdown complete"), failure (timeout forçado com SIGKILL → threads são encerradas, sem corrupção de arquivo). Evidence `.omo/evidence/task-2-provider-health-daemon-robustez.txt`
  Commit: Y | feat: graceful shutdown with SIGTERM handler

- [ ] 3. Connection pooling + timeout hardening no proxy
  What to do: Em `proxy_handler.py`, criar `urllib.request.build_opener(urllib.request.HTTPHandler)` uma vez no `HealthProxyServer.__init__` e reutilizar o opener em todas as requisições (em vez de `urllib.request.urlopen` direto). Adicionar timeout de conexão DNS separado (5s) via `urllib.request.Request` + timeout no `urlopen`. Adicionar header `Connection: keep-alive` nas requisições. Garantir que opener seja fechado em shutdown.
  Must NOT do: Não adicionar httpx/requests como dependência. Não mudar a interface do proxy handler. Não alterar o comportamento de health check.
  Parallelization: Wave 2 | Blocked by: none | Blocks: none
  References: `proxy_handler.py:101-158,458-510`
  Acceptance criteria: `python3 -c "from proxy_handler import HealthProxyServer; s=HealthProxyServer(); assert hasattr(s, 'opener')"`
  QA scenarios: happy (2 chamadas consecutivas ao proxy usam mesma conexão TCP), failure (9router down → timeout tratado sem crash). Evidence `.omo/evidence/task-3-provider-health-daemon-robustez.txt`
  Commit: Y | perf: connection pooling and keep-alive in proxy

- [ ] 4. SSE client list cleanup na dashboard
  What to do: Em `dashboard.py`, periodicamente limpar clientes SSE desconectados. Adicionar `sse_clients` como dict com timestamp, remover entradas com timestamp > 60s sem atualização. Implementar thread de cleanup (a cada 30s) que varre e remove clientes que não enviaram heartbeat no intervalo.
  Must NOT do: Não introduzir weakref (compatibilidade Python). Não mudar o formato do SSE event. Não bloquear o loop principal da dashboard.
  Parallelization: Wave 2 | Blocked by: none | Blocks: none
  References: `dashboard.py:311-357,49`
  Acceptance criteria: Conectar SSE client (`curl -s -N http://localhost:20132/api/metrics/sse &`), confirmar recebimento de dados, matar o curl (`kill %1`), verificar que o client_id é removido da lista em <60s via endpoint interno de debug ou inspeção de log.
  QA scenarios: happy (conecta, recebe dados, disconnect clean → client removido imediatamente), failure (cliente cai sem aviso → cleanup loop remove entrada stale em <60s). Evidence `.omo/evidence/task-4-provider-health-daemon-robustez.txt`
  Commit: Y | fix: prevent SSE client list memory leak

- [ ] 5. Centralizar configurações no config.py
  What to do: Mover todas as constantes de caminho hardcoded para `config.py`: `LOG_PATH` (daemon.py:236), `SNAPSHOT_DIR` (metrics_persistence.py:18), `PROMPT_LIMITER_DIR` (config.py já existe mas verificar), `TEMPLATES` (dashboard.py:34). Adicionar suporte a env vars: `HEALTH_PROXY_PORT`, `DASHBOARD_PORT`, `NINEROUTER_URL`, `NINEROUTER_KEY`, `HEALTH_FILE`, `LOG_FILE`. Adicionar validação de startup (ex: portas dentro de range, diretórios existem ou são criados).
  Must NOT do: Não adicionar parsing de .env file (só env vars do sistema). Não mudar a assinatura das funções que usam esses valores. Não quebrar compatibilidade reversa (config sem env var usa default).
  Parallelization: Wave 1 | Blocked by: none | Blocks: 6, 9
  References: `config.py`, `daemon.py:236`, `metrics_persistence.py:18`, `dashboard.py:34`
  Acceptance criteria: `NINEROUTER_URL=http://example.com python3 -c "from config import NINEROUTER_URL; assert 'example.com' in NINEROUTER_URL"`
  QA scenarios: happy (env var sobrescreve default), failure (porta inválida → erro claro). Evidence `.omo/evidence/task-5-provider-health-daemon-robustez.txt`
  Commit: Y | refactor: centralize all config in config.py with env var overrides

- [ ] 6. Test suite com pytest + CI via GitHub Actions
  What to do: Criar `tests/` dir com:
    - `tests/test_health_registry.py` — testar mark_healthy, mark_error, cleanup_expired, is_provider_healthy, força de lock (2 threads escrevendo concorrente)
    - `tests/test_cooldown.py` — testar cálculo de backoff exponencial, limites, permanent error
    - `tests/test_error_parser.py` — testar parse de erros HTTP (429, 403, 400, 500) e log lines
    - Criar `pytest.ini` com configuração básica
    - Criar `.github/workflows/ci.yml` com:
      - Python 3.11, 3.12
      - `uv pip install pytest pytest-cov ruff mypy`
      - `ruff check .`
      - `mypy .` (com `--ignore-missing-imports`)
      - `pytest --cov=. --cov-report=term-missing`
  Must NOT do: Não testar o dashboard HTML/JS (cobertura server-side apenas). Não adicionar testes end-to-end que precisem do 9router rodando. Não usar mock externo — usar dados em memória.
  Parallelization: Wave 3 | Blocked by: 1, 5 | Blocks: none
  References: `health_registry.py`, `cooldown.py`, `error_parser.py`, `proxy_handler.py`
  Acceptance criteria: `pytest tests/ --cov=.` → 100% success, coverage report
  QA scenarios: CI run no GitHub mostra green check. Evidence `.omo/evidence/task-6-provider-health-daemon-robustez.txt`
  Commit: Y | ci: add pytest suite and GitHub Actions CI workflow

- [ ] 7. API key auth para endpoints admin
  What to do: Em `dashboard.py`, adicionar verificação de `X-API-Key` header nos paths `/api/admin/*` e `/api/alert/webhook` (POST). A chave é definida via env var `ADMIN_API_KEY`. Se a env var não estiver setada, os endpoints ficam acessíveis sem auth (backward compat). Se estiver setada, requisições sem o header correto recebem 401. Adicionar também no proxy_handler.py para `/health/reset/`.
  Must NOT do: Não adicionar dependência de JWT/OAuth. Não exigir auth nos endpoints publicos (health, providers, metrics). Não loggar a chave nos logs.
  Parallelization: Wave 3 | Blocked by: 5 | Blocks: none
  References: `dashboard.py:389-433`, `proxy_handler.py:339-386`
  Acceptance criteria: `curl -X POST http://localhost:20132/api/admin/reactivate -H 'X-API-Key: wrong' -d '{"provider":"test"}'` → 401
  QA scenarios: happy (chave correta → 200), failure (chave errada → 401), sem env var (comportamento original). Evidence `.omo/evidence/task-7-provider-health-daemon-robustez.txt`
  Commit: Y | feat: API key auth for admin endpoints

- [ ] 8. File logging com rotação
  What to do: Em `daemon.py:66-79`, adicionar `RotatingFileHandler` como segundo handler (além do stderr). Path do log via config.py (default `~/.9router/health-daemon.log`). Tamanho máximo 10MB, 3 backups. Formato JSON no file também. Manter stderr com ou sem JSON conforme `json_logs` param.
  Must NOT do: Não mudar o formato de log do stderr. Não adicionar dependências externas (RotatingFileHandler é stdlib). Não loggar em disco se o diretório não puder ser criado (apenas stderr).
  Parallelization: Wave 2 | Blocked by: 5 | Blocks: none
  References: `daemon.py:44-82`, `config.py`
  Acceptance criteria: Rodar daemon → `~/.9router/health-daemon.log` existe com entradas JSON (parseáveis com `python3 -m json.tool` em qualquer linha).
  QA scenarios: happy (log cresce e rotaciona — forçar >10MB com logger.debug massivo, verificar `health-daemon.log.1` criado e log principal resetado), failure (diretório não existe → cria automaticamente). Evidence `.omo/evidence/task-8-provider-health-daemon-robustez.txt`
  Commit: Y | feat: rotating file logging for daemon

- [ ] 9. Prometheus /metrics endpoint
  What to do: Adicionar em `dashboard.py` a rota `GET /metrics` que expõe métricas no formato Prometheus:
    - `health_providers_total{status="healthy"} N`
    - `health_providers_total{status="cooldown"} N`
    - `health_providers_total{status="probing"} N`  
    - `health_providers_total{status="disabled"} N`
    - `health_requests_total{provider="..."} N`
    - `health_requests_latency_ms{provider="..."} N`
    - `health_daemon_uptime_seconds N`
  Não adicionar dependência externa (escrever o format manualmente — é texto plano simples). Atualizar `README.md` com exemplos de consulta.
  Must NOT do: Não adicionar prometheus_client lib. Não expor dados sensíveis (caminhos de arquivo, chaves). Não quebrar o formato de texto Prometheus (cada linha termina com \n, help/type antes das métricas).
  Parallelization: Wave 3 | Blocked by: 1, 5 | Blocks: none
  References: `dashboard.py:190-192`, Prometheus exposition format: https://prometheus.io/docs/instrumenting/exposition_formats/
  Acceptance criteria: `curl http://localhost:20132/metrics` → retorna texto Prometheus válido com pelo menos 5 métricas
  QA scenarios: happy (métricas refletem estado atual), failure (registry vazio → métricas zero). Evidence `.omo/evidence/task-9-provider-health-daemon-robustez.txt`
  Commit: Y | feat: add Prometheus /metrics endpoint

## Final verification wave
> Runs in parallel after ALL todos. All must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit — cada todo foi implementado conforme especificação
- [ ] F2. Code quality review — ruff + mythy passam sem erros
- [ ] F3. Real smoke test — daemon reinicia, dashboard carrega, SSE conecta, admin endpoints funcionam
- [ ] F4. Scope fidelity — nenhuma mudança fora do escopo (sem async/await, sem FastAPI, etc.)

## Commit strategy
Commits individuais por todo (Y em cada um). Ordem sugerida: 1, 5, 2, 3, 4, 8, 6, 7, 9. Mensagens seguem conventional commits.

## Success criteria
- Daemon inicia e para graciosamente (SIGTERM)
- Nenhuma condição de corrida no HealthRegistry (testado com threads concorrentes)
- Proxy reusa conexões TCP (verificado via netstat)
- SSE client cleanup não acumula clientes mortos
- CI pipeline passa (lint + test + cobertura)
- Admin endpoints exigem API key (quando configurada)
- Logs vão para arquivo com rotação
- Métricas Prometheus disponíveis em /metrics
- Todas as configs centralizadas no config.py
