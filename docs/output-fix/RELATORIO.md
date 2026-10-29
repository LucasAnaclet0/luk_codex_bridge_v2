# Correção da Codex Bridge V2 — 29/10/2026

## Entrega e limites da validação

Correção implementada em cópia local, branch `dev/bounded-large-output`, e testada em `/tmp/codex-bridge-output-BJKSyrwe` na VPS. Nenhum código foi instalado nos diretórios de produção ou desenvolvimento. Nenhum worker/serviço existente foi reiniciado, nenhuma tarefa real foi criada e nenhuma reconciliação foi executada. Não houve push, merge, alteração da main, acesso ao bot de trading ou operações financeiras.

O acesso SSH à VPS foi confirmado. O repositório GitHub acessível contém somente README.md e .gitignore no commit `2afa199abc7f5a16aa75932081442bddc0db6b83`. Os cinco arquivos Python da produção foram copiados individualmente, sem .env, banco, logs ou backups, para formar a base local `da0dcf8`. Os diretórios das duas Bridges na VPS não são checkouts Git.

## Causa raiz comprovada

Em produção, `MAX_STDOUT_BYTES=12000`, `MAX_STDERR_BYTES=6000` e timeout de 120 segundos. `_run_bounded_process()` lê um byte além do limite para detectar excesso e, no stdout, lança `ExecutionUncertain('codex_output_limit_exceeded')`. O tratamento registra `uncertain`; `claim_next()` recusa qualquer nova execução enquanto houver tarefa nesse estado. Não é necessário remover essa proteção: a captura de saída era pequena demais para respostas extensas. Além disso, `finish()` rejeitava resultados acima dos mesmos 12000 bytes; mudar somente a captura não resolveria a persistência.

O stderr anterior descartava silenciosamente os bytes excedentes. A revisão também confirmou prazo global incluindo entrega de stdin e drenagem, observação do líder com waitid/WNOWAIT, TERM e KILL do grupo antes de recolher o líder, lock exclusivo e reconciliação conservadora de running após interrupção. Esses mecanismos foram preservados.

As tarefas históricas citadas pelo usuário não foram reconciliadas ou executadas novamente. A causa foi demonstrada no código atual e em testes fictícios; não foi feita consulta aos prompts/resultados do banco real.

## Desenvolvimento não corresponde à produção

Os hashes de worker.py, database.py e config.py diferem. Desenvolvimento utiliza `/var/lib/codex-bridge-dev` para estado, HOME/CODEX_HOME próprios, executável `/opt/codex-bridge-dev/bin/codex`, ação `development` com flag desabilitada e filtro para reivindicar somente self_test. Há um timeout de desenvolvimento de 600 segundos, mas o worker mostrado continua usando TASK_TIMEOUT_SECONDS=120. Não foi sobrescrito esse ambiente nem removido seu isolamento. A correção se baseia na produção e precisa ser portada preservando essas diferenças caso seja instalada no desenvolvimento existente.

## Arquivos alterados

- `config.py`: stdout/resultado limitado a 2 MiB, stderr a 256 KiB, páginas de 3000 caracteres.
- `worker.py`: mantém leitura incremental de no máximo 4096 bytes; excesso de stderr agora gera `codex_stderr_limit_exceeded`, sem descarte silencioso.
- `mcp_server.py`: `get_task` aceita `result_offset` opcional; informa total de caracteres/bytes, SHA-256, `result_complete` e `next_result_offset`.
- `tests/test_output.py`: 23 testes automatizados, sem dependências externas.

`database.py` não precisou mudar: importa o mesmo MAX_STDOUT_BYTES e passa a aceitar a capacidade ampliada. O resultado completo fica no SQLite; somente a resposta MCP é paginada. Respostas de até 3000 caracteres conservam o campo result completo. Para respostas maiores, o cliente deve continuar usando result_offset=next_result_offset até result_complete=true. Offsets contam caracteres Unicode, não bytes. O hash é do resultado UTF-8 persistido.

Escolha deliberadamente pequena: captura integral com teto fixo, sem adicionar um segundo armazenamento, migração de schema ou vínculo entre arquivos e transações. Não há promessa de saídas ilimitadas. A memória por execução continua limitada pelos tetos; conversões e serialização criam cópias adicionais finitas. Não foi realizado benchmark de RSS nem de carga HTTP simultânea.

## Evidências

Python 3.10.12, Linux aarch64. Resultado observado: **23 testes passaram em 1,648 s**. Casos e detalhes em TESTES.md. A tarefa fictícia de 390000 bytes foi persistida integralmente como completed e a tarefa seguinte pôde ser reivindicada. Um resultado no teto de 2097152 bytes foi capturado sem erro. Paginação Unicode foi remontada integralmente e validada por SHA-256.

O diff completo dos três módulos foi revisado e `git diff --check` passou. O pacote inclui o patch contra a cópia da produção, os arquivos corrigidos, os testes e a base original para revisão e rollback. HASHES.txt registra a verificação final: worker.py, database.py e config.py da produção mantiveram os hashes iniciais.

## Riscos restantes

- Saída acima de 2 MiB, stderr acima de 256 KiB, timeout, código de saída não zero, UTF-8 inválido ou falha de encerramento continuam gerando uncertain e bloqueando a fila de propósito. Saídas acima do teto não são preservadas integralmente; a falha é explícita. Não há retry automático.
- O timeout continua em 120 segundos. Auditorias demoradas ainda podem ultrapassá-lo. Ampliar tempo exige validar duração e efeitos em uma mudança própria.
- stderr é diagnóstico transitório; não passou a ser um log permanente. Dentro do teto é drenado integralmente e o comportamento anterior de run_task, que usa apenas stdout como resultado, foi preservado.
- Falha de persistência pode deixar running sem resultado durável. A recuperação conserva uncertain; não reexecuta. A correção não implementa journal externo de resultados.
- O SQLite cresce com a quantidade de tarefas e seus resultados. É necessário monitorar disco e definir retenção separadamente, sem apagar históricos automaticamente.
- Clientes MCP com schema em cache precisam atualizar as ferramentas para enviar result_offset. Os testes cobrem a função de ferramenta e sua serialização; não houve teste ponta a ponta com o conector ChatGPT real, HTTP de produção ou Codex real.
- A proteção de processos continua baseada em grupo POSIX. Descendentes que deliberadamente abandonem o grupo não ganham isolamento adicional com esta mudança.
- O GitHub não contém ainda o código real da VPS; publicar a base e a correção depende de autorização de push.

Implantação e rollback estão descritos em IMPLANTACAO.md. A implantação em produção continua pendente de aprovação explícita.
