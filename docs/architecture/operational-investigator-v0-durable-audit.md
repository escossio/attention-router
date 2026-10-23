# Investigador V0 — trilha de auditoria durável e store segregado

**Estado:** PROPOSED. **Entidade:** Investigador V0; **armazenamento candidato:** namespace próprio no cluster PostgreSQL já existente, mediante aprovação posterior. Nenhuma migração ou role real é criada por este documento.

## Objetivo

Cada investigação precisa ser reproduzível e responsabilizável inclusive quando um conector falha, o agente é interrompido ou o runtime não retorna. O `MemoryAudit` e o `AuditSink.record` existentes são prova estrutural offline, **não** auditoria durável ou imutável.

## Fronteiras transacionais planejadas

1. **Admissão:** credencial/tenant/source validados por autoridade externa ao modelo; inserir de modo atômico seed, hash do original, idempotency binding, nonce/replay admission e estado `CREATED`, antes de entregar o evento ao agente.
2. **Consultas:** inserir intenção de ferramenta (`invocation_id`, ação, escopo, fingerprint sanitizado, tempo, orçamento/reserva de recursos); registrar autorização denegada ou erro mesmo se a chamada não ocorrer.
3. **Evidência:** associar a cada observação um `evidence_id` único, hash do conteúdo, referência restrita, timestamp da fonte quando disponível, estado de redaction e vínculo inequívoco com `invocation_id` e sessão.
4. **Hipóteses:** registrar revisão cronológica como novo evento, nunca sobrescrever silêncio de contradição. Separar fatos observados, inferências do modelo e verificação humana.
5. **Conclusão:** gravar motivo objetivo de saída (`CONCLUDED`, `INCONCLUSIVE`, `BUDGET_EXHAUSTED`, `BLOCKED`, `FAILED`) e vínculos de evidência; se o processo morre antes, o lease/heartbeat vencido permite recuperação sem inventar conclusão.
6. **Retomada:** usar estado idempotente e checkpoints seguros; jamais reconstruir fatos brutos a partir do resumo da IA.

## Modelo lógico proposto

| Objeto | Campos de governança (resumo) |
| --- | --- |
| `investigation_seed` | session/seed IDs, principal/tenant binding, origem, bytes restritos, hash, hora, completeness e versão |
| `investigation_session` | versão de contrato/runtime, state, orçamento autorizado, lifecycle timestamps, replay id |
| `tool_invocation` | invocation ID, seq, tool/version, policy decision, fingerprint, timings, errors e redaction state |
| `evidence_record` | ID, invocation, fonte, hash, referência restrita, tamanho, tempo de fonte, payload eligibility |
| `hypothesis_revision` | hypothesis ID, revisão, supporting/refuting evidence IDs, status e incerteza |
| `investigation_conclusion` | findings, evidences, limitações, classe causal, motivo do encerramento |
| `admission_nonce` | vínculo principal + nonce + TTL, claim atômico para replay entre instâncias |

Os nomes são **conceituais**, não DDL. A escolha entre database e schema depende de capacidade, grants, retenção, backup/restore, replicação e política de migrations do cluster atual. Nenhum dado real será versionado em Git.

## Autorização

- O papel que grava auditoria **não** escreve em tabelas operacionais que o investigador consulta.
- Separar identidade de leitura por fonte da identidade de escrita somente no namespace do investigador.
- Evitar logging de segredos, raw prompt, conteúdo sensível em traces, logs de CI ou comentários de PR. Um digest ou fingerprint não substitui controle de acesso.
- Definir retenção, encryption-at-rest, auditoria de leitores, política de redaction e descarte sem comprometer trilha de integridade.
- Fluxo multi-tenant exige constraints de tenant, auditoria de acesso e testes negativos de leitura cruzada.

## Resistência e recuperação

- Falha em persistir o seed/nonce => **não** iniciar investigação, não enviar ACK de ingestão.
- Queda entre intenção de consulta e resposta => deixar invocação com estado incompleto, sem produzir evidência fictícia.
- Timeout/interrupção => conservar evidências anteriores e reportar explicitamente lacunas.
- Confirmação de sucesso de ferramenta externa => nunca derivar causalidade ou remediação automaticamente.
- Custo de armazenamento, locks, cardinalidade, queries lentas e backup/restore precisam ser medidos antes da ativação.
- Uma tabela append-only com hashes é verificável até certo ponto, mas **não** garante imutabilidade contra DB admins. Não prometer proteção criptográfica sem um mecanismo de ancoragem externo efetivo.

## Gates antes de AGT

- [ ] Schema e roles isoladas propostas, aprovadas e testadas em PostgreSQL descartável via CI distribuída.
- [ ] Testes de idempotência/nonce em concorrência e restart.
- [ ] Crash/restart entre seed, query, evidência e conclusão com recuperação demonstrada.
- [ ] Testes de restrição tenant e denial of access, restore e rollback.
- [ ] Auditoria não copia payloads nem credenciais em outputs públicos.
- [ ] Revisão de limites operacionais, retenção e impacto no cluster real sem mutação antes da autorização.

**Estado real:** nenhuma persistência durável do Investigador implementada; testes atuais são offline e em memória.