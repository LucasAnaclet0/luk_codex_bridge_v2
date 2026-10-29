# Evidências dos testes isolados

Execução observada em 29/10/2026, diretório `/tmp/codex-bridge-output-BJKSyrwe`, Python 3.10.12, Linux aarch64. Banco temporário novo por teste; subprocessos são exclusivamente Python com `-I`, ou mocks. Não foi iniciado o executável Codex, o main do worker ou o servidor MCP.

Resultado do unittest: `Ran 23 tests in 1.648s — OK`.

| Teste | Evidência |
|---|---|
| reproduces_old_12000_byte_failure | 12001 bytes com teto antigo geram codex_output_limit_exceeded |
| large_result_persisted_and_next_task_claimable | 390000 bytes Unicode, conteúdo exato no banco, completed, próxima tarefa reivindicada |
| exact_stdout_ceiling | 2097152 bytes completos, exit 0 |
| hard_stdout_ceiling_is_explicit | teto+1 resulta em uncertain explícito |
| stderr_above_old_limit_is_complete | 20000 bytes completos, sem truncar em 6000 |
| exact_stderr_ceiling | 262144 bytes completos |
| stderr_never_silently_truncated | teto+1 gera codex_stderr_limit_exceeded |
| both_pipes_do_not_deadlock | 200000 bytes em cada pipe, exit 0 |
| timeout_remains_uncertain_and_blocks_queue | prazo fictício de 0,1 s, estado e bloqueio preservados |
| descendant_holding_pipe_times_out_and_is_killed | líder sai, descendente mantém pipe; timeout encerra grupo em menos de 2 s |
| nonzero_exit_observed | exit 7 corretamente observado |
| run_task_nonzero_is_uncertain | run_task com processo mockado mantém uncertain |
| invalid_utf8_is_uncertain | resultado inválido não vira completed |
| start_failure_is_safe_failed | falha pré-execução fica failed |
| cleanup_failure_is_uncertain | encerramento não confirmado mantém uncertain |
| concurrent_claim_same_task_once | 8 concorrentes, exatamente 1 claim da tarefa |
| worker_exclusive_lock | segundo lock recusado |
| recovery_never_requeues | running vira uncertain; recuperação idempotente |
| commit_error_after_success_is_confirmed | commit com confirmação perdida é conferido por leitura |
| persistence_failure_remains_running_then_recoverable | falha de gravação retorna persistence_unconfirmed, recuperação conservadora |
| database_rejects_oversize_and_duplicate_finish | excesso e segunda finalização rejeitados |
| mcp_pages_reassemble_unicode_result | páginas recompõem resultado exato com SHA-256 e contagem de bytes |
| mcp_short_result_backwards_compatible_and_offsets_validated | resultado curto completo; offset negativo, booleano, string e fora do intervalo rejeitados |

Reprodução, em uma cópia Linux isolada do diretório `source` do pacote:

```sh
python3 -m unittest discover -s tests -v
```

Não executar esse comando dentro do diretório de produção: config.py carrega o .env adjacente. A cópia fornecida não inclui .env ou banco. Os testes nunca chamam initialize com o DB_PATH de configuração; usam somente seus próprios bancos temporários.

O nome do teste de descendentes descreve a intenção do encerramento do grupo. A asserção verifica o erro de timeout e a duração, não enumera todos os processos do sistema. A execução real de TERM/KILL é exercitada; a garantia de ausência absoluta de descendentes fora do grupo não foi testada.
