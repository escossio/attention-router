# Investigador V0 — limite de confiança das respostas das ferramentas

**Estado:** PROPOSED — interface sintética e testes, sem autorização para conectores reais.
**Contrato:** `AR-OPS-INV-V0@0.1.0-draft` · **implementação:** PR #351 · **marco:** #339.

## Distinção entre evidência e contexto do agente

O conector autorizado obtém uma observação e o broker registra a tentativa, o resultado, o fingerprint e a evidência. A **evidência original** não pode ser modificada silenciosamente só para caber no prompt. Uma segunda projeção, independente e estritamente tipada, define que parte da observação poderá ser apresentada ao modelo.

```
Entidade externa / fixture
         |
         v
Broker de leitura autorizado
         |
         +----> Evidência original (acesso restrito; auditável)
         |
         v
Projeção de resultado por operação
         |
         v
Agents SDK — somente campos permitidos e identificador local de evidência
```

Uma resposta que não corresponde integralmente a um contrato conhecido é **recusada**, não traduzida em texto livre. Recusar a visão do modelo não significa apagar o material que a ferramenta devolveu. O armazenamento restrito dessa evidência ainda depende de contrato futuro de retenção, autenticação e autorização.

## Operações sintéticas permitidas para prova offline

| Fonte/operação | Exposição tipada ao agente | Negativas obrigatórias |
| --- | --- | --- |
| Zabbix / `read_event` | `event_id` sintético, severidade enum, data/hora com offset | mensagens livres, chave desconhecida, token, hostname real |
| Zabbix / `read_history` | `cpu_pct` numérico ou série limitada de pares data/valor | dados extras, campos executáveis, valores não finitos, séries extensas |
| Tempo / `read_trace` | indicador de cobertura e data/hora, sem spans detalhados | baggage, atributos livres, contexto privado |
| CI / `read_jobs` | até 16 registros de identificadores sintéticos e tempos | nomes reais de host, campos extras, conteúdo integral de logs |
| Qualquer outra origem/operação | **nenhuma** | rejeitar sem fallback genérico |

**Este quadro não define a interface de Zabbix/Tempo/CI de produção.** Em conectores reais, a autorização por operação, origem e tenant deve ser verificada fora do modelo; os tipos/volumes devem ser revistos contra dados de fonte realmente suportados.

## Garantias e limitações

- Ao modelo chegam apenas `evidence_index`, `source`, `operation` e os campos da visão tipada. Não enviar `source_reference`, observação bruta, log, token ou comando externo.
- Toda tentativa de fornecer `debug`, `api_key`, `message` ou campos inesperados é recusada com erro genérico. Nada da informação indevida deve aparecer em exceções, métricas públicas ou auditoria genérica.
- O prefixo `synthetic:` é condição de fixture, não autenticação. IDs sintéticos seguem allowlist. Não usar isso para reconhecer origens reais.
- Os controles reduzem a superfície de prompt injection, mas **não garantem imunidade** a todos os ataques semânticos. Um modelo real e seu comportamento ainda precisam de avaliação independente.
- A ferramenta pode obter material legítimo que a visão tipada não aceita; neste V0 a decisão correta é falhar fechado e pedir evolução versionada do contrato, não liberar texto livre.
- Credenciais reais, egress a modelos, conectores operacionais, banco PostgreSQL e ingestão do Zabbix permanecem desabilitados/não implementados neste incremento.

## Portas para evolução

1. Definir um perfil versionado para cada fonte/operação real, incluindo proveniência, variáveis sensíveis, limites de cardinalidade e política de retenção.
2. Introduzir IDs imutáveis de evidência e auditoria durável em namespace isolado; manter material bruto com acesso mínimo e redaction na observabilidade geral.
3. Provar autenticidade e escopo de cada conector com teste negativo. Disponibilidade de rede nunca constitui permissão.
4. Implementar orçamento de tokens/custo, cancelamento efetivo e tratamento de erro de ferramenta sem transformar falha em ausência de evento.
5. Validar com ataques adversariais sintéticos e benchmark cego; somente depois propor autorização separada para dados operacionais.

**Sem merge automático, implantação, mudança na MikroTik ou alteração do pool de CI.**