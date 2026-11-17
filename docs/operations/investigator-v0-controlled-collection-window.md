# PROTOCOLO ESPECIAL — Janela de Coleta e Certificação do Investigador V0

**ID:** INV-V0-COLLECT-20261015
**Versão:** 0.1.0-draft
**Estado:** ABERTA para evidência documental/read-only, não para implantação ou ensaio operacional não autorizado.
**Marco de controle:** [issue #356](https://github.com/escossio/attention-router/issues/356)
**Arquitetura:** [contrato V0](../architecture/operational-investigator-v0-contract.md) · [auditoria durável](../architecture/operational-investigator-v0-durable-audit.md) · [ensaio PostgreSQL](investigator-v0-transactional-pg-proof.md)
**Data de abertura:** 2026-10-15, America/Fortaleza.

## 1. Motivação e prioridade

O Investigador V0 não é uma feature corriqueira: a confiabilidade das futuras análises depende da integridade do evento, da autenticidade da origem, do isolamento entre tenants, da correção dos controles de ferramentas e da distinção entre observação, hipótese e prova causal. Um check verde é apenas evidência de escopo limitado, e um sintoma repetido não é causa demonstrada.

**Decisão operacional:** até que esta janela seja encerrada por revisão formal, suspender progressão automática de implementação, merge, deployment e conexão operacional. Primeiro coletar e documentar. Não corrigir anomalias durante a observação sem registrar uma decisão separada e seu possível efeito sobre o benchmark.

## 2. Etapas independentes

| Etapa | Pode produzir | Não pode concluir |
| --- | --- | --- |
| Coleta | hashes, timestamps, resultados de CI, logs sanitizados, status, proveniência, cobertura | causa, sucesso de implantação, mudança de worker |
| Investigação | hipóteses, contraprovas, lacunas, relação temporal e relato condicionado | `ROOT_CAUSE_PROVEN` sem evidência suficiente |
| Decisão | gate GO/NO-GO com sign-off humano e riscos residuais | autorização retroativa para ação não registrada |
| Remediação eventual | plano de alteração/rollback em PR novo após autorização | apagar ou sobrescrever evidência da coleta |

## 3. Regra de pré-execução: um registro por comando

Antes de executar qualquer comando novo em GitHub, Codex, console, CI, DB ou API, publicar ou atualizar **um item prévio** no registro da issue #356, ou documento relacionado e referenciado nela, contendo:

1. **ID**: `COL-YYYYMMDD-NNN`, objetivo e pergunta que o comando responde.
2. **Alvo e autoridade**: entidade, ambiente, tenant, principal, endpoint somente quando não privado; actor executor.
3. **Instrução exata**: ferramenta/comando/argumentos ou pseudocomando revisável, com SHA e versão. Segredos nunca aparecem no registro público.
4. **Necessidade**: hipótese e razão por que os dados atuais não bastam; alternativa menos invasiva considerada.
5. **Classificação**: leitura, escrita exclusivamente documental, teste sintético, mudança operacional. O rótulo não substitui verificação de efeitos colaterais.
6. **Impacto previsto**: recursos, janela, orçamento, timeout, concorrência, efeitos em CI/jobs, redes, disco, DB e disponibilidade.
7. **Critério de resultado**: o que provará PASS/FAIL/INCONCLUSIVE e quando interromper.
8. **Autorização**: origem da permissão para esse escopo; se houver escrita operacional, autorização humana explícita e rollback.

Depois da execução, sem reescrever o plano como se fosse previsão acertada, anexar **resultado separado**: horário, SHA executado, exit/status, referência da evidência, artefatos e hashes quando apropriado, diferenças observado/previsto, impactos e próxima decisão.

**Comando sem justificativa prévia = comando não executado.** Exceção de emergência exigiria decisão operacional distinta, não está autorizada por este documento. Execuções automáticas já disparadas por PRs existentes devem ser registradas como fatos observados, não fingir que foram aprovadas retroativamente.

## 4. Janela de coleta — alcance e amostragem

- Fase A (agora): GitHub read-only, contratos, PRs, check-runs e logs sanitizados; nenhum comando no AGT/CI01/CI02/CI03.
- Fase B (somente após plano revisado): ensaios sintéticos em PostgreSQL descartável, com comando, SHA, worker, orçamento e política de reversão definidos.
- Fase C (autorização distinta): avaliação offline com modelo real e fixture sem oracle, somente após orçamento completo, tool boundaries e identidade validados.
- Fase D (não liberada): inspeção autorizada de AGT ou implantação de uma entidade. MikroTik permanece exclusivamente sob responsabilidade do operador.

O intervalo de observação não se encerra por data arbitrária. Preservar sequência de eventos e amostras antes de supor que uma repetição é representativa. Não manipular worker, suite ou fixture para forçar diferenças; o caso CI02 exige comparação do **mesmo SHA** em trabalhadores diferentes, ainda não obtida.

## 5. Matriz obrigatória de evidência

| Referência | Afirmativa | Prova disponível | Estado | Lacuna |
| --- | --- | --- | --- | --- |
| PR #355 | Código de seed/nonce/auditoria em uma transação sintética | arquivos e testes versionados | IMPLEMENTADO PARA TESTE | CI completa no SHA atual e falhas de crash/retry |
| PR #355 | Check distributed-postgres do head `ac67ec00a4145f3b35a56576523b27ded5044d62f` | GitHub check-run | SUCESSO DAQUELE CHECK | não generalizar para todo o ciclo, fsync, backup ou produção |
| PR #344 | Contrato durável/isolamento de dados | documentos versionados | PROPOSTO | revisão G0 e compatibilidade com código |
| Issue #345 | Repetições da falha de transporte em CI02 | check-runs com SHA distintos | OBSERVADO | comparação controlada do mesmo SHA, causa indeterminada |
| Issue #342 | CI01 e CI02 para testes cegos | protocolo sintético, histórico | NÃO CERTIFICADO COM LLM REAL | fixtures privadas/holdout e avaliação externa |

**Vocabulário de status:** `OBSERVADO`, `CONFIRMADO`, `INDÍCIO`, `HIPÓTESE`, `INCONCLUSIVO`, `BLOQUEADO`, `NÃO TESTADO`. Não promover `OBSERVADO` para `CAUSA COMPROVADA` sem teste causal independente.

## 6. Cadeia de custódia

- Conservar dados de origem sob acesso restrito; documentação pública referencia apenas commits, hashes, metadados mínimos e URLs autorizadas.
- Registrar origem, horário UTC/offset, versão, método de coleta, normalização/redaction e hash do artefato **que foi efetivamente obtido**.
- Diferenciar artefato bruto, projeção sanitizada, resumo do agente e julgamento humano.
- Não copiar segredos, tokens, endereços privados, logs operacionais integrais ou payloads de tenants para issues ou Actions.
- A ausência de uma consulta/erro de worker não é prova de ausência de evento. Preservar erros como fatos da ferramenta.
- Atualizações são aditivas e datadas; correções documentais devem apontar o registro original. Git não torna uma base pública imutável a administradores.

## 7. Registros inaugurais (ações documentais, sem execução em infraestrutura)

| ID | Ação | Justificativa prévia | Resultado conhecido |
| --- | --- | --- | --- |
| COL-20261015-001 | Consulta read-only do PR #355 e dos contratos em #344 | Fixar baseline, identificar testes e lacunas antes de prosseguir | PR #355 draft; docs de auditoria e prova presentes; sem merge observado |
| COL-20261015-002 | Consulta read-only ao check-run `distributed-postgres` do SHA atual de #355 | Distinguir validação específica de prontidão global | `success` para `ac67ec00a4145f3b35a56576523b27ded5044d62f`; demais garantias não inferidas |
| COL-20261015-003 | Criar issue #356 | Dar identidade, escopo e critério de saída à janela | Issue aberta, sem efeitos operacionais |
| COL-20261015-004 | Versionar este protocolo no PR #344 | Guardar procedimento auditável fora da discussão informal | A confirmar pelo commit desta alteração; pode acionar CI documental |

Os IDs inaugurais descrevem os passos de organização desta janela; não são evidência de ensaio real do Investigador.

## 8. Requisitos de fechamento

- [ ] Inventário de comandos e efeitos assinado/revisado, sem lacunas inexplicadas.
- [ ] Matriz completa de invariantes e evidências, incluindo transações, tenant, nonce, limites e regressões.
- [ ] Testes reproduzidos em SHAs exatos com scope e orçamento, sem carga pesada no control plane.
- [ ] Divergências preservadas e classificadas, sem diagnósticos inventados nem checks mascarados.
- [ ] Revisão explícita do risco HMAC→DB, replay entre instâncias, crash, backup/retention e audit por consulta.
- [ ] G0 documental e gates aplicáveis de CI concluídos e revisados.
- [ ] Relatório GO/NO-GO limitado à **próxima etapa offline**, com responsável e riscos aceitos. Qualquer passo para AGT/produção requer nova autorização.

**Regra de ouro:** justificar antes, observar sem distorcer, documentar o resultado, só depois decidir.