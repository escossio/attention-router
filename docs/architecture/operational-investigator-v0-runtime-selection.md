# Seleção do runtime V0 — Investigador Operacional

**Status:** PROPOSED (para revisão do PR #344). **Decisão técnica recomendada:** utilizar o OpenAI Agents SDK Python já instalado no Attention Router, sem alterar o agente conversacional da Andy.

## Base comprovada

- O arquivo `pyproject.toml` da `main` fixa `openai-agents==0.22.2`.
- `attention_router/application/agents/andy_agent.py` já constrói `agents.Agent` e `AgentOutputSchema`.
- `attention_router/application/agents/service.py` utiliza `agents.Runner.run_sync(..., max_turns=...)`.
- A documentação pública do [Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk) descreve orquestração de ferramentas e estado pela aplicação; a [API FunctionTool](https://openai.github.io/openai-agents-python/ref/tool/) suporta funções locais.
- O [Codex SDK](https://github.com/openai/codex/blob/main/sdk/typescript/README.md) e o [Codex Python SDK](https://github.com/openai/codex/blob/main/sdk/python/docs/api-reference.md) são públicos, mas a integração do processo Codex ao papel de investigador com escopo estrito de ferramentas ainda não foi comprovada neste repositório. Não atribuir acesso a recursos internos do Codex.

## Definição

1. Um único `agents.Agent` especializado em investigação de incidentes, independente de `ANDY` e de ações da aplicação conversacional.
2. Um único `FunctionTool` de leitura controlada, que recebe apenas operação e fonte e encaminha a solicitação ao broker `_BoundedQueries`, fora da autoridade do modelo.
3. O broker impõe allowed_operations, tenant autorizado, orçamento, auditoria e preservação da observação. Nenhum agente recebe banco/SSH/Docker diretamente.
4. O resultado tipado deve ser convertido em `Findings` referenciando **somente** evidências realmente observadas. O texto do modelo não cria evidência nova nem altera origem.
5. Para o primeiro passo, usar apenas conectores simulados e um Runner substituível em testes. **Nenhuma chamada ao provedor** é autorizada nos testes, CI ou AGT. O modelo real e suas credenciais ficam desabilitados até gate separado.
6. Dados para prompts devem ser minimizados; o conteúdo original do evento não é enviado ao modelo automaticamente. Logs/atributos externos são dados não confiáveis. Saída de ferramenta deve ter sanitização explícita; falta dela bloqueia.

## Contratos que continuam em aberto

- Budget completo: tokens/custo, bytes, deadlines preemptivos e concorrência.
- Seed durável anterior ao runtime, armazenamento e auditoria estruturada.
- Garantia de provenance/IDs por evidência e de autenticação real de tenant.
- Modelo de encerramento causal e pós-validação, inclusive `INCONCLUSIVE`.
- Persistência PostgreSQL em namespace próprio, RBAC, retenção e recuperação.
- Autorização, monitoramento, credenciais e conectores reais para uma futura entidade AGT.

## Alternativas

| Caminho | Decisão V0 |
| --- | --- |
| Reaproveitar `openai-agents==0.22.2` e `Agent/Runner/function_tool` | **Escolhido para protótipo offline**: superfície pública e já presente no repo |
| Codex SDK / app-server | Preservado como referência/candidato posterior, não necessário para o primeiro protótipo |
| Runtime próprio ou multiagentes | Não criar, sem necessidade demonstrada |
| Correlacionador fixo de regras | Não usar como motor central de investigação |

## Critérios de prova

- Testes offline verificam registro de ferramenta pelo SDK sem uso de chave/API.
- O modelo não vê operação mutante, mesmo se a solicitar; bloqueio é independente do prompt.
- Resultados falsos ou índices de evidência inválidos são rejeitados.
- A execução não depende do Zabbix/Tempo/AGT/CI real.
- Apenas depois de G0–G2 e autorização específica poderá ser ativado um runtime real.

**Observação:** documentação da versão corrente do SDK pode diferir de `0.22.2`. O PR de código deve certificar a API efetivamente instalada naquela versão com testes de integração offline.