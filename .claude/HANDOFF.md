# Invoice Sentinel — handoff

Última atualização: 2026-08-29 (quarta sessão). Substitui a versão anterior; o que
continuava válido foi incorporado aqui.

Versionado no repositório desde esta sessão: um clone novo sem ele começa do zero. Isso também
significa que **ele é público** — escreva pensando em quem avalia o projeto, não só na próxima
sessão.

---

## 1. O que é

Agente autônomo que audita faturas B2B de telecom, construído para o **All Things Agentic
Hackathon** (track *The Taskmaster*). Uma fatura em PDF entra; sai uma carta de contestação
verificada para a operadora e um resumo executivo para o cliente, sem humano no meio.

Repositório: `Bren0-lz/Agent-Google` · Serviço:
https://invoice-sentinel-474711060457.us-central1.run.app

Pipeline (`SequentialAgent`, Google ADK 2.7.1):

```
intake              o que a pessoa anexou, e de quem é
invoice_extractor   PDF -> schema canônico (Gemini 3.5 Flash multimodal)
auditor             SequentialAgent
  load_audit_context      contrato + histórico, do Firestore
  rule_engine             ParallelAgent, 3 famílias, 5 regras, Python puro
  merge_findings          supressão, flags de revisão, ranking
  audit_judgment          LlmAgent — o único lugar que decide algo
  persist_findings        Firestore
dispute_writer      dois documentos, toda cifra verificada
```

---

## 2. Estado atual

| | |
|---|---|
| Testes | **161 passando**, offline, sem credencial, ~1 s |
| Extração | 99,55 % (1781/1789) · 15/15 schema · 15/15 hashes · 0 reparos |
| Auditoria | 5/5 recall · 0 falsos positivos · **1036,10 exatos** |
| Produção | HTTP 200, revisão `invoice-sentinel-00017-v29`, rodando como `invoice-sentinel-run` |
| Git | `main` sincronizado com `origin/main`, árvore limpa |

Tudo o que está no repositório está no ar, e vice-versa.

---

## 3. O que as sessões recentes mudaram

A terceira sessão inteira foi dirigida por **teste no serviço público**, não por leitura de
código: cada defeito de 3.1 a 3.7 apareceu usando o agente pelo navegador como um avaliador
usaria.

### 3.1 Operadora nomeada não era conferida contra a fatura

Dizer `northwind` sobre uma fatura da Vantel era aceito e auditado sem aviso. Saía certo só
porque o `content_hash` acertava uma extração feita antes sob o perfil correto; um PDF inédito
teria sido lido com as dicas de separador americanas, que leem `1.234,56` como `1.234`.

Agora `ExtractorAgent._carrier_mismatch` compara a operadora impressa (via `profile_key_in`, não
por igualdade de string) com o perfil pedido, e recusa antes de persistir.

### 3.2 Cache do hash consultado depois de pagar a extração

`extract_invoice` já calculava o `content_hash` antes de montar o prompt, mas ninguém consultava
o store com ele: reenviar um PDF pagava uma extração inteira que `save_invoice` descartava como
`AlreadyExists`. O agente agora consulta antes; ordem é hash → cache → checagem de operadora →
persistir.

### 3.3 Atestado de saúde para fatura que não pôde ser auditada

Sem contrato, três das cinco regras são puladas, `list_findings` volta vazia, e o prompt mandava
dizer que a fatura "looks clean". Uma conta nova — o primeiro caminho que qualquer avaliador
percorre — recebia esse atestado; a fatura de teste tinha 5,00 de sobrepreço, que apareceram
assim que o contrato entrou. A regra de escalar por falta de contrato existia dez linhas abaixo
no mesmo prompt e era **inalcançável**: `escalate_for_review` pede um `finding_id`, e ali não há
finding nenhum. O vazio agora bifurca em `get_contract`.

### 3.4 Tributo "por dentro" contado como cobrança a mais

Uma fatura brasileira real foi transcrita corretamente e ainda assim reportada como 149,42 fora
de balanço: ICMS, PIS, COFINS, FUST e FUNTTEL são calculados por dentro e discriminados só por
exigência da Lei 12.741/2012. `ExtractionProfile.tax_inclusive_pricing` agora declara o regime
(BR embutido, US somado) e `consistency_warnings` respeita. **Os tributos continuam sendo
transcritos** — estão impressos na fatura.

### 3.5 Nomes de produto comparados por string exata

`orphan_addon` acusou um adicional legítimo porque a fatura escreve `Vantel Multi SIM – eSIM
adicional` e o contrato `Vantel Multi SIM (eSIM adicional na mesma linha)`. Agora
`contract.normalised_name` colapsa caixa, acentos e pontuação; quando nem isso casa, a regra
olha o preço antes de acusar (`addon_priced_at` + `names_agree`), e na dúvida marca
`needs_human_review` em vez de virar contestação.

### 3.6 O extrator de contrato inventou planos

Transcrevendo um contrato de 3 planos, o modelo arquivou 6 — cada um de novo sob o nome
abreviado que a fatura imprime, "ajudando" quem tivesse de casar os dois documentos. O contrato
é a base contra a qual todo dinheiro é calculado; passou a conter cláusulas que ninguém assinou.
`Contract._check_no_duplicate_plans` rejeita, e o loop de reparo corrige.

### 3.7 Usabilidade e entrega

- A carta e o resumo agora são **artifacts do ADK**, visíveis e baixáveis na aba Artifacts,
  persistidos em `gs://agent-hackton-artifacts`. Uma disputa `blocked` **não** é anexada.
- `deploy.ps1 -MinInstances` (default 1) elimina os ~16 s de cold start.
- `help_text()` mostra a mensagem que espera de volta, com exemplo — o detalhe que as pessoas
  erram é que o PDF e o nome da operadora vão na **mesma** mensagem.
- README alinhado com o comportamento real.

### 3.8 O teto de custo passou a viver no script (quarta sessão)

O commit `8ce037f` estava parado numa branch local: o `deploy.ps1` em `main` não tinha
`--max-instances` nem `--concurrency`, e o teto que o serviço exibia existia só porque o
`gcloud run deploy` preserva o que não é especificado — proteção por efeito colateral.
Mesclado com `--no-ff` e publicado. O conflito era de adjacência (`main` acrescentou
`ArtifactBucket` e `MinInstances` onde a branch acrescentava o teto); os **três** parâmetros de
escala passaram a viver num bloco só, e o preflight anuncia os três juntos, porque só os três
juntos dizem quanto o serviço pode custar.

Revisão `00016-x54` confere: `maxScale=2`, `minScale=1`, `containerConcurrency=8`, agora vindos
de flags explícitas e não de herança.

### 3.9 Segurança: três heranças por omissão (quarta sessão)

Auditoria de uma checklist de segurança sugerida numa sessão anterior, conferida contra o GCP e o
código em vez de presumida. Três itens já estavam de pé (UBLA, zero logging no pacote, Secret
Manager inaplicável — o agente não tem segredo). Um partia de premissa errada: **o PDF enviado
nunca é gravado**, então lifecycle no bucket raw apagaria o que não existe. `RAW_INVOICE_BUCKET`
era constante morta e foi removida.

Dois riscos que a checklist não previa, ambos confirmados:

- **`--trace_to_cloud` exportava a fatura transcrita.**
  `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` tem default `true`, e o ADK só o desliga sozinho em
  `to_agent_engine` (`cli_deploy.py:1156`), não em `to_cloud_run`. Os bytes do PDF não vazavam
  (`_summarize_inline_data`); conta, MSISDN e nome sim. Agora `false` no `$RuntimeEnv`.
- **Carta de contestação de terceiro era legível sem credencial**, sabendo só o UUID da sessão —
  o `user_id` da dev-ui é a constante `user` para todo mundo. Decisão do Breno: manter aberto
  para não travar avaliação, limitar com lifecycle de 30 dias e **declarar no README**.

Mais: o serviço rodava como a SA default do Compute, com `roles/editor` no projeto. Agora
`invoice-sentinel-run`, com objeto concedido no bucket e não no projeto; o script recusa deployar
sem ela. `public_access_prevention` passou a `enforced` nos dois buckets.

O README também afirmava Pub/Sub com dead-letter queue e Secret Manager com "least-privilege
service account". **Não existe tópico nenhum no projeto**, não há segredo, e a SA era editor.
Corrigido, e a seção `Security and privacy` foi escrita sobre os fatos apurados.

**Verificado nos spans reais.** A API v1 do Cloud Trace (`traces?view=COMPLETE`, janela de 3 dias)
devolve os spans do ADK; medindo o tamanho de `gcp.vertex.agent.llm_request` / `llm_response`,
a fronteira é limpa: **202 spans antes da correção com até 18.445 caracteres**, e um deles carrega
`orphan_addon:(11) 97412-3308` — o número de telefone, escrito por extenso no trace; **11 spans
depois, todos com `{}`**. O endpoint `/dev/apps/.../debug/trace/session/{id}` não existe na build
implantada (só `/dev-ui`), e buscar o trace por id do log do Cloud Run dá 404 — o caminho que
funciona é listar e filtrar.

---

## 4. Decisões de design que precisam ser respeitadas

### 4.1 Nenhum valor monetário sai de um LLM
Três camadas: motor de regras em Python puro com `Decimal`; nenhuma tool de auditor aceita valor
monetário como argumento (teste com `inspect.signature`); `amount_guard` confere cada cifra da
prosa contra o conjunto que o motor computou. Insistiu? A disputa é gravada como `blocked`,
nunca `draft`.

### 4.2 Recusar em vez de adivinhar
`profile_for` levanta em chave desconhecida; o extrator recusa fatura cuja operadora impressa não
é a nomeada; o agente diz que não pôde auditar quando não há contrato. **Silêncio de uma regra
que não rodou não é evidência de fatura correta** — foi o defeito 3.3, e é a regra geral.

### 4.3 Contestar ≠ otimizar
`zombie_line`, `plan_tier_mismatch` e `chronic_overage` são `optimise` (a operadora faturou
certo); `orphan_addon` e `rate_drift` são `dispute`. Vive em `anomaly.py` como `Remedy` /
`CARRIER_ERRORS`, não num prompt. `flag_anomaly` **recusa** disputar um finding `optimise`.

### 4.4 As famílias de regras não são LlmAgents
Nem o `intake`. Chamada de modelo cujo único trabalho é invocar uma função é token gasto à toa e
disfarça trabalho determinístico de raciocínio.

### 4.5 Defesa estrutural, não promessa em prompt
Quando algo precisa ser garantido, vira validator ou assinatura de função — o prompt no máximo
reforça. Foi o critério nas correções 3.5 e 3.6.

### 4.6 A regra da região
Infra em `us-central1`; Gemini no endpoint **`global`**. `GOOGLE_CLOUD_LOCATION=global` é
deliberado e **não deve ser "corrigido"**. Já custou uma hora.

---

## 5. Armadilhas descobertas (custaram tempo, não repita)

**Bytes não sobrevivem ao `state_delta`.** É serializado em JSON na resposta do `/run`. O PDF não
trafega pelo estado: intake e extractor chamam `split_attachments()` sobre a mesma mensagem.

**`ctx.end_invocation` não pula os irmãos de um `SequentialAgent`.** Cada sub-agente recebe o
contexto via `model_copy`. Para silenciar estágios, cada um verifica o estado e não emite evento.

**Um `LlmAgent` sempre chama o modelo.** Por isso `auditor._skip_judgment_without_an_invoice` é
um `before_agent_callback` que pula a execução inteira.

**O cache por `content_hash` invalida teste de extração.** Depois da correção 3.2, reenviar o
mesmo PDF devolve a extração antiga sem chamar o modelo. Para testar extração de verdade,
**regere o PDF** (o reportlab embute timestamp, então o hash muda com o mesmo conteúdo).

**A dev-ui do ADK renderiza Markdown.** Uma linha indentada depois de uma lista é engolida como
continuação do último bullet. Texto do agente precisa ser escrito pensando nisso.

**O Chrome traduz a dev-ui automaticamente.** Para conferir o texto real que o agente produziu,
puxe pela API (`GET /apps/invoice_sentinel/users/{u}/sessions/{s}`), não pelo screenshot.

**O preview de artifact na dev-ui mostra mojibake.** Os bytes estão corretos em UTF-8 — verificado
baixando pela API. É defeito de exibição do ADK 2.7.1; não "conserte" gravando outro encoding.

**O modal de telemetria do ADK reaparece e engole o primeiro envio.** Feche-o antes de anexar.

**Aspas duplas em mensagem de commit quebram o Bash.** Use `git commit -F -` com heredoc.

**`SERVICE_DISABLED` não quer dizer "não existe".** `gcloud billing budgets list` falha com texto
que *parece* dizer falta de permissão quando a API está desabilitada. Habilite antes de concluir
qualquer coisa da resposta. Já está habilitada em `agent-hackton`.

**`gcloud auth login` não cria ADC.** Para rodar Gemini localmente:
`gcloud auth application-default login`. Sem isso, só dá para testar em produção.

**No PowerShell 5.1, não use `2>&1` ao chamar `deploy.ps1`** — o stderr do gcloud vira ErrorRecord
e o `$ErrorActionPreference='Stop'` aborta o deploy.

**O `deploy.ps1` termina com `Deploy failed: [WinError 5] ... __pycache__`.** É só a limpeza da
pasta temporária, **depois** do deploy ter sucedido. Confie na verificação de revisão do script.

**O grafo do agente não renderiza na UI** (`Error fetching graphs` no console). O pacote
`graphviz` vem com o `google-adk`, mas falta o binário `dot` na imagem. Consertar exige assumir o
Dockerfile, que hoje o `adk deploy cloud_run` gera sozinho. `assets/architecture.svg` cobre a
necessidade a custo muito menor.

---

## 6. Como as coisas são feitas aqui

### Testes
Offline, sem token, sem credencial. `FakeClient` em `tests/test_extractor.py` é importado pelos
outros arquivos. Sem `pytest-asyncio`: use `asyncio.run()`. Contextos de agente são
`SimpleNamespace` com só os atributos que o estágio lê — montar um `InvocationContext` real
testaria o ADK, não o código.

`tests/test_upload_flow.py` roda o `root_agent` inteiro num `InMemoryRunner` real, offline.

### Comandos
```bash
.venv\Scripts\python.exe -m pytest                                        # 161, ~1s
.venv\Scripts\python.exe -m scripts.eval_extraction --cache data/extracted
.venv\Scripts\python.exe -m scripts.eval_audit      --cache data/extracted
.venv\Scripts\python.exe -m scripts.eval_audit      --ground-truth   # isola bug de regra
.\deploy.ps1                                                          # sem 2>&1
```

### Git
**Nunca incluir atribuição de IA** em commits ou PRs — sem `Co-Authored-By`, sem "Generated
with". Pedido explícito e repetido: o repositório é avaliado como trabalho do Breno.

Mensagens em pt-BR sem acento, minúsculas, explicando **por que** e não só o quê. Commits
temáticos, um assunto cada.

### Regenerar o dataset
**Não.** Os `content_hash` são as chaves do Firestore e a âncora de toda métrica publicada.

---

## 7. O que falta

### 7.1 Dados de teste em produção

Ficaram no Firestore, para limpar quando o projeto fechar:

| Conta | O que tem |
|---|---|
| `ACC-BR-9001` | contrato, fatura, anomalia, disputa |
| `ACC-BR-9002` | fatura, sem contrato |
| `4.812.663-5` | contrato + fatura da Meridiano (documentos realistas) |

Mais os artifacts correspondentes em `gs://agent-hackton-artifacts`. Nenhuma delas colide com o
dataset (`ACC-BR-1041`, `2087`, `3312`, `ACC-US-77120`), então as métricas não são afetadas.

### 7.2 Acesso público

Roda `--allow-unauthenticated`, e qualquer visitante que anexe um PDF vira chamada ao Gemini com
billing ativo. Pior que o custo: quem tem o UUID da sessão baixa a carta de contestação dela sem
credencial (verificado). Mitigado, não resolvido, pelo lifecycle de 30 dias no bucket de
artifacts. Manter aberto foi decisão consciente enquanto o julgamento corre — um avaliador
precisa abrir a URL. Quando terminar, fechar é um comando reversível:

```bash
gcloud run services remove-iam-policy-binding invoice-sentinel \
  --member=allUsers --role=roles/run.invoker
```

Sem kill switch automático, deliberadamente: ele derrubaria o serviço sozinho, possivelmente no
meio de uma avaliação, e foi billing desabilitado que causou um 503 numa sessão anterior. O
orçamento de R$ 50 alerta, e quem recebe o alerta é o dono do projeto, com `roles/billing.admin`.

Nota de privacidade que ninguém avaliou ainda: **faturas telefônicas carregam dado pessoal**, e o
serviço aceita upload de estranhos.

### 7.3 Limitações conscientes

**Só duas operadoras.** Vivo, Claro ou AT&T são recusadas — corretamente, e o agente diz isso.
Mas "qualquer um pode usar" ainda quer dizer "com Vantel ou Northwind".

**Dataset sintético.** Nenhum dado real de cliente. Os números de extração descrevem dois
templates bem-comportados, não a cauda longa de PDFs reais.

**Oito erros conhecidos de extração:** `quantity`/`unit_amount` na seção de taxas do template
americano, que só imprime Descrição e Valor. Documentados como `UNPRINTED_FIELDS` e
deliberadamente não mascarados. Não movem um único achado.

**Os PDFs enviados não são guardados.** `upload://<sha256>` preserva o hash, não os bytes: um
documento auditado não existe mais em lugar nenhum depois do run.

---

## 8. Sugestões para o futuro

Em ordem de retorno sobre esforço.

**1. Repetir a técnica de teste sem viés.** A descoberta mais produtiva desta sessão foi pedir a
um agente sem nenhum conhecimento do projeto que pesquisasse faturas brasileiras reais e montasse
uma. Três defeitos apareceram de uma vez, todos invisíveis para o dataset — porque toda fatura do
dataset sai do mesmo gerador. Vale transformar isso em rotina: uma fatura nova, de origem
independente, a cada mudança relevante. Os documentos usados estão em
`C:/Users/breno/AppData/Local/Temp/vantel-docs/` (fora do repositório) e valeria versioná-los
como fixture de regressão.

**2. Detecção automática entre os perfis conhecidos.** Hoje a pessoa precisa nomear a operadora.
O extrator já sabe ler o nome impresso — a mesma máquina da correção 3.1 poderia *escolher* o
perfil em vez de só conferi-lo, recusando o que não reconhece. Preserva o princípio de recusar em
vez de adivinhar e remove o passo que mais confunde quem chega.

**3. Suíte de regressão com documentos adversariais.** Casos que já se sabe difíceis: ciclo
quebrado (26/07–25/08), identificadores em formato real (`(11) 97412-3308`, `4.812.663-5`),
crédito negativo, tributos discriminados, desconto por linha com arredondamento. Hoje isso só é
testado à mão, no navegador.

**4. Guardar o PDF auditado no bucket.** Rastreabilidade: hoje não há como reconferir uma
auditoria contra o documento que a originou.

**5. Um perfil genérico.** Destravaria qualquer operadora ao custo de afrouxar a garantia que o
README defende como princípio. Só depois da sugestão 2, e com o cuidado de manter a recusa como
padrão.

**6. Histórico real de múltiplos ciclos numa conta nova.** As três regras `optimise` exigem
`PATTERN_CYCLES` (3) ciclos e nunca foram exercitadas com documentos de origem independente — só
com o dataset.

---

## 9. Onde os arquivos importantes estão

```
invoice_sentinel/
  config.py               model id, regiões, coleções, limiares
  intake.py               porta de entrada, help_text, profile_key_in
  extractor.py            InvoiceSource, prompt, generate_validated (loop de reparo)
  extractor_agent.py      cache por hash + checagem de operadora
  contract_extractor.py   contrato assinado -> Contract
  contract.py             Contract, normalised_name, names_agree, lookups
  auditor.py              grafo do auditor, nothing_was_extracted, prompt do julgamento
  amount_guard.py         verifica cada cifra da prosa gerada
  anomaly.py              Remedy / CARRIER_ERRORS — verdade de domínio
  dispute_writer.py       dois documentos + artifacts
  schema.py               ExtractionProfile, ChargeCategory, consistency_warnings
  rules/conformance.py    orphan_addon e rate_drift
  rules/                  5 regras, 3 famílias, zero LLM
tests/                    161 testes, todos offline
scripts/                  dev-only, nunca entra no container
data/synthetic/           15 PDFs, 4 contratos, ground_truth.json
data/extracted/           15 extrações em cache — a evidência offline
```

`invoice_sentinel/requirements.txt` é **só runtime**. **Nada em `scripts/` pode ser importado por
`invoice_sentinel/`** — essa pasta não existe dentro do container.

---

## 10. Contexto humano

Breno trabalha numa consultoria B2B de telecom que audita contas telefônicas de pequenas e médias
empresas. O projeto existe porque esse trabalho é feito à mão, não escala, e é o primeiro a ser
pulado quando o mês aperta — que é exatamente quando o dinheiro vaza.

Ele prefere **ser avisado do problema real** a receber um relatório otimista. Todos os defeitos
da seção 3 apareceram porque ele pediu para testar no serviço público, com documentos que
ninguém preparou sabendo o que seria avaliado. Vale continuar testando assim — e vale dizer
quando algo saiu certo por acidente, porque foi o que aconteceu em 3.1 e teria passado batido.
