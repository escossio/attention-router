# ADR 0021 — Investigação operacional contextual com um único agente e ferramentas somente-leitura

**Status:** Accepted — **apenas para a direção arquitetural**, sem autorizar implantação  
**Data:** 2026-10-15  
**Contrato detalhado:** [AR-OPS-INV-V0](../architecture/operational-investigator-v0-contract.md) (`0.1.0-draft`, PROPOSED)  
**Implantação:** [Plano faseado](../operations/operational-investigator-v0-rollout.md)

## Contexto

Observabilidade identifica eventos e métricas, mas nem a queda nem a normalização de uma métrica comprovam, isoladamente, causa e conclusão de um incidente. A investigação do evento histórico CI01 será o primeiro ensaio cego; seu resultado não deve ser entregue ao agente antes das verificações.

Já existem OpenTelemetry/Tempo, Zabbix ROC, CI distribuído, PostgreSQL, catálogo AFO e adaptador opcional OpenAI Agents SDK. Uma solução de correlação manual rígida duplicaria lógica e limitaria a exploração de hipóteses.

## Decisão

Adotar como direção o **Investigador Operacional Autônomo V0**:

1. **Uma entidade executável nova** é o destino de implantação futura (container preferido como hipótese, não alocado); memória, política, ferramentas e trilha são módulos internos.
2. **Um único agente** com runtime existente sob avaliação. Preferir reuso comprovado do adaptador/SDK já presente; estudar implementações públicas Codex/SDK antes de concluir o reaproveitamento.
3. Ferramentas externas para Zabbix, Tempo, PostgreSQL, Linux, CI e AFO são somente-leitura e têm credenciais, escopos e limites independentes. A escrita é limitada ao estado/auditoria isolado do próprio investigador.
4. **Hipóteses dinâmicas; autorização determinística e externa ao modelo.** Preservar proveniência, contraditório e incerteza.
5. Reaproveitar **o mesmo cluster PostgreSQL** como proposta inicial, com database/schema e perfis separados, condicionado à certificação de capacidade, isolamento e recuperação.
6. Planejar/testar tudo no GitHub e CI offline antes de qualquer alteração no AGT. MikroTik é responsabilidade do operador, fora do escopo de automação.
7. Separar caminho de evento vivo e replay histórico cego; nenhum deles altera o registro governado AFO.

## Consequências

- O V0 é investigativo, **não é um corretor automático**.
- O evento original é preservado na extensão em que estiver disponível, com limitações explícitas de completude.
- A saída pode ser `INCONCLUSIVE` ou `BUDGET_EXHAUSTED`, sempre auditável.
- Um container não autoriza, por implicação, rede/IP/VLAN adicionais.
- O código de esqueleto pode ser preparado em branch paralela ao contrato; divergências de interface exigem reconciliação por PR.
- Nenhuma ferramenta ativa, banco novo, role ou container existe por consequência desta ADR. Operação requer gates e autorização posterior.

## Decisões ainda abertas (não bloqueiam o esqueleto offline)

- Seleção comprovada e integração do runtime de agente.
- Endpoints/portas e filtros dos conectores reais.
- Database versus schema; retenção, quota, backup e grants.
- Alocação de rede/topologia real, autenticação egress do provedor.
- Política de orçamento e métricas de aceite com valores concretos.
- Eventualmente notificações, push ou periodicidade de varredura.
- Detalhes do replay/benchmark e datasets autorizados.

## Alternativas não escolhidas no V0

- Correlacionador de incidentes centrado em árvore de decisões.
- Framework próprio de agentes.
- Implantação multiagente inicial.
- Remediação automática ou alteração de alertas Zabbix.
- PostgreSQL adicional por padrão.
- Exposição de rede própria/IP dedicado sem demonstração de necessidade.

## Referências existentes

- `AGENTS.md` e `docs/operations/github-first-development-control-loop.md`
- `docs/architecture/container-communication-network-placement-v1.md`
- `docs/architecture/channel-context-dual-track-v1.md`
- `docs/ci-agent-v0.md` (agente de CI preexistente, não substituir)
- `docs/operations/afo/README.md`
