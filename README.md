# Jataí

**Agente local de pesquisa científica** — descoberta, aquisição,
triagem, análise profunda e catalogação de publicações, com LLMs
locais (Ollama) ou provedores em nuvem (Google Gemini, OpenRouter).
Tudo auditável, configurável e reproduzível.

> Jataí permite criar pipelines de pesquisa científica:
> gerar consultas, buscar em 12 fontes bibliográficas,
> deduplicar, fazer triagem título/resumo e texto integral com auxílio de IA,
> baixar e analisar PDFs e classificar as evidências — registrando cada
> decisão, prompt e resposta bruta das APIs.

---

## Autor e contribuições

**Autor:** Tarlis Portela

---

## Funcionalidades principais:

- Arquitetura do agente e pipeline de tarefas configurável
- Conectores bibliográficos (DBLP, OpenAlex, Crossref, arXiv, PubMed,
  Semantic Scholar, CORE, Scopus, IEEE, ScienceDirect, SciELO, ACM)
- Triagem estruturada por critérios com evidências, deduplicação
  assistida e análise profunda de PDFs
- Auditoria completa: JSON bruto das APIs, histórico de
  chamadas LLM com prompts renderizados, eventos de execução e modificações manuais
- Provedores de LLM locais e em nuvem (Ollama, Gemini, OpenRouter)
- Interface web e Jataí Manager (app desktop
  de instalação e gerenciamento)

---

## Requisitos

- **Python ≥ 3.11**
- ~500 MB de disco (deps + browsers do Playwright sob demanda)
- Opcional: **Ollama** para modelos locais; chaves de API para Gemini,
  OpenRouter ou Semantic Scholar (ou para outras fontes de publicações)

---

## Instalação

### Opção 1 — Jataí Manager (app desktop para iniciantes)

Baixe o instalador da sua plataforma (Mac OS `.app`, Windows `.exe` ou Linux `.deb`)

O app verifica o Python do sistema, cria um venv em
`~/.jatai/venv` (ou diretório escolhido pelo usuário), 
instala o pacote e permite iniciar/parar o servidor e
configurar a pasta de trabalho — veja `manager/README.md`.

### Opção 2 — pip (recomendado para uso de especialistas)

```bash
pip install jatai
```

#### Executar via linha de comando

```bash
jatai init                    # cria ~/.jatai
jatai start                   # http://127.0.0.1:8000
```
#### CLI (guia de comandos)

| Comando | Descrição |
|---|---|
| `jatai start [--host H] [--port P]` | Sobe o servidor web local |
| `jatai new "Nome" [--description …]` | Cria um projeto |
| `jatai init` | Cria `~/.jatai` (ou `$JATAI_HOME`) |
| `jatai pdf-login [--url …]` | Abre navegador p/ login institucional (aquisição de PDFs) |

`JATAI_HOME` define o diretório base (padrão `~/.jatai`) — usado pelo
Jataí Manager para apontar a pasta de trabalho.

### Opção 3 — para desenvolvedores que desejam extender ou modificar o código

```bash
git clone <repo> && cd jatai
python3.12 -m venv .venv
.venv/bin/pip install -e .          # pacote + dependências
.venv/bin/pip install playwright && .venv/bin/playwright install chromium  # fontes anti-bot
```

---

## Primeiros passos

Depois, pela interface web:

1. **Pesquisa → Configuração** — áreas, palavras-chave, questões de
   pesquisa, critérios de inclusão/exclusão e período de cobertura.
2. **Pesquisa → Fontes** — habilite as fontes; configure API keys
   (Semantic Scholar exige; as demais são opcionais). Cada fonte tem
   botão ↗ para a página oficial.
3. **Pesquisa → Consultas** — gere a partir da configuração ou escreva
   expressões; ordene com ↑/↓ (a ordem da tabela é a de execução).
4. **Agente → Modelos** — cadastre provedores (Ollama local, Gemini,
   OpenRouter) e sincronize os modelos disponíveis.
5. **Agente → Pipeline** — ative/ordene as tarefas do agente.
6. **Agente → Execuções** — inicie uma nova execução e acompanhe os
   eventos em tempo real (progresso por página, retentativas, erros).

Fluxo de uma execução: **geração de consultas → busca nas fontes →
deduplicação → triagem título/resumo → aquisição de PDF → triagem
profunda → análise → classificação → conceitos**.

---

## Como contribuir

Contribuições são muito bem-vindas! Seja reportando bugs, sugerindo funcionalidades, aprimorando a documentação ou enviando código, toda ajuda fortalece o desenvolvimento do **Jataí**.

### Formas de Contribuir

* **Reportar bugs:** Abra uma *issue* descrevendo o problema, os passos para reproduzi-lo, o comportamento esperado e a versão do seu sistema operacional e do Python.
* **Sugerir melhorias:** Abra uma *issue* explicando o problema que você quer resolver e como a nova funcionalidade ajudaria no uso diário.
* **Enviar código ou documentação:** Faça um *fork* do projeto e envie um *Pull Request* com suas alterações.

### Configurando o Ambiente Local

1. **Faça um fork** deste repositório e clone a sua cópia:
   ```bash
   git clone [https://github.com/seu-usuario/jatai.git](https://github.com/seu-usuario/jatai.git)
   cd jatai
   ```

2. **Crie o ambiente virtual e instale as dependências de desenvolvimento** (recomendamos o [uv](https://docs.astral.sh/uv/), mas `pip` também funciona):
   ```bash
   # Com uv
   uv sync --all-extras

   # Ou com venv + pip padrão
   python -m venv .venv
   source .venv/bin/activate  # No Windows: .venv\Scripts\activate
   pip install -e ".[dev]"
   ```

   Ou rodar localmente:
   ```bash
   jatai init && jatai start
   # Em modo dev:
   # python -m jatai.cli start
   ```

3. **Execute os testes e verificações de estilo** para garantir que o ambiente está funcionando:
   ```bash
   pytest
   ruff check .
   ```

   Há uma suíte de testes cobrindo conectores (com dublês HTTP), execução de consultas,
   triagem, deduplicação, ordenação de consultas e persistência de buscas.

### Fluxo de Pull Request

1. Crie uma *branch* descritiva a partir da `main`:
   ```bash
   git checkout -b feat/nome-da-funcionalidade
   # ou: fix/descricao-do-bug
   ```
2. Implemente suas alterações e adicione testes unitários ou de integração para cobrir o novo comportamento.
3. Verifique se todos os testes passam (`pytest`) e se a formatação está alinhada ao projeto (`ruff format --check .`).
4. Faça *commits* claros e objetivos descrevendo o motivo das mudanças.
5. Envie a *branch* para o seu *fork* e abra um **Pull Request** detalhando o que foi alterado e como testar.

### Gerando Jataí Manager

Para gerar o executável do Jataí Manager, siga os passos abaixo:

```bash
# Gerar executável para Windows
./manager/build-win.ps1

# Gerar executável para macOS
./manager/build-mac.sh

# Gerar executável para Linux
./manager/build-linux.sh
```

### Boas Práticas

* Mantenha cada *Pull Request* focado em uma única correção ou funcionalidade para agilizar a revisão.
* Atualize a documentação caso sua alteração modifique comandos de CLI, variáveis de ambiente ou endpoints da API.

---

## Arquitetura do Sistema

```
jatai/
  cli.py            # jatai start|new|init|pdf-login
  app.py            # FastAPI: rotas web e páginas Jinja/HTMX
  agents/           # runner de tarefas, triagem, análise, classificação
  catalog/          # ingestão, dedup de registros, proveniência, decisões
  dedup/            # normalização e similaridade de títulos/DOIs
  documents/        # aquisição e parsing de PDFs
  exports/          # CSV, BibTeX, RIS, PRISMA
  research/         # configuração do projeto (áreas, critérios, fontes…)
  search/           # DSL de consultas + adaptadores por fonte
  sources/          # conectores bibliográficos (Strategy pattern)
  storage/          # modelos SQLAlchemy (um banco SQLite por projeto)
  web/              # templates Jinja + CSS/JS estáticos
manager/            # Jataí Manager: app desktop de instalação/gerência
```

### Conectores (`jatai/sources/`)

Cada fonte implementa `SourceConnector` (`search`, `is_available`,
`requires_api_key`) e delega a compilação da expressão ao seu
adaptador em `jatai/search/adapters/`. Todos suportam paginação com
`on_progress`/`on_retry`/`on_page`/`should_stop`, preservação de
parciais e coleta de payloads brutos para auditoria.
`get_connector(source_type, config)` instancia o conector repassando
apenas as chaves aceitas (ano, API key, mailto, sort…).

### Dados

- **`$JATAI_HOME/`** (`~/.jatai` por padrão) — configuração global,
  `projects/<slug>/` com o SQLite de cada projeto.
- **`<projeto>/searches/`** — JSON de cada consulta executada.
- **`<projeto>/pdfs|extracted|index/`** — documentos e derivados.

---

## Licença

Projeto em versão Beta 0.1.0. GNU GPL v3.0
