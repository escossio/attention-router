# Investigador V0 — contrato de orçamento de modelo e deadline

**Estado:** PROPOSED. **Referência:** AR-OPS-INV-V0 (#344). Nenhuma chamada a modelo externo autorizada por este documento.

## Distinções importantes

- `max_queries`: número de tentativas de ferramenta, já exercitado offline.
- `max_seconds`: checagem cooperativa entre chamadas, **não** deadline preemptivo da execução bloqueante.
- `max_evidence_bytes` / `max_total_evidence_bytes`: volume de evidência após retorno, não custo de rede/stream/memória interna do conector.
- `max_tool_response_bytes`: tamanho máximo da projeção de ferramenta destinada ao SDK.
- **Ainda inexistentes:** cotas firmes de tokens, gasto monetário e paralelismo efetivo; deadline/cancelamento de chamada externa; cap individual de egress e de context window.

## Orçamento exigido antes do primeiro modelo real

| Dimensão | Critério | Comportamento ao exceder |
| --- | --- | --- |
| Turnos do modelo | máximo explícito por sessão | encerrar com estado parcial, sem retries ilimitados |
| Entrada + saída de tokens | teto por sessão e limite por turno, com uso verificado pelo provedor/SDK | negar próxima chamada, reportar limitação; custos desconhecidos não são tratados como zero |
| Custo estimado e real | preços e modelo/versionamento rastreáveis, rate-card versionado com data | interromper quando atingir limite; estimativa não substitui custo contabilizado |
| Duração total | deadline monotônico externo ao raciocínio do modelo | cancelar chamada pendente quando suportado e impedir novas ferramentas |
| Tempo por ferramenta | timeout e número máximo de retries por operação/endpoint | falha fechada, preservar histórico parcial |
| Tamanho de ferramenta | limite no transporte, na resposta e no prompt, com paginação | rejeitar ou truncar apenas via contrato explícito, sem perder silenciosamente o significado |
| Concorrência | máximo de sessões e chamadas simultâneas por tenant e fonte | fila limitada; não abrir conexões ilimitadas |
| Memória do processo | limite do container/host **futuro**, com observabilidade de OOM | isolamento e fail closed, não impor agora recursos no AGT |

## Responsabilidade pelo controle

**O agente apenas propõe consultas.** O controlador fora do modelo decide se há saldo de tempo, tokens, recursos, permissões e custo. O orçamento é alocado antes da chamada; status e uso são reconciliados após a resposta. Medição ausente ou atraso de telemetria exige margem e comportamento conservador. Não confiar em relatórios do próprio LLM como medidor de custos.

Um `Runner.run_sync(max_turns=...)` limita turnos do SDK, mas não prova sozinho um tempo total nem um limite de custo monetário. Não classificar exceção de timeout ou ausência de provider como incidente resolvido.

## Prova offline

- Injectar contador/medidor fictício de tokens para testar limites sem credenciais.
- Simular provider timeout, resposta parcial, retries, cancelamento e indisponibilidade de métrica de custo.
- Validar que cada execução registrada contém versão de modelo, SDK, contrato, orçamento, consumo e motivo de saída.
- Reproduzir mesma fixture sob orçamentos diferentes sem mutar dados operacionais.
- Testar possíveis falhas na contabilização de chamadas concorrentes e duplicadas.

## Ativação

**Primeiro experimento com modelo real**, se vier a ser autorizado, deve utilizar somente fixture sintética e ferramentas simuladas, sem AGT, Zabbix ou PostgreSQL real. Separar custos e autorização do experimento das políticas operacionais finais. A execução não vira produção pelo fato de um teste passar.

**O presente documento não configura segredos, serviços, modelos nem orçamento monetário automático.**