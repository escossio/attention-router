# Pré-certificação D06-A/D08-A — contratos isolados

Referência: `andy-whatsapp-semantic-conversation-pilot-v1.md`, §§3,5–10; diretrizes arquiteturais D06-A/D08-A aprovadas. Perfil de minimização aprovado em 17/11/2026, com preservação de memória histórica em meses/anos. Publicação saneada em PR draft autorizada exclusivamente para revisão e CI. Nenhum prazo numérico, tratamento pessoal ou implantação aprovado.

## Incremento sobre A+B

A árvore A+B é preservada. Este incremento não modifica STT, Transport, Observer, MAESTRO, Artifact, flags ou migrações. D01 continua atribuição semântica independente de autoridade. Continuidade ao vivo permanece no pipeline existente; aceleração histórica continua separada, sem importação/recaptura real.

`semantic_retention.RetentionSchedule` recebe regras explícitas por classe/finalidade, tenant, conta, titular, versão, validade e referência de decisão. Não há prazo padrão. Ausência de regra, consentimento falso, escopo divergente, data sem fuso, expiração e TTL inválido bloqueiam. Fixtures são SYNTHETIC. Os prazos das evidências dependentes podem restringir uma derivação. **Prazo de armazenamento de áudio não é automaticamente prazo de validade da transcrição:** as autorizações de transcrição/arquivo são independentes. O contrato não agenda purgas nem interpreta uma referência de decisão como consentimento autenticado.

A integração com o laboratório conserva `LabGrant` e seus limites conservadores. A configuração por objeto/finalidade prepara uma futura política operacional; não converte o laboratório em um serviço produtivo nem escolhe um armazenamento de consentimento. Regras reais precisarão de assinatura/revisão, instalação explícita e migração de consumidores.

## Journal e âncora

`semantic_policy_anchor.AnchoredPolicyJournal` reutiliza `PolicyJournal`. Ordem sob lock: escrever payload preparado + fsync; CAS de revisão/digest na autoridade independente; publicar payload + fsync. Leitura exige revisão **e** digest atuais. Restore antigo, âncora perdida/corrompida e CAS conflitante bloqueiam. Falha após CAS permite apenas roll-forward do payload preparado com digest exato. Falha anterior ao CAS não confirma revogação; o chamador deve manter o pedido pendente e repetir. Não declarar exclusão/revogação concluída se a autoridade estiver indisponível.

`RevisionAuthority` é o contrato do armazenamento autoritativo. `SyntheticFileAuthority` é exclusivamente um adaptador de teste. Diretórios distintos demonstram separação lógica, **não** separação física, WORM, criptografia ou resistência a administrador. Implantação precisa de um domínio de restore independente, autenticação de escritor, política de backup da própria âncora e prova de monotonicidade. Restaurar também a autoridade pode reintroduzir dados: este gate permanece BLOCKED.

Nenhum tombstone registra conteúdo ou credenciais. Os journals existentes mantêm IDs/decisões mínimas. Tombstone não deve expirar antes da última cópia restaurável e dos consumidores de replay; período exato depende do inventário de backups. DELETE_DERIVATIVES continua distinto de HIDE, REJECT, REVOKE e PAUSE. VERIFY_DELETION enumera resíduos e nunca afirma exclusão física total sem prova.

## Consumidores

`GovernedSemanticLab.consumer_read` revalida ClientSession/OWNER, política e fontes dentro do lock de revogação; não exporta uma autorização reutilizável em cache. Reutiliza:

- `memory.search_archive`: autorização antes de pontuar conteúdo;
- `memory.search_memory`, `memory.memory_context`, `memory.get_memory_evidence`: autorização por claim;
- `personal_context.build_personal_context` e `context_retrieval.retrieve_personal_context`: claims/fatos autorizados antes do ranking;
- `cognitive_graph.build_cognitive_graph_slice`: remove nós e arestas incidentes antes de devolver a projeção;
- `context_compiler`: mantém o filtro já existente antes de seeds/travessia e sem fallback não autorizado.

Claims/fatos legados sem fechamento de evidências governado são excluídos, mesmo que ACTIVE. A existência de uma linha canônica não demonstra consentimento. Relações indiretas não ampliam o conjunto autorizado. As APIs antigas continuam compatíveis fora dessa fachada: **não estão certificadas como consumidores D06**. O runtime orgânico, recomendações, sugestões, materializações e ferramentas antigas precisam receber esse escopo e provar closure de fontes antes da ativação. Consumidor não registrado falha fechado na fachada. Não iniciar ciclo que possa criar ações para testar um leitor.

## Revisão Client API / SDK

`api/v1/semantic_review_preview.py` oferece um router **não registrado em web.app**. Testes constroem uma aplicação isolada. Caminho proposto: `/api/v1/client/semantic-preview`.

| Método | Sufixo | Contrato |
|---|---|---|
| GET | `/candidates` | Candidatos com Interpretation tipada, evidências SourceEvidence e estado não canônico |
| GET | `/history` | Revisões e operações com IDs/estados; sem conteúdo da conversa |
| POST | `/review` | candidate_id, action, decision_ref; replacement_id em CORRECT/SUPERSEDE |
| POST | `/operations` | LabOperation: PAUSE/REVOKE/HIDE/REJECT/DELETE/EXPORT/VERIFY distintos |
| POST | `/context` | Query no corpo, grafo, evidências, decisões, validade e grants_authority=false |

ClientSession Bearer existente, titular único, tenant/conta obtidos do grant autenticado; não aceitos do cliente. Replay reautenticado. Erros 401/409/503 sem detalhes privados; no-store. ADMITTED não é verdade; PROMOTE não existe. Exportação revalida acesso e não guarda conteúdo no recibo. Schema de prévia separado do OpenAPI produtivo, sem alegar presença em APK.

SDK Android: reutilizar `sdk/client-api`, `ClientSessionCredential`, transporte HTTPS existente, serialização e testes MockWebServer. A fronteira Contacts autoriza somente seus dois arquivos. Uma classe `AttentionRouterSemanticReviewClient.kt` e seu teste exigem **FRONTIER_EXPANSION_REQUIRED**, com governança independente. Não modificar architecture-guard nem incluir a classe em Contacts para contornar o limite. Proposta de fronteira no pacote privado; implementação Android dessa nova classe permanece bloqueada até autorização específica. Contacts existente foi auditado sem mudança.

## Segurança e aceitação

Testes sintéticos cobrem CAS, restore, perda/corrupção, falha parcial, prazos/classes/escopos, leitores, API autenticada real e revogação de sessão. PostgreSQL descartável prova ciclo da API/âncora e cadeia 0061→0062 Artifact→0063 GPS. Regressões relacionadas e duas provas bilaterais reutilizadas. Os totais e SHAs ficam no pacote de evidências; não equivalem a CI formal.

Nenhuma aprovação arquitetural implica consentimento pessoal. Promoção canônica permanece desabilitada. O schema 0064 de consentimento GPS não foi integrado. Configuração de produção e caminhos de entrega permanecem intactos.
