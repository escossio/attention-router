# Projeção mínima do evento para investigação contextual V0

**Contrato principal:** `AR-OPS-INV-V0@0.1.0-draft` · **estado:** PROPOSED
**Implementação candidata:** PR #350, empilhado na pilha offline de PRs #338 → #346 → #348 → #349.

## Separação obrigatória

Evento original armazenado com fidelidade e referência restrita **não é** o texto enviado ao agente. O sistema de admissão autorizado cria uma projeção mínima, tipada e sanitizada, referenciando o SHA-256 do evento original. A projeção é o **sintoma inicial**, não um diagnóstico, uma autorização ou uma conclusão.

Fluxo lógico:

1. Origem admitida (ainda simulada na V0) → preserva bytes do evento original e metadados de autenticidade separados.
2. Camada de admissão → valida identidade e escopo e produz projeção tipada de poucos campos; não deve confiar em instruções contidas em logs/eventos.
3. Broker → verifica vínculo entre SHA-256 da projeção e bytes do evento e oferece apenas ferramentas de leitura autorizadas.
4. Agente → recebe projeção e consulta as ferramentas que considerar relevantes. Respostas das ferramentas continuam sendo dados não confiáveis.
5. Conclusão → só pode citar evidências realmente coletadas, com classe de incerteza explícita.

## Campos mínimos da proposta V0

| Campo | Tipo/limites | Exposição ao agente |
| --- | --- | --- |
| `event_sha256` | SHA-256 hex de 64 caracteres, associado ao seed | Sim, apenas fingerprint, não prova de autenticidade |
| `symptom` | enum: resource_pressure, service_unavailable, latency_increase, disk_pressure, network_degradation, unknown | Sim |
| `severity` | enum: info, warning, high, critical, unknown | Sim |
| `observed_at` | timestamp ISO 8601 com fuso | Sim |
| `target_role` | enum: ci_worker, application, database, network, unknown | Sim; nenhuma identidade real de host |
| `observed_value` | número finito ou ausente | Sim, conforme fonte autorizada |
| `unit` | enum: percent, milliseconds, bytes, count, none | Sim; vinculado à presença do valor |

**Nunca enviar por padrão:** bytes do evento original, tenant ID real, hostnames/IPs, token, credencial, mensagens livres do Zabbix, logs, stack traces, identificadores pessoais, texto extraído sem validação, gabarito causal de incidentes históricos ou acesso ao GitHub de incidentes públicos.

## Autoridade e prova

- No estágio offline, `projection_resolver` é somente uma fixture que simula a camada de admissão. **Não é autenticação real.**
- Em implantação futura, autorização da origem e tenant deve ocorrer antes de resolver a projeção; o modelo não pode influenciar a seleção de contexto nem a lista de ferramentas permitidas.
- O SHA-256 prova apenas equivalência do conteúdo vinculado, não quem o originou ou se o evento é confiável.
- Sem projeção admitida, projeção inválida ou vínculo SHA inconsistente, nenhum Runner real é acionado.
- Um evento pode ser preservado integralmente no armazenamento restrito e exposto ao modelo apenas como projeção; isso evita alterar o registro original.

## Gates de aceitação

- [ ] Testes negativos de enums, timestamp, NaN/Inf, hash diferente, origem ausente e tentativa de prompt injection em bytes brutos.
- [ ] Verificar com Runner simulado que somente campos permitidos chegam ao prompt.
- [ ] Ferramenta real continua bloqueada até contrato de redaction, observação idempotente e autoridade por fonte.
- [ ] Definir com segurança escopo real do tenant, admissão Zabbix e persistência antes de AGT.
- [ ] Medir se os campos do seed são suficientes para escolha dinâmica de consultas em avaliação cega. Sem isso, não afirmar investigação autônoma.

**Não objetivos:** construir trigger Zabbix, criar conta PostgreSQL, dar SSH a hosts, mudar MikroTik, ligar um modelo de produção, modificar evento original ou declarar causa raiz.