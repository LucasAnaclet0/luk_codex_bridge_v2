# Implantação e rollback — ainda não executados

## Antes de aprovar

1. Revisar RELATORIO.md, TESTES.md e a totalidade de correction.patch. O patch é contra a cópia da produção, não contra o repositório GitHub vazio de código.
2. Reproduzir os testes numa cópia Linux isolada sem .env, banco real ou credenciais. Usar os arquivos de source/ do pacote. Não executar worker.py nem o bot.
3. Confirmar os hashes atuais de worker.py, config.py e database.py da produção contra HASHES.txt. Se houver divergência, interromper a implantação e adaptar/retestar sobre a nova base. Comparar também mcp_server.py com baseline/mcp_server.py antes de substituí-lo.
4. Validar o cliente MCP com a paginação: a nova assinatura é get_task(task_id, result_offset=0). Consumir next_result_offset até result_complete=true e conferir SHA-256 do resultado remontado. Atualizar o schema do conector; clientes antigos que ignorarem os novos metadados mostrarão só a primeira página em respostas grandes.
5. Se optar pelo desenvolvimento existente, portar apenas os deltas da correção preservando STATE_DIR, CODEX_HOME, allowlist do executável e restrições de ações. Não substituir seu config.py pelo arquivo de produção. Reexecutar a suíte nessa versão portada antes de considerá-la validada.

## Aplicação em produção — exige aprovação explícita e janela

Nenhum comando de serviço foi fornecido porque o gerenciador e os nomes das unidades não foram inspecionados. Identificar o modo real de inicialização e documentar os comandos exatos antes da janela. Não adivinhar unidades nem usar encerramento por padrões amplos de processos.

1. Com autorização específica, suspender novas submissões e aguardar a tarefa corrente terminar. Não matar uma execução em andamento para instalar o patch. Identificar e pausar somente os componentes da Bridge autorizados; confirmar ausência de workers ativos. Qualquer mudança/reinício desses componentes requer essa aprovação.
2. Criar um diretório de backup com permissões restritas e preservar os três módulos que serão substituídos, seus hashes, permissões e proprietários. Preservar config.py e .env existentes sem imprimir seu conteúdo. Não excluir backups antigos.
3. Fazer backup consistente do SQLite com a API `sqlite3.Connection.backup`, para um novo arquivo, usando uma conexão de origem em modo somente leitura. Não copiar apenas o arquivo .sqlite3 de uma base WAL ativa. Preservar o original e arquivos WAL/SHM; não apagar nenhum deles. Conferir integridade do backup com `PRAGMA integrity_check` no arquivo de backup.
4. Instalar somente worker.py, config.py e mcp_server.py corrigidos. Conferir que as configurações específicas da produção não mudaram desde a coleta. Fazer cópias temporárias no mesmo filesystem, preservar dono/permissões e substituir por rename enquanto todos os componentes da Bridge estiverem pausados. Os três arquivos devem estar consistentes antes de iniciar qualquer componente. Não instalar database.py: ele permanece inalterado. Não copiar source/ por cima do diretório inteiro.
5. Conferir os hashes dos arquivos instalados com os de source/ no pacote. Não há migração de banco. Atualizar o schema do conector MCP.
6. Somente após autorização, retomar os componentes identificados da Bridge. Não tocar no bot trader. Validar autenticação e conectividade sem registrar headers ou tokens.
7. Com autorização para teste real, usar uma tarefa sem efeitos que gere uma resposta >12000 bytes e <2 MiB, seguida por uma resposta curta. Confirmar completed, leitura integral paginada com hash e conclusão da segunda tarefa. A geração por modelo não garante tamanho exato: verificar o tamanho observado. Não alterar/reconciliar as tarefas históricas indicadas na missão.
8. Monitorar memória, disco, duração e estados da fila. Erros de timeout, teto, persistência ou encerramento precisam de análise; não reenfileirar automaticamente nem remover uncertain para liberar a fila.

## Rollback

1. Obter autorização para a janela de rollback e suspensão/retomada dos componentes. Suspender submissões, aguardar execução em andamento e pausar somente a Bridge conforme o procedimento confirmado.
2. Preservar uma nova cópia consistente do banco e dos arquivos corrigidos. Não restaurar um backup antigo de banco por padrão: isso perderia tarefas novas e poderia causar execução duplicada.
3. Restaurar exclusivamente os três módulos originais preservados na janela de implantação, com seus donos/permissões. Usar baseline/ apenas após conferir que coincide com a versão realmente substituída. Manter .env, banco, históricos, logs e backups atuais.
4. O schema SQLite não mudou, portanto o banco atual permanece utilizável pelo código anterior. Resultados grandes já persistidos devem ser preservados. A versão antiga volta a ter o teto de 12000 bytes para novas conclusões e retorna resultados sem paginação; planejar exportação/leitura local controlada dos resultados grandes antes de reverter o endpoint, se necessário.
5. Atualizar novamente o schema do cliente e retomar somente os componentes autorizados. Verificar a fila sem reexecutar tarefas concluídas. Não usar reconciliação automática para compensar falhas do rollout.

## Pendente

Esta entrega valida o processamento e persistência com dados fictícios. Permanecem pendentes a aprovação, identificação dos componentes de serviço, teste do cliente MCP real com o novo schema e o canário real pós-implantação. Nenhum desses passos foi declarado como já realizado.
