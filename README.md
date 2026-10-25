# LUK Codex Bridge — protótipo seguro

Esta é uma primeira base local da ponte. Ela **não se conecta ao ChatGPT**, não abre uma porta de rede e não acessa o repositório do bot principal. O protótipo aceita somente a tarefa fixa `self_test`, para validar fila SQLite e invocação controlada do Codex dentro do próprio diretório da ponte.

## Princípios

- Sem servidor HTTP, túnel ou porta de entrada nesta versão.
- Sem aceitar prompts arbitrários enviados pela rede.
- SQLite com identificador aleatório e estados persistentes.
- Lock exclusivo de worker no Linux, mantido durante recuperação e processamento para impedir concorrência entre workers.
- O worker é `dry-run` por padrão.
- A única ação executável é `self_test`, com instrução fixa e sandbox `read-only`.
- Diretório de trabalho fixo: diretório da própria ponte.
- O caminho do Codex deve corresponder ao caminho absoluto aprovado em `config.py`; a execução usa o descritor já validado.
- stdout e stderr têm limites em bytes durante a leitura, são drenados em paralelo e não são persistidos nem impressos integralmente.
- Sem retry automático de tarefa que falhou ou ficou com estado incerto. Tarefas que ficaram `running` após interrupção são marcadas `uncertain`.
- Não contém tokens, credenciais ou chaves.

## Requisitos

- Python 3.10+
- Codex CLI instalado e autenticado
- Caminho absoluto do executável Codex configurado via variável `CODEX_BIN`

## Instalação na VPS

1. Copie o conteúdo do pacote para `/home/ubuntu/luk_codex_bridge_v2`.
2. Inspecione os arquivos antes de executar.
3. Crie um ambiente virtual apenas se necessário; esta versão usa somente a biblioteca padrão do Python.
4. Execute os testes:
   `python3 -m unittest discover -s tests -v`
5. Inicialize o banco e crie tarefa de teste:
   `python3 bridge.py init`
   `python3 bridge.py submit-self-test`
6. Simule primeiro:
   `python3 worker.py --once`
7. Só depois de revisar os arquivos e a saída, execute explicitamente:
   `python3 worker.py --once --execute`

## Limites importantes

- Este pacote ainda não é uma ponte utilizável diretamente pelo ChatGPT.
- Não execute com `--execute` antes de revisar o código e confirmar o caminho de `CODEX_BIN`.
- A chamada define o diretório de trabalho e pede `--sandbox read-only`; isso, por si só, não prova que leitura, escrita ou rede estejam confinadas. O sandbox efetivo precisa ser validado separadamente.
- A validação do caminho impede substituição depois da abertura do executável, mas não consegue autenticar conteúdo trocado antes da abertura por outro processo com o mesmo usuário. Proteja o diretório do Codex fora deste protótipo.
- A execução real não usa o diretório do bot principal.
- Não instale serviço systemd nem configure inicialização automática nesta fase.
- Não configure rede, webhook, polling remoto ou MCP nesta fase.
- Não adicione ações genéricas de shell nem aceite comandos/prompt do banco de dados.
