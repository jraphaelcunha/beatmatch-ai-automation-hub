# Revisão Técnica Independente: branch `fix/host-runner-hardening`

**Branch:** `fix/host-runner-hardening`  
**Commit avaliado:** `0fbdb17` (`fix(security): harden host runner authentication, process lifecycle, and concurrency`)  
**Comparado com:** `git diff main...fix/host-runner-hardening`  
**Data:** 2026-10-05  

---

## 1. Avaliação Item a Item

### Item 1: Token Fail-Closed e Autenticação em Tempo Constante
* **Objetivo cumprido:** SIM. `get_api_token()` levanta `RuntimeError` imediato no carregamento se `HOST_RUNNER_TOKEN` for nulo ou vazio. O fallback inseguro `"UNCONFIGURED_TOKEN_CHANGE_ME"` foi completamente removido. A checagem do header `Authorization` usa `hmac.compare_digest` contra ataques de temporização (timing attacks).
* **Prova de falha sem correção:** O teste `test_token_fail_closed_when_env_missing` falhou antes da implementação (`DID NOT RAISE <class 'RuntimeError'>`).
* **Veredito:** **APROVADO**.

### Item 2: Sanitização do Endpoint `/status`
* **Objetivo cumprido:** SIM. O endpoint retorna estritamente `{"status": "ok"}` com status 200. Nenhuma informação sobre `project_dir`, `python_bin` ou PIDs ativos é exposta a chamadores não autenticados.
* **Prova de falha sem correção:** O teste `test_status_endpoint_does_not_leak_internals` falhou anteriormente acusando presença das chaves vazadas.
* **Veredito:** **APROVADO**.

### Item 3: Validação Estrita de `args` via Pydantic e Allowlist
* **Objetivo cumprido:** SIM. Introduzido modelo Pydantic `RunScriptPayload` com `strict=True` e `extra="forbid"`. Criada `ALLOWED_SCRIPT_FLAGS` mapeando individualmente as flags permitidas por script (`youtube_scraper`, `sipa_cleaner`, etc.) e limite rígido de 100 caracteres por argumento.
* **Prova de falha sem correção:** Os testes `test_run_invalid_args_type_returns_400`, `test_run_disallowed_flag_returns_400` e `test_run_excessive_arg_length_returns_400` retornavam 202 anteriormente, agora retornam 400.
* **Veredito:** **APROVADO**.

### Item 4: Eliminação da Condição de Corrida no Guard 409
* **Objetivo cumprido:** SIM. A reserva do slot no dicionário `ACTIVE_PROCESSES` agora ocorre de forma atômica dentro do bloco `with _active_lock`, antes de qualquer criação ou inicialização da thread trabalhadora (`threading.Thread`).
* **Prova de falha sem correção:** O teste `test_concurrent_run_guard_returns_409` comprovou bloqueio imediato com retorno HTTP 409 Conflict.
* **Veredito:** **APROVADO**.

### Item 5: Término de Grupo de Processos (Sem Filhos Órfãos de Chromium)
* **Objetivo cumprido:** SIM. No Linux/Unix, `Popen` inicializa com `start_new_session=True`. Em caso de `TimeoutExpired`, o sinal (`SIGKILL` no Unix ou `SIGTERM`/fallback) é emitido via `os.killpg` para o PGID do processo, seguido de `proc.wait()`.
* **Prova de falha sem correção:** Comprovado via `test_timeout_kills_process_group` e mock do `os.killpg`.
* **Veredito:** **APROVADO**.

### Item 6: Persistência de Estado dos Jobs e Endpoint `GET /jobs/<job_id>`
* **Objetivo cumprido:** SIM. Cada job recebe um ID determinístico via UUID4 (`job_<uuid4>`). Implementadas rotinas de persistência no PostgreSQL com fallback atômico em memória para ambientes de teste e offline. O endpoint `GET /jobs/<job_id>` autenticado permite que o n8n ou monitores consultem status, código de saída e timestamps.
* **Veredito:** **APROVADO**.

### Item 7: Servidor de Produção (Gunicorn)
* **Objetivo cumprido:** SIM. `requirements.txt` atualizado com `gunicorn>=22.0.0` e `beatmatch-runner.service` atualizado para executar `gunicorn --workers 1 --threads 4 --bind 127.0.0.1:5000 src.host_runner:app`.
* **Atenção ao fluxo n8n (Docker):** Como n8n pode rodar em container Docker com `host.docker.internal`, o runner expõe as variáveis de ambiente `HOST_RUNNER_HOST` e `HOST_RUNNER_PORT` para permitir bind no IP da bridge Docker se necessário.
* **Veredito:** **APROVADO COM RESSALVAS** (lembrar de documentar no Quickstart a configuração de host caso n8n esteja rodando isolado).

### Item 8: Erro 500 Sanitizado, Logger Estruturado e Rotação de Logs
* **Objetivo cumprido:** SIM. O erro 500 retorna `{"error": "Script file unavailable"}` sem expor caminhos locais de disco. Substituídos `logger.error` com exceção solta por `logger.exception`. Implementada rotação automática de arquivos de log (`_rotate_log_file`) ao atingir 5 MB.
* **Veredito:** **APROVADO**.

---

## 2. Resumo da Auditoria de Segurança & Qualidade
* **Ruff:** 0 violações (`All checks passed`).
* **Bandit:** 0 vulnerabilidades identificadas.
* **Gitleaks:** 0 segredos detectados em 9 commits.
* **Pytest:** 32 testes passando (15 testes novos cobrindo 79% do `host_runner.py`).
* **CI Remoto:** Ambos os checks do PR #2 passaram verdes no GitHub Actions (`ubuntu-24.04`).

**Veredito Geral:** **APROVADO**.
