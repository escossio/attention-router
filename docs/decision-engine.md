# Motor de Decisão

A resolução considera contato exato, categoria de relacionamento, contexto ativo e regra padrão.
Cada política recebe um `match_score`. A ordenação final é determinística:

1. prioridade
2. especificidade
3. pontuação de correspondência
4. identificador da política

A decisão registra regras correspondentes, vencedora, motivo e `policy_version_id` em
`decisions` e `audit_events`.

Estados de interação: `RECEIVED`, `WAITING`, `DECIDING`, `ACTING`, `WAITING_ACK`,
`ESCALATING`, `ACKNOWLEDGED`, `CANCELED_BY_HUMAN_REPLY`, `COMPLETED`, `FAILED`.

Estados de ação: `REQUESTED`, `DISPATCHED`, `EXECUTED`, `ACKNOWLEDGED`, `FAILED`.
Execução de atuador não encerra a escalada. Só reconhecimento explícito muda a interação para
`ACKNOWLEDGED`.

Políticas publicadas são versionadas. Alterar uma política cria nova versão e ativa essa versão;
interações antigas continuam apontando para a versão antiga, permitindo reconstruir a decisão
histórica.
