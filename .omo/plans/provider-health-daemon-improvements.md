# provider-health-daemon-improvements - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** O fim definitivo dos erros "Model not found" ao retomar sessões do opencode (limpeza automática e contínua dos registros corrompidos, com proteção contra atualizações que desfazem correções), reinicializações do gateway de IA sem janelas de indisponibilidade que quebram as sessões, acesso mais seguro (serviços escutando só na máquina local), rotação inteligente que prioriza os modelos gratuitos ainda não usados no dia, painel com tempos de resposta reais e alertas, e infraestrutura de qualidade (CI, linter, documentação).

**Why this approach:** O incidente de 20/08 à noite provou a cadeia causal completa: restart do gateway → janela morta de ~30s → fallback chains cascatajando para modelos mortos → registros fantasmas no banco → retomada de sessão quebrada. O plano ataca os três elos (janela morta, fonte dos ghosts, limpeza) em vez de só tratar o sintoma.

**What it will NOT do:** Não implementa pools multi-conta; não reescreve o projeto em outra linguagem nem adiciona dependências externas; não adiciona autenticação ao dashboard.

**Effort:** Medium
**Risk:** Medium - mexe no daemon que serve a própria inferência do opencode; mitigado por bind-first, testes (574) e CI remoto verde
**Decisions I made for you:** (1) sanitizador só toca registros com mais de 1h — sessões vivas intocadas; (2) serviços passam a escutar apenas em 127.0.0.1 (uso é 100% local); (3) CI = GitHub Actions rodando o pytest existente; (4) cota diária calculada dos dados locais já existentes, sem API externa; (5) patcher do plugin é idempotente com marker e roda no timer horário. Fora de escopo: pools multi-conta, rewrite, auth no dashboard.

Your next move: plano aprovado para execução via `$start-work provider-health-daemon-improvements`. Full execution detail follows below.

---

> TL;DR (machine): Esforço médio, risco médio — 9 todos em 3 waves: sanitizador anti-ghost hourly + timer, re-aplicador de patch do plugin, bind-first (<3s), bind 127.0.0.1, rotação quota-aware, TTFT p50/p95 + alerta no dashboard, auditoria de CI, baseline ruff/pyright + README/runbook.

## Scope
### Must have
- C1 Anti-ghost guard: sanitizador periódico do opencode.db + re-aplicador idempotente do patch dot-strip do plugin oh-my-openagent (perdido em updates @latest — ocorreu em 14/08 21h03)
- C2 Restart resilience: daemon deve bindar :20131/:20132 ANTES da inicialização pesada (hoje o boot roda sanity checks antes do bind, deixando ~30s de janela morta que faz fallback chains do opencode cascatajarem para modelos Zen mortos — incidente 20/08 23h07)
- C3 Security: bind padrão 127.0.0.1 nos dois servidores HTTP do daemon
- C4 Quota-aware rotation: priorizar providers free sem uso no dia (objetivo do usuário: "zerar todos os providers free todos os dias")
- C5 Observability: TTFT real (p50/p95) no dashboard + alerta de pool degradado
- C6 Hygiene: CI (GitHub Actions pytest), ruff+pyright baseline, README com arquitetura e runbook

### Must NOT have (guardrails, anti-slop, scope boundaries)
- NÃO implementar pools multi-conta (task Plexo própria: "Provider Discovery Dinâmico + Pools de Contas")
- NÃO reescrever em outra linguagem nem introduzir dependências externas novas (projeto é stdlib-only)
- NÃO adicionar sistema de auth ao dashboard
- NÃO alterar semântica dos combos existentes (combo-round-robin, main-rr, combo-fast, combo-thinking)
- NÃO editar o bundle do opencode core (bun) — só o plugin oh-my-openagent via patcher idempotente com marker
- NÃO limpar refs providerID=opencode com menos de 1h (sessões vivas)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after, pytest (suíte existente: 444 passed em 11s — `python3 -m pytest tests/ -q`)
- Evidence: `.omo/evidence/task-<N>-provider-health-daemon-improvements.<ext>` (logs de comando + saída)

## Execution strategy
### Parallel execution waves
> Wave 1: fundamentos independentes (sanitizador, patcher, bind-first, security). Wave 2: quota (depende de explorar metrics_store). Wave 3: observability + hygiene.

### Live-system execution constraints (obrigatórias)
- O daemon :20131 serve a PRÓPRIA inferência que o worker usa para executar. Todos 3 e 4 exigem restart/rebind: aplicar AMBOS os commits e testar num ÚNICO restart (nunca dois restarts separados).
- Nunca reiniciar o daemon com streaming em andamento no próprio worker (aguardar turno ocioso entre tool calls).
- Sanitizador (todo 1) roda contra opencode.db em WAL com busy_timeout=60s; se travar >60s, abortar sem commit parcial (transação única por batch já garante).
- Rejeitado por escopo (review round-20260820-2359): "maintenance mode" no daemon — alto custo, risco coberto pelas constraints acima.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
> **Restart batching:** todos 3 e 4 são desenvolvidos em paralelo mas deployados juntos — UM ÚNICO restart ao final da Wave 1 cobrindo ambos (ver Live-system execution constraints).
| 1 | — | 2 | 3, 4 |
| 2 | 1 | — | 3, 4 |
| 3 | — | — | 1, 2, 4 |
| 4 | — | — | 1, 2 |
| 5 | — | 6 | 1–4 |
| 6 | 5 | — | 7, 8, 9 |
| 7 | — | — | 5, 6, 8, 9 |
| 8 | — | — | 5, 6, 7, 9 |
| 9 | 8 | — | 5, 6, 7 |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [x] 1. Sanitizador anti-ghost do opencode.db + systemd timer
  What to do / Must NOT do: Criar `scripts/opencode_ghost_sanitizer.py` (stdlib-only): conecta em `~/.local/share/opencode/opencode.db` com `PRAGMA busy_timeout=60000`; para as tabelas/colunas `message.data`, `session.model`, `part.data`, `session_message.data`, `event.data`: localiza rows contendo `"providerID":"opencode"` cuja idade >1h (`message.time_created`/`session.time_created`; part/session_message/event via join ou rowid proxy — usar `time_created` quando a tabela tiver, senão SKIP (conservador: row sem timestamp NUNCA é limpa), parse JSON recursivo e substitui qualquer dict com `providerID=="opencode"` por `{"providerID":"9router","id/modelID":"ollama/gpt-oss:120b"}` removendo `variant` (mesma lógica validada em 20/08, ver backup `/mnt/data/opencode.db.bak-ghostfix-20260820-223053`). Loga cada row alterada em `~/.9router/ghost-sanitizer.log` ANTES do UPDATE. Idempotente. Criar `~/.config/systemd/user/opencode-ghost-sanitizer.{service,timer}` (OnCalendar=hourly). NÃO tocar em refs com <1h; NÃO deletar rows; NÃO rodar VACUUM.
  Parallelization: Wave 1 | Blocked by: — | Blocks: 2
  References (executor has NO interview context - be exhaustive): fixer validado nesta sessão (padrão recursivo `fix_node`); schema REAL verificado via pragma_table_info: `message.time_created` e `session.time_created` existem; bugs-erros-opencode.md entradas 2026-08-14 e 2026-08-20
  Acceptance criteria (agent-executable): PRÉ-CONDIÇÃO: script asserta colunas esperadas via `pragma_table_info` antes de agir (schema drift → exit 2 com mensagem clara); `python3 scripts/opencode_ghost_sanitizer.py --dry-run` lista rows alvo sem modificar; execução real reduz `SELECT COUNT(*) FROM message WHERE data LIKE '%"providerID":"opencode"%' AND time_created < (strftime('%s','now')-3600)*1000` para 0; `PRAGMA integrity_check` retorna ok; `systemctl --user list-timers opencode-ghost-sanitizer.timer` mostra timer agendado
  QA scenarios: happy — criar row fake antiga em DB de teste (`/tmp/opencode/test.db`) e verificar remap; failure — DB locked (segundo processo segurando lock) deve logar erro e sair código 1 sem corromper; stress — rodar sanitizador enquanto um loop writer LIMITADO (60s, sqlite3 INSERT a cada 100ms no DB de teste) grava em paralelo (WAL); sanitizador deve terminar (busy_timeout resolve locks) e `PRAGMA integrity_check` ok ao final; se o sanitizador exceder 120s totais, matar com timeout e reportar falha. Evidence .omo/evidence/task-1-provider-health-daemon-improvements.log
  Commit: Y | feat(scripts): sanitizador anti-ghost do opencode.db com timer hourly

- [x] 2. Re-aplicador idempotente do patch dot-strip do plugin
  What to do / Must NOT do: Criar `scripts/omo_plugin_patcher.py`: verifica que o arquivo existe (`os.path.isfile`, senão exit 1 com mensagem) e se `~/.cache/opencode/packages/oh-my-openagent@latest/node_modules/oh-my-openagent/dist/index.js` contém o marker `resolved.model = resolved.model.replace(/\.+$/, "");` dentro de `resolveModelPipeline2`; se ausente, aplica o patch (backup `index.js.bak-patcher-<ts>` antes), valida com `node --check`; se presente, exit 0 silencioso. Chamar no início do sanitizer (todo 1) e adicionar ExecStartPre no service do timer. NÃO aplicar nenhum outro patch; NÃO editar arquivo se marker presente.
  Parallelization: Wave 1 | Blocked by: 1 (mesmo serviço/timer) | Blocks: —
  References: patch original aplicado em 14/08 (bugs-erros-opencode.md, seção sandotfix); função em dist/index.js linha ~83933 `function resolveModelPipeline2(request)`
  Acceptance criteria: remover manualmente o marker numa cópia de teste e rodar o patcher restaura o marker + `node --check` ok; rodar 2x seguidas = segunda é no-op; log registra ação
  QA scenarios: happy — patch aplicado em cópia /tmp; failure — arquivo ausente → erro claro exit 1. Evidence .omo/evidence/task-2-provider-health-daemon-improvements.log
  Commit: Y | feat(scripts): re-aplicador idempotente do patch dot-strip do plugin

- [x] 3. Bind-first: servidor HTTP sobe antes da inicialização pesada
  What to do / Must NOT do: Em `daemon.py`, mover a criação/bind do HTTPServer (:20131 proxy e :20132 dashboard) para ANTES dos sanity checks/probe inicial/catálogo sync; inicialização pesada vira thread background. Objetivo: janela entre `systemctl restart` e primeiro 200 em :20131 < 3s. NÃO mudar comportamento dos handlers; N remover checks, só postergá-los.
  Parallelization: Wave 1 | Blocked by: — | Blocks: —
  References: daemon.py função main()/start do servidor (linhas ~620+, threads em ~782-871); incidente: restart às 22h49 levou ~30s até bind (medido nesta sessão)
  Acceptance criteria: BASELINE antes: medir `systemctl --user restart provider-health-daemon` → primeiro curl 200/401 aceito (loop 1s, registrar segundos — baseline medido nesta sessão: ~30s). Depois: mesmo teste <3s; suíte pytest 444+ green
  QA scenarios: happy — restart medido <3s até aceitar conexão (curl loop com timestamp); failure — porta ocupada por processo externo deve falhar com mensagem clara (não crash-loop silencioso). Evidence .omo/evidence/task-3-provider-health-daemon-improvements.log
  Commit: Y | perf(daemon): bind-first elimina janela morta no restart

- [x] 4. Security: bind 127.0.0.1 por padrão
  What to do / Must NOT do: `config.py`: adicionar `HEALTH_PROXY_HOST = os.environ.get("HEALTH_PROXY_HOST", "127.0.0.1")` e `DASHBOARD_HOST` análogo; `daemon.py` usa nas duas instâncias HTTPServer. Atualizar unit systemd se necessário. NÃO adicionar auth; NÃO mudar portas.
  Parallelization: Wave 1 | Blocked by: — | Blocks: —
  References: config.py seção Network (~linha 27-38); ss hoje mostra 0.0.0.0:20131/20132
  Acceptance criteria: `ss -tln | grep 20131` mostra `127.0.0.1:20131` (não 0.0.0.0); request determinístico pós-restart: `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:20131/v1/chat/completions -H 'Authorization: Bearer <key do opencode.json>' -H 'Content-Type: application/json' -d '{"model":"combo-round-robin","messages":[{"role":"user","content":"pong"}],"max_tokens":300,"stream":true}'` retorna 200; override `HEALTH_PROXY_HOST=0.0.0.0` sobe em 0.0.0.0 quando setado
  QA scenarios: happy — curl local 200 após restart; failure — acesso de fora da máquina recusado (testar via IP LAN local se disponível, senão ss assertion basta). Evidence .omo/evidence/task-4-provider-health-daemon-improvements.log
  Commit: Y | fix(security): bind 127.0.0.1 por padrão nos servidores do daemon

- [x] 5. Agregação de uso diário por provider
  What to do / Must NOT do: Estudar `metrics_store.py` + `data.sqlite` (schema real pode diferir — INSPECIONAR antes) e expor `daily_provider_usage(date=None) -> dict[provider, dict[tokens, requests, cost]]` somando registros do dia (timezone America/Sao_Paulo). Se data.sqlite não tiver provider/model nas linhas de uso, derivar do access.log via `access_parser.py` existente. Testes com fixtures temporárias. NÃO criar tabela nova sem necessidade; NÃO usar API externa.
  Parallelization: Wave 2 | Blocked by: — | Blocks: 6
  References: metrics_store.py, access_parser.py, ~/.9router/data.sqlite (inspecionar schema), ai_usage_record é do opencode (NÃO usar)
  Acceptance criteria: PRÉ-CONDIÇÃO: daemon ativo (`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:20131/v1/models` retorna 200/401); pytest novo `tests/test_daily_usage.py` green; chamada retorna dict não-vazio após gerar tráfego real via curl no proxy
  QA scenarios: happy — 3 requests reais → usage conta 3; failure — dia sem dados → dict vazio sem exceção. Evidence .omo/evidence/task-5-provider-health-daemon-improvements.log
  Commit: Y | feat(metrics): agregação de uso diário por provider

- [x] 6. Rotação quota-aware no meta-router/combo
  What to do / Must NOT do: No ponto onde o 9router/combo escolhe provider (inspecionar como o combo-round-robin resolve no proxy — `_spread_select`/combo cache em proxy_handler.py), aplicar boost de peso a providers com uso diário == 0 (flag `QUOTA_AWARE_ROTATION=true` em config.py, default ligado). Dashboard: contador "providers zerados hoje". NÃO mudar ordem quando flag off; NÃO penalizar providers pagos.
  Parallelization: Wave 2 | Blocked by: 5 | Blocks: —
  References: smart_router.py SPREAD_BAND linha 232 e `_spread_select` linhas 455-472 (verificado), meta_router.py weighted pick, todo 5
  Acceptance criteria: com flag on e fixture de uso simulado, sequência de escolhas prefere provider zerado (teste unitário determinístico com mock); flag off comporta igual ao atual (444 green)
  QA scenarios: happy — teste unitário de weighting; failure — metrics indisponível → degrada para comportamento atual sem exceção. Evidence .omo/evidence/task-6-provider-health-daemon-improvements.log
  Commit: Y | feat(routing): rotação quota-aware para zerar free tiers diários

- [x] 7. Dashboard: TTFT p50/p95 + alerta de pool degradado
  What to do / Must NOT do: `dashboard.py`: painel com TTFT p50/p95 das últimas 24h (ttft_ms já coletado em _record_usage/_record_upstream_health — confirmar campo no metrics store) e badge/alert via `alerter.py` quando healthy_count < limiar (config `POOL_DEGRADED_THRESHOLD`, default 8). NÃO adicionar dependência de frontend build.
  Parallelization: Wave 3 | Blocked by: — | Blocks: —
  References: dashboard.py (~23KB, endpoints /api/*), alerter.py, ttft_ms em proxy_handler.py (_record_upstream_health/_record_usage)
  Acceptance criteria: GET /api/ (novo endpoint ou extensão) retorna ttft_p50/p95 numéricos; threshold testável com valor alto forçando alert em teste
  QA scenarios: happy — após tráfego real, p50 > 0; failure — sem dados → null sem crash. Evidence .omo/evidence/task-7-provider-health-daemon-improvements.log
  Commit: Y | feat(dashboard): TTFT p50/p95 e alerta de pool degradado

- [x] 8. CI GitHub Actions — auditar e estender existente
  What to do / Must NOT do: CI JÁ EXISTE (`.github/workflows/ci.yml`: lint-test, matrix python 3.11/3.12 via uv, push/PR em master — verificado). Tarefa: garantir que a suíte pytest completa roda no workflow (conferir step de test; adicionar `ruff check scripts/ tests/` se ausente após todo 9 criar config). NÃO criar workflow novo; NÃO adicionar jobs além do existente.
  Parallelization: Wave 3 | Blocked by: — | Blocks: 9
  References: suíte atual roda com `python3 -m pytest tests/ -q` (444 passed, stdlib-only)
  Acceptance criteria: registrar conteúdo atual na evidence (`cat .github/workflows/ci.yml`); workflow contém step executando pytest; próximo push dispara run verde no GitHub (verificar via `gh run list --limit 1`)
  QA scenarios: happy — run verde no push; failure — quebrar um teste propositalmente em branch descartável mostra run vermelho (opcional, evidência opcional). Evidence .omo/evidence/task-8-provider-health-daemon-improvements.log
  Commit: Y | ci: GitHub Actions rodando pytest

- [x] 9. ruff + pyright baseline + README/runbook
  What to do / Must NOT do: `pyproject.toml` mínimo com [tool.ruff] (line-length 120, target py312) e [tool.pyright] em modo basic com excludes para padrões legados conhecidos (pyright já existe localmente: `~/.local/bin/pyright-langserver` — usar esse binário; se ausente, npm install -g --prefix /mnt/data/npm-global conforme regra de storage) (proxy_handler.py tipos None etc. — baseline pragmático, não big-bang); README.md com diagrama da cadeia (opencode→20131→20128→providers, kiro 20129, dashboard 20132), runbook (restart seguro, sanitizador, cooldowns, ports) e seção de troubleshooting apontando bugs-erros-opencode.md. Rodar `ruff check --fix` apenas em arquivos NOVOS (scripts/ dos todos 1-2); NÃO refatorar legado.
  Parallelization: Wave 3 | Blocked by: 8 | Blocks: —
  References: LSP errors pré-existentes listados nesta sessão (tipagem fraca em proxy_handler.py); bugs-erros-opencode.md como fonte do runbook
  Acceptance criteria: `ruff check scripts/ tests/` zero erros; README.md existe com seções Arquitetura/Runbook/Troubleshooting; CI continua verde
  QA scenarios: happy — comandos acima exit 0; failure — introduzir erro sintético em script novo → ruff pega. Evidence .omo/evidence/task-9-provider-health-daemon-improvements.log
  Commit: Y | chore: ruff/pyright baseline + README com runbook

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. Plan compliance audit — todos os must-have atendidos; guardrails auditados
- [x] F2. Code quality review — 574 testes, Ruff e CI remoto verdes; Pyright inconclusivo
- [x] F3. Real manual QA — restart/bind/health/pool verificados em produção local
- [x] F4. Scope fidelity — sem core bundle, multi-account pools, auth ou semântica de combos alterados

## Commit strategy
Um commit por todo (9 commits), conventional commits conforme padrão do repo (`feat(scope):`, `fix(scope):`, `perf`, `ci`, `chore`). Push para origin/master ao final de cada wave. Nenhum force-push.

**Rollback:** qualquer commit que regredir é revertido com `git revert <sha>` + `systemctl --user restart provider-health-daemon` (isolamento por commit torna o revert independente). Antes do PRIMEIRO run do sanitizador em produção, snapshot do opencode.db: `sqlite3 ~/.local/share/opencode/opencode.db ".backup '/mnt/data/opencode.db.bak-pre-sanitizer-<ts>'"`. Se o bind-first quebrar o boot: `git revert` do commit 3 restaura comportamento anterior (systemd auto-restart recupera).

## Success criteria
1. `opencode/kimi-k3..`-class errors impossíveis de persistir: sanitizador hourly ativo e verificado por timer
2. Restart do daemon aceita conexões em <3s (fim do cascade de fallback chains)
3. `ss -tln` mostra 127.0.0.1 nos ports do daemon
4. Providers free sem uso no dia recebem prioridade comprovada por teste unitário
5. Dashboard expõe TTFT p50/p95 e alerta de pool degradado dispara em threshold
6. CI verde no GitHub; ruff limpo em scripts/ e tests/; README com runbook
7. Suíte pytest completa green (444 + novos) após cada todo
