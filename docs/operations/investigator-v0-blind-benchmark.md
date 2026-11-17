# Investigador V0 — protocolo de benchmark cego

**Contrato:** AR-OPS-INV-V0@0.1.0-draft · **marco:** #339 · **avaliação:** #342 · **estado:** planejamento, nenhum replay real executado.

## Separação de acesso

- **Investigador:** recebe somente o seed inicial sanitizado e as respostas das ferramentas que escolher consultar. Não recebe diagnóstico conhecido, comentários de PR, gabarito ou ferramentas externas livres.
- **Simulador:** entrega respostas autorizadas, erros e lacunas coerentes com o snapshot congelado. Não entrega conclusões do avaliador.
- **Avaliador:** mantém gabarito e critérios fora do alcance do agente, compara afirmações com evidências independentes e não muda respostas durante execução.
- **CI pública:** apenas fixtures sintéticas e hashes; não publicar logs privados, inventários de hosts, dados de tenants nem o gabarito dos holdouts.

Uma issue pública descrevendo um incidente já contamina um suposto teste cego se o investigador puder buscar livremente o GitHub. Durante o benchmark, permitir apenas conectores simulados governados, sem pesquisa externa.

## Caso A — evento histórico CI01 (primeiro ensaio)

1. Obter o evento original do Zabbix somente se disponível e autorizado. Registrar material efetivamente recuperado, SHA-256, instante e lacunas; não inventar payload.
2. Dar ao agente apenas o alerta inicial e escopo admissível, sem antecipar execução de CI ou causa provável.
3. Expor métricas, logs, traces e execuções como respostas de consultas separadas. Ele escolhe dinamicamente as verificações.
4. Confrontar hipóteses quando a evidência permitir; eventual execução CI identificada precisa apontar IDs/horários/logs de suporte.
5. Encerrar com fatos comprovados, indícios, hipóteses refutadas e incertezas. Falta de dados gera INCONCLUSIVE, não uma causa inventada.

## Caso B — episódio transport/CI02 (holdout candidato)

O check distributed-transport de #338 falhou em CI02 para SHA 687142864d9b045fcfcc2e962da9f995fa680b87. O do #344 passou em CI01 para SHA diferente 79b72d915bf0b6a6acd97765ef7290f703696848. O teste TypeScript envolvido é idêntico, porém isso não estabelece identidade de execução nem causa raiz. O gate é shadow, não required, conforme documentação do laboratório. Ver #345.

**Critério:** manter truth_status=UNRESOLVED até existir validação causal independente. Não inserir o erro detalhado ou hipóteses da issue #345 no seed visível ao agente. Caso ainda sem gabarito permite medir reconhecimento de incerteza, não precisão causal.

## Manifesto mínimo por replay

| Campo | Regra |
| --- | --- |
| case_id | identificador opaco e estável |
| source_hash | hash dos bytes efetivamente recuperados |
| source_completeness | COMPLETE, PARTIAL ou UNAVAILABLE |
| admission_scope | tenant/ambiente atestado por fonte confiável |
| candidate_sha | commit exato da implementação avaliada |
| contract_version | versão normativamente revisada |
| runtime_model_version | runtime, SDK, modelo e versões conhecidas |
| tool_fixture_version | snapshot de ferramentas simuladas |
| limits | número máximo de consultas, tempo, dados, custo/tokens |
| oracle_ref | referência restrita para avaliador, nunca para o agente |
| traces | IDs, fingerprints, status, horários, evidências, conclusão |

## Protocolo de pontuação

- Iniciar a execução com seed e manifesto congelados.
- Registrar cada solicitação de ferramenta (inclusive recusada, falha ou timeout), retorno e identidade da evidência.
- Classificar alegações em PROVEN, INDICATIVE e UNKNOWN apenas com referências auditáveis; proximidade temporal não prova causalidade.
- Medir consultas, duração, limites, custo quando disponível, falsos positivos, casos inconclusivos e vazamento de dados.
- Interromper por evidência suficiente ou limite, sem descartar achados parciais.
- Aplicar incidentes não usados para desenhar prompts/conectores como holdouts independentes.

**Gate:** testes de contrato determinísticos não equivalem à demonstração de raciocínio investigativo. Nenhum êxito em replay autoriza deploy AGT, novo container, permissão operacional ou remediação.

**Dependências:** #338, #340, #341, #342, #345, #346; contrato em #344.