# First Contact V1 — apresentação em texto e áudio TTS (Passada 19)

## Objetivo e compatibilidade

Somente a apresentação inaugural `INTRODUCE` elegível e confirmada poderá entregar **texto e uma nota de voz complementar**, usando o TTS API Switcher, perfil existente `andy`, MediaStore, normalização MP3 → Ogg/Opus e WhatsApp Transport já implantados. Não existe segunda geração pelo modelo de linguagem: o áudio é derivação da resposta textual aprovada, com verificação SHA-256 e idioma da conversa.

O opt-in exige simultaneamente `TTS_ENABLED=true`, `TTS_AUTO_SEND=true` e `TTS_SUPPLEMENTAL_ENABLED=true`. Todos os padrões mantêm voz complementar OFF. Apenas **novas outboxes textuais de apresentações `INTRODUCE` autorizadas** recebem a marcação de áudio; habilitar o opt-in não varre decisões antigas que não foram marcadas. As flags não serão ativadas em produção nesta PR.

## Efeitos e prova de entrega

- **Texto principal:** chave idempotente `execution:<intent-id>`, preservando a resposta e o First Contact como estavam.
- **Áudio complementar:** chave idempotente `execution:<intent-id>:audio`, vinculada ao texto principal por identificador e hash. Somente é gerado para `INTRODUCE` após o texto principal chegar a `DONE` e a apresentação chegar a `CONFIRMED` com prova HMAC e ACK >= 2. Respostas `CONTINUE`, contatos vinculados e conversas comuns permanecem somente em texto.
- ACK insuficiente, texto bloqueado/falhado/`AMBIGUOUS`, prova ausente ou divergência de conteúdo impedem o envio do áudio. O áudio não confirma uma apresentação e não autoriza reenviar texto incerto.
- Revalidação antes do envio verifica intent, decisão, identidade/recipiente, tenant, texto primário entregue, artefato, hash e flags. Falha ou resultado externo ambíguo do áudio modifica apenas seu próprio efeito, preservando o texto. Nada pode fazer replay cego.
- A resposta histórica somente em áudio para entradas de voz permanece disponível quando já elegível. O recebimento e a transcrição das mensagens de voz do usuário são independentes deste opt-in. O novo complemento de voz não concede ao visitante ferramentas, ações, memória privilegiada nem autorização adicional. O contrato imutável `text-only-v1` continua sendo uma restrição de autoridade, não uma autorização de ferramentas por TTS.

## Limites e validação

Intents sintéticas de plataforma foram excluídas: o orçamento de execução atual protege um único efeito lógico `WHATSAPP_TEXT`. Ampliar essa autorização exige uma frente distinta. A implementação não altera entrada de mensagens, histórico, esquema de banco, MAESTRO nem Android. A apresentação sonora ocorre **após a confirmação do texto**, e não simultaneamente, priorizando a ausência de duplicidade.

Testes locais sintéticos direcionados: **133 aprovados** (`test_first_contact_agent.py`, `test_whatsapp_voice_flow.py`, `test_visitor_default_runtime_guard.py`, `test_repetition.py`); `ruff`, `compileall` e `git diff --check` aprovados. Não houve TTS de provedor real nem envio pelo WhatsApp. Certificação oficial de GitHub Actions e PostgreSQL distribuído depende de PR no SHA final; implantação e flags produtivas requerem etapa operacional posterior.

O teste distribuído Python do SHA inicial identificou regressão na supressão de respostas históricas sem payload `text`. Correção cirúrgica: filtrar somente outboxes de voz na busca pela resposta textual histórica, preservando formatos legados. Teste novo comprova que áudio posterior à mensagem não oculta o texto. Revalidação local 133 PASS no código corrigido; o SHA anterior não pode receber GO.
