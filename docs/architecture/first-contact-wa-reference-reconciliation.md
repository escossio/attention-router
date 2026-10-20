# First Contact V1 — identificação WhatsApp Web e recuperação por prova autenticada

## Incidente e causa comprovada — 20/10/2026

Uma apresentação inaugural chegou ao WhatsApp, mas a outbox passou para `AMBIGUOUS`, com `TRANSPORT_REQUEST_OUTCOME_AMBIGUOUS`, enquanto o ledger de saída registrava `FAILED`. O Transport lançou `FIRST_CONTACT_MESSAGE_REFERENCE_UNKNOWN`: `client.sendMessage()` não forneceu a referência esperada. O WhatsApp Web atual expõe um identificador composto no campo `id.$1`, sem `id._serialized`; o extrator anterior ignorava `$1`. O evento `message_create` da sessão reconheceu a mensagem como saída automatizada, mas não preservou seu identificador. Sem referência e ACK correlacionados, o First Contact permaneceu não confirmado e o TTS complementar corretamente não foi liberado.

A perícia read-only no AGT01 comprovou **um único evento de saída** no intervalo, com hash SHA-256 do texto, hash de peer canônico, horário compatível, referência WhatsApp composta `true_<peer@lid>_<message-id>` e ACK real nível 3. Não é uma permissão para inventar referência nem reenviar automaticamente.

## Correção do caminho normal

- `message-id.js` incorpora o campo `$1` para eventos e respostas. O mesmo extrator é usado na classificação `message_create` e nos eventos `message_ack`.
- Se o `sendMessage` resolver sem ID, o Transport **espera até um segundo** por uma única referência composta registrada pelo observador de saída, que já correlaciona conteúdo, identidade e janela temporal. Se nenhuma ou mais de uma referência aparecer, falha fechado como antes: ledger FAILED, outbox AMBIGUOUS, nenhum replay.
- O ledger preserva evidência de ACK recebida enquanto a mensagem ainda está `SENDING` e a transporta monotonicamente ao marcar `SENT`. Nenhuma referência sintética (`accepted-*`) é válida para First Contact.

## Reconciliação histórica estritamente evidenciada

Um método interno do ledger, sem rota HTTP pública, permite reconciliar somente uma tentativa inaugural já marcada `FAILED` quando um operador fornece observação da **sessão WhatsApp autenticada** com `fromMe=true`, identidade remota verificável, mensagem idêntica via hash, instante dentro de 120 segundos, identificador composto canônico e ACK entre 2 e 4. A prova exige correspondência única, inclusive sem registros competidores do mesmo texto/peer/janela. A reconciliação registra hash de evidência, não faz envio e não pode ser repetida.

O reconciliador existente do Worker consulta o Transport por HMAC e confirma `FirstContactAttempt` somente com `SENT` + ACK>=2. Neste caso comprovado, uma outbox primária `AMBIGUOUS` com erro específico de transporte também pode ser marcada `DONE`, com intent `SENT`, **apenas na mesma transação da confirmação autenticada**; eventos anteriores permanecem no audit log. Isso permite TTS complementar pelo texto original, sem gerar resposta diferente. Outras AMBIGUOUS não são tocadas.

## Apresentação

A composição inaugural em português recebeu uma mensagem mais acolhedora, com identidade clara e referência honesta à voz. Uma resposta genérica do Agent ("Oi! Como posso ajudar?") deixa de duplicar a saudação; conteúdo específico do usuário continua anexado. A label `[Andy • IA]`, a versão da apresentação e os contratos de autorização permanecem inalterados.

## Validação e limites

Testes sintéticos cobrem `$1`, ausência de ID em `sendMessage`, observação única, ACK antecipado, duas observações concorrentes (bloqueio), hash/peer/ACK inválidos, recuperação histórica sem envio, continuidade de intro/ACK/TTS/outbox e preservação de texto sem prova. Os checks oficiais do GitHub são obrigatórios antes de implantar; sem merge automático ou alteração de produção nesta PR.

A inspeção via CDP foi **somente leitura** e não mostrou identificadores pessoais. A reconciliação histórica produtiva não ocorre automaticamente na PR: requer execução operacional delimitada e auditada sobre a única tentativa comprovada, após deploy certificado, sem reenviar texto.
