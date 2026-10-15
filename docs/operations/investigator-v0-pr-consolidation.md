# Sequência de consolidação — Investigador Operacional V0

**Estado:** plano de revisão, não autorização de merge/deploy. **Data:** 2026-10-15.

## Fonte de verdade e ordem de revisão

Esta implementação cresceu por PRs deliberadamente empilhados. Cada PR de código tem como *base* a branch do PR anterior, não `main`; checks verdes em uma branch não certificam automaticamente a integração posterior com uma `main` mais recente.

| Ordem lógica | PR | Conteúdo | Dependência |
| --- | --- | --- | --- |
| 0 | #344 | Contrato normativo, grafo, ADR, limites e protocolos de teste | Revisão G0, base main |
| 1 | #338 | Esqueleto isolado e conectores simulados | Base main |
| 2 | #346 | Auditoria por consulta, autorização offline, evidência parcial | Base #338 |
| 3 | #348 | Adaptador Agents SDK opt-in | Base #346 |
| 4 | #349 | Replay sintético + grader estrutural integrado | Base #348 |
| 5 | #350 | Projeção sanitizada do seed | Base #349 |
| 6 | #351 | Visões tipadas e rejeição de respostas inseguras | Base #350 |
| 7 | #352 | Quotas individuais e acumuladas de evidências | Base #351 |

**PR #347 é um ramo paralelo anterior** com o carregador e fixture de replay; os mesmos quatro arquivos foram importados para #349, já combinado à stack principal. Na promoção, **não duplicar commits/diff nem assumir que os dois PRs devem ser mergeados**. Revisar os arquivos por blob/diff, selecionar uma trilha canônica e fechar o PR redundante somente após confirmar que o conteúdo está efetivamente versionado na `main`.

## Portas antes de rebase/merge

1. Capturar SHAs exatos das heads e verificar ausência de mudanças não revisadas. `mergeable=true` não prova conformidade, teste ou autorização.
2. Fechar a revisão do contrato G0; identificar decisões abertas e implementações deliberadamente parciais. Não tratar documento de futuro como evidência de feature atual.
3. Conferir diferenças e testes de cada PR **em relação à sua base**; evitar review que atribua os arquivos do PR pai ao filho.
4. Verificar required checks do SHA candidato e checks de segurança. `distributed-transport` está documentado como **shadow não-required** em `ops/provisioning/distributed-ci-lab/README.md`; falha persistente no CI02 permanece em #345 e não pode ser mascarada, apesar de não ser gate required.
5. Reconciliar contratos com código: admissão autenticada, namespace PostgreSQL, IDs de evidência e audit trail estruturado ainda são ausências, não promessa operacional do V0.
6. Somente após revisão dos contratos, rebase/reconciliação do stack de código e CI green aplicável, submeter decisão humana de merge. Não fazer merges automáticos encadeados.
7. Qualquer teste com modelo real, credenciais ou rede operacional requer gate próprio e autorização; passar CI offline não concede esse direito.

## Desenho de CI correto

- `distributed-python`: gate completo de Python nos workers, obrigatório.
- `distributed-postgres`: gate com PostgreSQL sintético e SHA exato, obrigatório.
- `python-tests`, `transport-tests`, `docker-build`, `secret-scan`, CodeQL: controles de workflows públicos conforme regras de proteção vigentes.
- `distributed-transport`, `distributed-docker`: shadow checks, úteis como inteligência diagnóstica; consultar status sem promovê-los silenciosamente a required nem desativá-los.
- Não rodar suíte pesada no AGT control plane por falta de capacidade; usar workers distribuídos segundo política existente.

## Limites de aceitação da fase atual

O stack pode provar estrutura, contratos, fidelidade de dados sintéticos, ferramenta somente-leitura, auditoria in-memory e rejeição de payload inseguro. **Ainda não prova agente investigando causa real**, mesmo que o Runner SDK esteja corretamente conectado. O primeiro experimento com modelo real precisa preservar um gabarito cego, registrar orçamento/custo, não executar correções e separar evidência de inferência.

## Operação e reversão

Zero containers/VMs extras implantados por esses PRs. MikroTik fica sob gestão do operador, sem mudanças automatizadas. O AGT permanece intocado até autorização operacional específica, credenciais mínimas e rollback revisado. O incidente CI02 na #345 é mantido como candidato ao teste cego, sem isolamento prematuro do worker.