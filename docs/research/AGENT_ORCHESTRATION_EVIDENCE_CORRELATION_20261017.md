# ESTANTE — Agent Orchestration Learning: intenção × execução × efeito

**Data de registro:** 2026-10-17  
**Estado:** ESTACIONADO / PESQUISA — sem autorização de implementação ou implantação  
**Origem:** discussão arquitetural sobre MAESTRO, Attention Router, plugin Remote Desktop Commander e painel MGMT (Andy Ops)  
**Repositório canônico:** `escossio/attention-router`  
**Branch documental:** `research/agent-orchestration-evidence-correlation-20261017`

## 1. Ideia preservada

Investigar se a sequência de etapas anunciadas por ferramentas de agentes — por exemplo, *inspecionando*, *verificando relevância*, *validando*, *certificando* e *executando* — pode inspirar contratos de execução mais verificáveis nos orquestradores existentes.

A hipótese não é copiar rótulos, mensagens de progresso ou comandos. É correlacionar o **plano/ação declarada** com o **que foi efetivamente executado** e o **efeito observável**. O objetivo é aprender padrões de recuperação, diagnóstico, contestação, retomada e escalonamento a partir de evidências, inclusive casos de falha.

A palavra "aprendizado" neste registro significa **análise e extração controlada de padrões operacionais**. Não pressupõe treinamento de modelo, atualização automática de políticas ou autonomia adicional.

## 2. O que já foi verificado versus o que ainda é hipótese

### Confirmado no repositório (2026-10-17)

- O pacote existente do Andy Ops Live Supervisor está em `ops/provisioning/andy-ops-panel/`.
- Seu README descreve a visão **Chat / Console**, que observa o canal `Chat -> Remote Desktop Commander -> AGT`, lê o histórico limitado JSONL de chamadas da ferramenta e apresenta último comando/chamada, duração, resultado e processos descendentes recentes.
- O README prevê sanitização/redação de dados de comandos antes da exibição no navegador e mantém a interface restrita à rede local.
- Existe uma frente distinta, **Investigador V0**, com aba MGMT e eventos tipados no PR #364 (draft); esse mecanismo não é prova de que o histórico do Remote Desktop Commander esteja automaticamente correlacionado às mensagens de progresso percebidas no chat.

### Não verificado / a investigar

- Se as expressões de progresso exibidas ao usuário correspondem a estados reais do executor, estados do próprio cliente ou apenas textos de apresentação.
- Se há identificadores compartilhados e timestamps suficientemente precisos para ligar cada mensagem exibida ao evento JSONL correto.
- Se o histórico disponível cobre todas as chamadas, retornos e efeitos relevantes ou só um subconjunto; lacunas, chamadas indiretas e falhas de coleta precisam permanecer visíveis.
- Se o efeito final pode ser demonstrado por telemetria independente (serviço, processo, endpoint, estado antes/depois), sem inferir sucesso apenas de um exit code.
- Se há casos de execução segura que podem ser abstraídos em contratos, e não em scripts cegos.

## 3. Modelo de estudo proposto (não implementado)

Para uma execução histórica escolhida, construir uma linha do tempo com:

1. **Intenção declarada:** mensagem/etapa observável, origem, timestamp e relação presumida com a solicitação.
2. **Execução registrada:** chamada da ferramenta, alvo, operação, início/fim, resultado técnico e fonte de evidência, apenas metadados sanitizados.
3. **Efeito observado:** alteração ou manutenção de estado em uma fonte independente; indicador explícito quando o resultado é desconhecido.
4. **Avaliação:** correspondência, divergência, dependências, decisões repetidas, caminhos alternativos, falhas e lacunas na correlação.

Evitar inferir raciocínio interno a partir de mensagens de interface; distinguir ordenação temporal de causalidade. Medir incerteza de relógio entre hosts, ids ausentes, duplicidade, timeouts, retries e efeitos colaterais.

## 4. Possíveis aplicações futuras (hipóteses)

- Contratos de evidência entre etapas do MAESTRO/Attention Router.
- Escalonamento para outro agente quando o executor atual falhar, sem perda de contexto validado.
- Retomada parcial por dependências e validade temporal, sem repetir efeitos irreversíveis.
- Contestação e arbitragem de diagnósticos na Mesa de Debate com base em fatos rastreáveis.
- Casos de teste derivados de execuções bem-sucedidas **e malsucedidas**, evitando promover correlação ou sorte a causalidade.

Nenhuma dessas aplicações está aprovada ou implementada por este documento.

## 5. Primeiro experimento futuro (somente leitura)

Escolher **uma execução histórica já concluída** do canal Chat / Console, com início e término conhecidos. Comparar as mensagens de progresso disponíveis, o JSONL sanitizado observado pelo MGMT e o estado anterior/posterior comprovável por fonte independente. Registrar:

- provenance / versão / ambiente / intervalos UTC;
- hipótese de cada correspondência, evidência a favor e contra;
- etapas sem par observável e desvios entre intenção e ação;
- resultado como **CONFIRMADO**, **INDÍCIO**, **HIPÓTESE**, **NÃO VERIFICADO** ou **BLOQUEADO**;
- conclusão explícita sobre a suficiência ou insuficiência dos dados.

O estudo deve ser feito com permissão e coleta mínima; excluir tokens, senhas, payloads pessoais, prompts, credenciais e dados de tenants. Não expor argumentos brutos em documentação pública. Não se conectar a sistemas nem executar remediações como parte desse primeiro estudo.

## 6. Regras para retirar da estante

- Conferir a implementação atual da MGMT e os contratos do plugin antes de desenhar novos componentes.
- Reutilizar observabilidade existente; preservar interfaces, atribuições, controles de identidade e mecanismos de autorização.
- Produzir matriz evidência → decisão → ação → efeito, incluindo falhas e falso positivo.
- Separar diagnóstico, contestação, autorização, execução e certificação; nunca conceder ao classificador autoridade de execução.
- Registrar critérios objetivos de aprovação/reprovação em testes sintéticos e só depois propor mudança de código em PR independente.
- Não fazer merge, deploy, alterar runtime, acionar modelo pago ou publicar demonstrações como prova real por força deste registro.

## 7. Estado do trabalho anterior

Esta frente é **somente documental** e foi estacionada para retornar à tarefa ativa no AGT/MGMT, preservando o desenvolvimento em andamento. Não cria novo motor nem substitui o Investigador V0.

**Referências internas já existentes:**
- `ops/provisioning/andy-ops-panel/README.md`
- PR #364: aba MGMT INVESTIGADOR, prova sintética e journal tipado (draft).
- Issue #356: janela formal de coleta/certificação do Investigador V0; decisões de execução continuam sob seus próprios gates.
