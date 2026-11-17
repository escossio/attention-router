# Interpretação estruturada → CandidateInsight: contrato de laboratório V1

Referências obrigatórias: piloto WhatsApp V1 (PR409, c318f6b), desenho linguístico de revisão, Personal Context V2A/C/D/E/F/J. Esta proposta contratual não altera aprovações D06/D08 nem habilita runtime pessoal.

## Fronteiras implementadas

`SourceEvidence → Interpretation → validate_interpretation → propose_interpretation → CandidateInsight V2E → decisão do proprietário → compile_graph_context V2F + candidatos separados`.

`semantic_interpretation.py` é um adaptador offline, não um segundo motor ou armazenamento. O extrator `memory.preview_event_candidate` continua baseline. O núcleo V2E, modelos de persistência, grafo, compilador, resolver de identidade e runtime não foram substituídos. Não há rota, worker, flag produtiva, migração, chave ou chamada externa nova.

SourceEvidence conserva tenant/conta, autor/base, origem, horário, texto, modalidade, transcrição e vínculo de acontecimento. Interpretation separa ação, polaridade, citação, negação, participantes referenciados, horário da mensagem, data/intervalo do acontecimento, zona, incerteza, referências e spans exatos, contradições, confiança/racional, estado, mecanismo e versão. JSON adicional é recusado; tipos/enums e tamanhos são limitados.

O validador relê o arquivo antes de persistir; rejeita fontes ausentes, outro tenant/conta, autor incompatível, participante sem fonte, origem automatizada, autor incerto, SECRET, spans adulterados, reply forjado, evento ou horário inventados e data sem âncora suportada. Pergunta, citação, negação, ironia reconhecida e ambiguidade STT não podem ser promovidas pelo modelo a confirmação. Ciência exige evidência do próprio declarante. Validação estrutural não comprova verdade semântica.

A interface OfflineInterpreter não implementa rede. O adaptador, e não o texto gerado, fornece engine/version. Um mock LLM pode propor uma interpretação para texto não coberto pelo baseline, desde que passe o contrato e os filtros explícitos; isso entra em NEEDS_REVIEW. Não se afirma que os filtros detectem toda ironia/injeção possível. Inferência probabilística nunca vira fato por confiança. Valores de confiança da regra são heurísticos, não probabilidades calibradas.

## Linguagem e acontecimentos

A camada distingue proposta, contraproposta, mudança, confirmação, cancelamento, concordância/discordância, negação, pergunta, resposta, relato de terceiro e ciência. “Tô dentro”, “fechou”, “bora” e “não vai rolar” têm cobertura sintética. Anáfora sem referência, possibilidade, ironia reconhecida e trecho inaudível permanecem UNRESOLVED. “Sim”/“não” sem reply permanece não resolvido; com reply conserva a referência e não presume o conteúdo respondido. Citações conservam o autor do relato; não certificam a declaração da pessoa citada.

Intervalos ISO e “daqui a duas semanas” são ancorados explicitamente. Datas relativas usam horário do provedor/fuso. “Outro domingo” não ganha uma semana inventada. Cenários repetidos usam IDs distintos de acontecimento; não se fundem por palavras/data. O projetor cronológico mantém todos os estados, inclusive cancelamento posterior e contradição pendente. Associação entre conversas continua no contrato de prévia anterior: hipótese não equivale a LINK revisado.

## V2E, correções e V2F

A admissão deste adaptador é restrita a mensagens arquivadas com `platform_lineage.classification=SYNTHETIC`. Exige política explícita de laboratório, consentimento, finalidade, expiração e ausência de tombstone. RULE permanece PROPOSED; LLM/EMBEDDING ou contradição entram NEEDS_REVIEW, usando o serviço V2E existente. ADMITTED significa elegível para promoção futura, nunca FactRow/RelationshipRow. Nenhuma função do adaptador cria essas linhas ou autoridade/outbox.

O predicado deriva de conta + referência do acontecimento (ou mensagem não vinculada); a chave V2E acrescenta tenant/ator. Rejeição/supersessão do mesmo conjunto de evidências bloqueia nova proposta, inclusive por nova versão do parser. Correção efetiva exige evidência nova e revisão, não reapresentação automática do dado rejeitado.

O compilador V2F continua recuperando o grafo existente com caminhos, limites e proveniência. O wrapper devolve fontes sintéticas da conta e candidatos associados em campos separados. Não adiciona um nó de “verdade” para CandidateInsight. A demonstração associa mensagens a um episódio por decisão sintética explícita, usando os modelos V2D existentes; interpretação por si só não cria episódio canônico.

## Retenção, exclusão e restauração — proposta, não aprovação D06

| Objeto | Contrato demonstrado | Pendência produtiva |
|---|---|---|
| Política | Finalidade SYNTHETIC_EVALUATION, consentimento explícito, expires_at obrigatório; uma hora somente nas fixtures | Prazo e finalidade reais, base de consentimento e autoridade operacional |
| Candidatos | Expirados/rejeitados/superseded não são oferecidos como contexto elegível; estado preservado até purga autorizada | Agendamento e retenção/auditoria pessoal |
| Derivados | Purga física de CandidateInsight/evidências sintéticos cobertos por tombstone; lineage de correção exige escopo completo | Cascata abrangente de todos os derivados reais |
| Arquivo, áudios, episódios | Não apagados por esta operação de derivados | Política separada de remoção/exportação; privado não é apagado |
| Cache | Não foi criado cache novo; reuso exige fingerprint da política, prazo e releitura do estado dos candidatos; correção invalida | Invalidação distribuída e todos os consumidores |
| Backup | Restore barrier mantém união dos tombstones, consentimento mais restritivo e menor prazo; backup antigo não relaxa decisão atual | Cofre durável de tombstones externo ao backup, procedimentos de restauração e purga de backups reais |

A barreira de restauração é um contrato testado com estado sintético. Não se afirma ter purgado backups ou mídias reais. O chamador deve conservar a política corrente fora do snapshot restaurado antes de reabrir consultas. Sem essa política corrente não existe garantia demonstrada contra ressurgimento; operação pessoal permanece bloqueada.

## Avaliação e IA existente

Corpus versionado: `tests/fixtures/bilateral/language-corpus-v1.json`, 26 casos de desenvolvimento. Resultados por caso e latência são gerados pelos testes em diretórios temporários. Comparação de rótulos exatos: baseline 6/26, camada ampliada 26/26; 7 respostas mantidas não resolvidas. A taxonomia ampliada distingue classes que o baseline agrupava, portanto estes números não demonstram superioridade geral. Não há holdout independente, calibração, estimativa populacional nem certificação V2J. Autoria e recuperação de IDs são asserções por caso; precisão de associação de eventos fica sem estimativa, pois os cenários são poucos e construídos. Custo de fornecedor e chamadas externas: zero; custo de CPU não é zero e latência consta em nanossegundos por caso.

Inventário consultado: `artifact_understanding.OpenAIArtifactUnderstandingProvider`, `ai_interviewer.OpenAIConfigurationInterviewer`, `real_pilot.OpenAIPilotProvider` já oferecem integrações de modelo, mas seus escopos não autorizam ler conversas privadas neste adaptador. `cognitive_runtime` usa grafo ORGANIC e candidatos determinísticos. V2J trata readiness e mantém aprendizado OFF/ACTIVE proibido; não é implementação de embeddings ou GNN para esta tarefa. Nenhum desses providers foi chamado ou reconfigurado.

## Reprodução e demonstrações

- `tests/test_candidate_insights.py`: corpus, output mock, validação adversarial, ciclo V2E/grafo, correção, retenção, cache e exclusão.
- `tests/integration/test_postgres_candidate_insight_migration.py`: mesmos ciclos em PostgreSQL descartável, além dos testes existentes de esquema/downgrade.
- `tests/test_bilateral_integrated.py[natural]`: captura/STT/arquivo e três conversas; rematerialização **somente de fixture** em arquivo SYNTHETIC para o adaptador offline. A captura ao vivo admite ORGANIC por contrato e foi preservada; fontes originais não são adulteradas. A cópia sintética registra o ID original e mantém autoria/STT/proveniência.
- `andy-language-integrated-demo.json`, `andy-language-governance-demo.json` e `andy-language-multiday-demo.json`: pipeline completo, caminhos do grafo, candidatos, decisões e evolução cronológica.

Progresso funcional desta fase é offline e demonstrável. Qualidade de linguagem irrestrita, política pessoal D06/D08, fornecedor real, integração com main/gates formais e implantação continuam fronteiras independentes; nenhuma foi presumida autorizada.
