# Conversation repetition guard

O `ConversationRepetitionGuard` é uma barreira determinística antes de qualquer novo outbox. Ele separa exatamente-once técnico por evento de repetição conversacional entre eventos distintos.

## Regra

O guard calcula uma assinatura de estado usando actor, intenção explícita e entrada semântica normalizada. A assinatura de resposta acrescenta o objetivo determinístico. Dentro da janela configurável (`CONVERSATION_REPETITION_WINDOW_SECONDS`, padrão 24 horas), uma entrada equivalente no mesmo contexto ativo só é suprimida quando o mesmo objetivo já foi satisfeito por uma resposta útil efetivamente entregue (`OutboxMessageRow.status=DONE`). O motivo final é `SEMANTIC_REPEAT_OBJECTIVE_ALREADY_SATISFIED`.

Maiúsculas, acentos, pontuação, pequenos typos e reformulações sem novos slots não alteram o estado. Mudança de informação, intenção, ação ou `active_context` quebra a supressão. A janela é secundária: a fronteira primária continua sendo o estado/conversation context.

Resposta proposta, review pendente, supressão, bloqueio, falha ou outbox não enviada não satisfazem o objetivo. Assim, uma pergunta válida sem resposta útil entregue continua permitida. O caminho legado é protegido em `dispatch_next_action`; os caminhos de autonomia e execução também consultam o guard antes de criar ou liberar um intent/outbox.

## Flood e variantes

O guard consulta somente interações anteriores que possuem outbox concluída, antes de aplicar o limite de histórico. Assim, 100 eventos distintos semanticamente equivalentes continuam sendo processados como inbound, mas não produzem 100 respostas depois que o objetivo foi satisfeito. Variantes de texto não contornam o bloqueio porque a comparação é por estado e objetivo, não apenas por hash literal.

## Memória

Claims semânticas equivalentes são deduplicadas por actor, predicate, objeto normalizado e estado de validade. Evidências adicionais podem apontar para a claim ativa existente; conflito real cria supersession/histórico conforme a política, e não é tratado como duplicata.

## Auditoria e métricas

`response_suppressed` registra somente identificadores internos, objetivo, assinatura semântica, hash curto da resposta anterior e referência da decisão anterior. O evento declara as métricas lógicas:

- `responses_suppressed_repeat_total`
- `semantic_repeat_inbounds_total`
- `identical_response_suppressed_total`
- `low_information_loop_suppressed_total`
- `memory_duplicate_claim_prevented_total`

Nenhum texto de mensagem ou segredo é incluído no evento do guard.
