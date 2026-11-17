# Andy: piloto de compreensão semântica bilateral de conversas WhatsApp V1

**Estado:** contrato de trabalho e decisões de produto para revisão pelo titular. Não é evidência de funcionalidades já implantadas.
**Destino de versão:** Attention Router, `docs/architecture/andy-whatsapp-semantic-conversation-pilot-v1.md`.
**Princípio:** reutilizar os motores existentes; nenhuma reconstrução indiscriminada de Transport, Observer, MAESTRO, memória ou identidade.
**Abrangência:** mensagens privadas da própria conta WhatsApp do titular, associação de participantes e contexto semântico. A primeira conversa piloto real é identificada exclusivamente em registro **privado fora do Git**.
**Privacidade documental:** nenhum telefone real, nome de contato real, LID real, trecho de conversa, gravação, credencial, endpoint interno, IP/VLAN ou inventário do AGT deve constar deste documento ou de testes versionados.

## 1. Finalidade e hierarquia de decisões

Construir a capacidade de Andy compreender **os dois lados de uma conversa WhatsApp**, preservando quem escreveu/falou, o instante do WhatsApp, as evidências de origem, a identidade do interlocutor e as relações semânticas, inclusive quando há áudios enviados pelo proprietário.

Este documento é a referência obrigatória de intenção do titular **para este piloto**, não substitui as fronteiras de segurança, o contrato do código, a autenticação, a privacidade nem as regras do repositório. Em caso de conflito, **não tentar contornar** proteções existentes. Documentar conflito, alternativa e impacto; pedir decisão do titular antes de alterar uma fronteira.

**Ritual obrigatório do Codex antes de cada decisão arquitetural, mudança funcional ou correção relevante:**

1. Reabrir **esta versão exata do documento no Git**, os arquivos reais e seus testes. Não trabalhar apenas de memória nem de mensagens resumidas.
2. Apontar a seção desta especificação que justifica a ação, a evidência concreta do código/ambiente, os contratos afetados e o efeito sobre o que já funciona.
3. Separar **fato verificado**, **regra de produto escolhida pelo titular**, **hipótese técnica** e **limitação ainda não testada**. Divergência exige registro e revisão, não correção silenciosa.
4. Definir teste de aceitação e regressão **antes** da implementação. Aplicar mudanças em worktree/branch isolada. Preservar o código existente, o backend de autoridade e o fluxo bidirecional.
5. Validar, apresentar resultados e riscos antes de commit, merge, alteração de serviço, instalação de APK, habilitação de permissões ou publicação. Publicar apenas quando houver autorização para aquele passo.
6. Em toda PR, tratar separadamente as trilhas **continuidade ao vivo** e **aceleração histórica**, e explicitar caso a segunda seja adiada por segurança.

Este documento **não** concede autorização para ler agendas, importar histórico completo, transcrever mensagens privadas em provedor novo, responder contatos, enviar mensagens nem habilitar processamento de IA externo.

## 2. Estado atual confirmado na investigação

### 2.1 Arquivamento bilateral

O Attention Router já possui `inbound_events`, `conversation_threads`, `conversation_messages`, `voice_transcriptions` e mecanismo de reconciliação por eventos de entrada, com horários `sent_at` (horário de ocorrência do provedor), `source_received_at` (recebimento) e momento de arquivo. A organização da conversa usa um identificador estável `wwebjs:<id>@lid` quando disponível, com aliases telefônicos `<telefone>@c.us` nos metadados do ingresso. O nome salvo no celular não está materializado automaticamente no título da conversa.

O trabalho observado numa conversa piloto privada já mostrou:
- Áudios recebidos são enviados ao pipeline de mídia/STT e podem ter texto associado à mensagem original.
- Áudios enviados pela conta ficam arquivados como evento, mas muitos permanecem em `UNKNOWN_FROM_ME`, `PENDING_TRANSCRIPTION`, sem download de mídia e sem registro em `voice_transcriptions`.
- A configuração `CONVERSATION_CAPTURE_OWNER_VOICE_ENABLED` pode estar ativa, mas o gate do Transport continua exigindo `OWNER_MANUAL_OUTBOUND_OBSERVED`, e a classificação da mídia retorna `UNMATCHED_MEDIA_FROM_ME` quando a proveniência não demonstra automação nem autoria manual.
- Os identificadores LID e telefone **já podem coexistir nos eventos**, mas a busca no arquivo de conversas não é uma consulta de identidade de pessoa; o título/nome precisa de resolução própria.
- O arquivamento **não equivale à admissão na memória**. Um arquivo de mensagens sem extração/episódios não proporciona sozinho compreensão semântica persistente.

Arquivos a inspecionar primeiro, sem presumir que continuam idênticos: `whatsapp-transport-local/src/{conversation.js,outbound-provenance.js,bridge.js,inbound.js,media.js}`, `attention_router/application/conversation_live_capture.py`, serviços STT existentes, contratos de memória/proveniência e infraestrutura de identidade/tenant. Confirmar também os commits e PRs em curso antes de escolher a branch.

### 2.2 O que não foi demonstrado

Não existe, apenas com `fromMe=true` em uma sessão WhatsApp Web, prova criptográfica de qual aparelho enviou o conteúdo ou biometria de quem pronunciou as palavras. O dispositivo pode ter sessões vinculadas, operadores adicionais e saídas automatizadas. **Isso não impede a regra de atribuição de produto descrita abaixo**, mas impede chamar essa regra de prova física ou conceder privilégios por causa dela.

Não afirmar que os áudios antigos podem ser recuperados sem verificar disponibilidade, consentimento, idempotência e limites reais de mídia. Não inventar transcrições nem atribuir a pessoas o conteúdo ainda não transcrito.

## 3. DECISÃO EXPLÍCITA DO TITULAR: autoria das mensagens enviadas

**Regra de produto solicitada:** para fins de compreensão semântica da conversa pessoal, tratar o conteúdo **manual** enviado pela **conta WhatsApp autenticada do titular** como fala/declaração do proprietário, inclusive áudios enviados do telefone. Não exigir biometria vocal para que a Andy compreenda o lado do titular. Esta é a interpretação operacional escolhida pelo proprietário da conta.

**Implementação correta dessa decisão:**

- Evidência técnica primária: sessão WhatsApp reconhecida e autenticação do proprietário/tenant, `fromMe=true`, mensagem observada com ID do provedor e conversa resolvida.
- **Precedência absoluta da origem conhecida:** se há comprovante de saída pela automação/MAESTRO/Outbox, registrar `ROUTER_AUTOMATED_OUTBOUND_OBSERVED` (não assumir que foi o humano). Mensagens de terceiros, grupos, self-chat de comando e eventos com identidade conflitante conservam suas regras específicas.
- Para saída da conta não identificada como automação, permitir a atribuição semântica **por regra do titular**, com um estado/fundamento explícito, por exemplo `OWNER_ACCOUNT_ATTRIBUTED_BY_POLICY_V1`. Pode ser representado em nova coluna, metadado ou camada de interpretação, desde que **não substitua silenciosamente a classificação bruta `UNKNOWN_FROM_ME`** nem falsifique a cadeia de proveniência. A escolha do esquema é decisão técnica a validar.
- Atribuição semântica por política significa: "esta conta é do proprietário e ele decidiu tratar suas saídas manuais como suas para contexto". **Não** significa "a câmera/sensor provou que ele segurava o telefone", nem "assinatura de humano verificada".
- Em caso de evidência positiva conflitante (automação comprovada, outbox pendente com proveniência concorrente, conta não autenticada, múltiplos proprietários, tenant divergente), **não promover** autoria ao proprietário até resolução. Conservar o evento, a transcrição quando legal/autorizada e a incerteza.
- A regra **não** concede autoridade a comando, aprovação, ação externa, acesso a arquivos, memória secreta ou envio de resposta. `OWNER_COMMAND` e decisões de execução exigem seus controles de autenticação específicos.

**Critério central do usuário:** a Andy deve conseguir reconstruir o contexto em que o titular diz algo e a outra pessoa responde, sem transformar a exigência de prova biométrica em uma barreira artificial ao entendimento.

## 4. P0: descobrir e congelar evidências antes de editar

Fazer inspeção objetiva, preferencialmente read-only:
- Reconstruir contratos e flags reais de origem/observação de saída, eventos `message` / `message_create`, perfis autorizados, deduplicação e limites de download de mídia.
- Seguir um áudio de saída, em dados **sintéticos**, do WhatsApp até o registro do evento, media spool, STT, atualização de `conversation_messages` e leitura para contexto.
- Distinguir ausência de transcrição porque **não houve captura** de mídia de falha real do serviço STT.
- Identificar a API existente que mapeia LID↔telefone e a política de LID canônico.
- Mapear a autoridade de contato salvo, o escopo OAuth disponível, a política de exclusão/retificação e componentes semânticos existentes.
- Checar estado real dos serviços em separado da expectativa do GitHub. Não alterar contêineres para "diagnosticar".

Saídas de P0: inventário mínimo de componentes já existentes, lacunas, contratos reutilizáveis, riscos, testes necessários e plano dividido por PRs pequenas. Confrontar cada escolha com este documento.

## 5. P1: áudio de saída, captura e STT bilateral

**Objetivo:** áudio enviado pelo titular passa por captura autorizada e transcrição, assim como áudio recebido, sem tocar nas regras de envio de mensagens.

Condições:
- Reutilizar `captureInboundMedia`, spool, notificação assinada, armazenamento de mídia, pipeline STT e `capture_voice_transcription`. Evitar duplicação de motores.
- Introduzir uma política explícita e reversível para **mídia de saída da conta autenticada**, limitada inicialmente a conversa **direta** e tenant autorizado. Preservar a origem bruta, motivo da decisão e prova de conta.
- Preservar a classificação "autoria por regra do titular" separadamente da proveniência física do aparelho. Nenhum áudio de saída pode virar `OWNER_COMMAND` pelo simples fato de ser `fromMe`.
- Para automações conhecidas, registrar `AUTOMATED` e sua referência sem tratá-las como falas humanas. Ambiguidade não pode produzir execução, mesmo se a captura de mídia for permitida.
- Cada mensagem conserva ID do WhatsApp, `sent_at`, identidade da conversa, direcionalidade, hash/recibo de mídia, status STT e referência de proveniência. Replays devem ser idempotentes; falhas devem produzir estados explícitos e permitir retentativa limitada.
- Registrar um motivo distinguível para `MEDIA_NOT_AVAILABLE`, `MEDIA_CAPTURE_DISABLED`, `MEDIA_CAPTURE_FAILED`, `TRANSCRIPTION_PENDING`, `TRANSCRIPTION_FAILED` e `TRANSCRIBED`. Evitar "pending" eterno sem explicação.
- Priorizar **novas mensagens ao vivo**. Históricos antigos exigem etapa autônoma com consentimento, janela autorizada, limites e recuperação de mídia comprovadamente possível; **não** varrer o WhatsApp inteiro, não reiniciar cursor e não forçar download do perfil antigo.
- Tratamento sensível: texto transcrito pode conter informações íntimas. Nenhuma transcrição, telefone, nome, áudio ou trecho deve aparecer em logs, CI público, GitHub, TTS, notificações ou prompts fora de autorização.
- Não habilitar transcrição externa nova ou upload novo sem revisar regras de privacidade e contrato de fornecedor atual.

**Aceite P1:** testes de texto + áudio em ambas as direções, manual da conta e saída automatizada, classificação ausente, falha de mídia, falha/retentativa STT, sessão inválida, conflito de tenant, grupo, replay e erro de origem. A leitura do contexto identifica o proprietário no lado manual conforme a política, sem alegar biometria.

## 6. P2: identificação consistente LID, telefone e contato

**Problema:** a conversa usa LID como chave canônica e o telefone pode existir apenas como alias dos eventos; uma pesquisa por telefone diretamente na chave principal gera falso "não encontrado".

**Contrato a preservar/propor:**
- Manter a chave de conversa canônica por `tenant + source + source_account + LID`, sem trocar indevidamente todas as conversas para número.
- Criar/usar um índice de **aliases provados**, sempre com origem, instante, tenant, fonte WhatsApp e validade: `LID ↔ PN (@c.us)`. Não converter LID em telefone por conta própria. Preferir o mapeamento disponibilizado pelo provedor (ex.: `getContactLidAndPhone`), com confirmação e limites de confiança.
- Resolvedor deve localizar uma conversa tanto pelo LID quanto pelo número conhecido; conflitos/múltiplos LIDs para o mesmo telefone **não** devem ser fundidos silenciosamente. Nome/telefone não são chave universal de pessoa.
- Separar **contato salvo** de **identidade da pessoa**; o nome que o dono salvou é um alias escolhido por ele, não certificação civil da identidade.
- Evolução de nomes, números, portabilidade, contatos duplicados e exclusões precisam gerar atualização de vínculo sem apagar eventos históricos originais.
- Não indexar números completos em logs ou repositórios públicos; busca autenticada e estritamente por owner/tenant.

**Contatos Google:** habilitar integração própria na Andy com **Google People API**, leitura somente com autorização separada e explícita, escopo mínimo `https://www.googleapis.com/auth/contacts.readonly`. Conexão atual com Gmail ou conector do ChatGPT não concede, por si, acesso à agenda no backend da Andy. Não solicitar permissão para escrita, criação ou edição dos contatos. Normalizar números criteriosamente para comparação (formato E.164 quando justificável), preservar vínculo de origem e bloquear matches ambíguos.

**Agenda local Android:** investigar como opção independente, caso alguns nomes existam somente no telefone. Permissão `READ_CONTACTS` somente se necessária, através de consentimento da aplicação, seguindo governança Android. Preferir não requerê-la quando sincronização Google read-only já satisfaz o objetivo. **Não varrer a agenda inteira sem autorização**. Mostrar ao usuário o contato vinculado e a origem do nome.

**Aceite P2:** um LID com telefone provado encontra uma conversa por ambos; nomes da agenda aparecem como "salvo como", com fonte; conflitos e contatos ausentes permanecem explícitos; mudança/remoção de nome atualiza a resolução sem reescrever a autoria histórica; vazamento entre tenant não ocorre.

## 7. P3: contexto e primeira associação semântica de uma conversa

A memória precisa compreender **quem disse o quê, a quem, quando e com que evidência**, com ambos os lados completos. Reusar `conversation_threads`, `conversation_messages`, `memory_evidence`, `semantic_episodes` e módulos de memória existentes antes de propor modelos novos.

Modelo conceitual:
- **Mensagem**: identificador do provedor, sent_at, direção, tipo, texto ou status STT, origem observada, base de atribuição e autor semântico, tenant e conversa.
- **Conversa**: LID canônico, aliases telefônicos provados, conta/tenant, título de contato como metadado derivado, não identidade comprovada.
- **Episódio**: período coerente, vínculos às mensagens e participantes, assunto emergente, confiança, ambiguidades, última atualização e histórico de correções.
- **Fato durável**: somente afirmação elegível após extração limitada, proveniência consultável e regras de sensibilidade, preferência e revisão do titular. Nunca tratar o resumo integral da conversa como memória verdadeira e permanente.

Um resumo sem ambas as vozes transcritas deve indicar **contexto incompleto**, não preencher lacunas com conjecturas. Uma transcrição incerta ou atribuída por política deve conservar sua base e nunca promover afirmações sobre terceiros sem evidência adequada. Uma inferência sobre relacionamento não é um fato confirmado.

**Piloto:** uma conversa direta elegível, selecionada privadamente pelo titular mediante alias/número. Não gravar no Git o número, nome, LID, textos ou gravações reais. O acesso ao histórico do piloto deve seguir consentimento de leitura, retenção e processamento. Usar dados sintéticos na CI; se o titular autorizar validação de dados reais, fazer inspeção mínima com evidência local privada e sem enviar mensagens.

**Aceite P3:** sequência temporal fiel ao WhatsApp, dois lados semânticos disponíveis quando houve STT, nomes com origem explícita, referência a cada mensagem, recuperação da conversa sem misturar interlocutores, prevenção de duplicatas, revisão/correção pelo titular, e prova de nenhuma mensagem automatizada tratada como declaração pessoal.

## 8. Segurança, privacidade e isolamento

- Manter a arquitetura `Android → SDK → Client API → Attention Router`; Transport WhatsApp ingressado somente pelo caminho autenticado existente; nenhuma chamada direta de UI a banco/segredo.
- Preservar autenticação/tenant/humano/dispositivo, isolamento de contatos, prova de encaminhamento e idempotência. Origem da mensagem **não** concede autorização de execução.
- Separar consentimento para observar conversa, armazenar mídia/transcrição, usar dados na memória e acessar contatos. Nenhum consentimento é inferido apenas por uso da conta.
- Projetar retenção, exportação, exclusão/retificação, tratamento de backups e dados derivados antes da admissão persistente de conteúdo pessoal.
- Avaliar a **LGPD** e eventuais obrigações de privacidade e bases legais pertinentes antes de habilitar processamento real de dados de terceiros. Este documento não constitui validação jurídica.
- **Proibido** criar novo L2/L3, VLAN, IP, container, credencial, TTS ou motor de IA sem necessidade comprovada e autorização específica.
- Sem automação de envio, abordagem de contato, respostas à conversa piloto, replicação de áudio, varredura de diretórios pessoais ou operações sobre contatos.
- Provas públicas usam números, interlocutores, mensagens e áudios **sintéticos**.

## 9. Ordem de execução e portas de validação

1. **P0, diagnóstico:** mapear código e evidência, registrar alternativas e risco; nenhuma mudança funcional.
2. **P1, mídia/STT:** habilitar atribuição de conta para compreensão e captura segura dos áudios de saída; testar em isolamento.
3. **P2, resolução de contato:** mapear LID/PN e projetar conexão Contacts Google com consentimento granular; separar implementação de OAuth e sincronização da evolução da identidade.
4. **P3, semântica:** correlacionar mensagens e transcrições dos dois lados e materializar episódios/relações somente nos contratos já existentes ou em expansão justificada.
5. **Gate de integração:** testes unitários de Transport, API, STT, aliases, Google OAuth/People API mockada, PostgreSQL descartável, regressões de governança, CodeQL e revisão de dados privados.
6. **Piloto físico/autenticado:** somente após aprovação do titular, com escopo limitado a uma conversa; evidências locais, horários, estados e rollback. Não alterar a produção nem disparar mensagens automaticamente.

Para cada etapa, entregar: **mudança proposta; referência desta especificação; código existente reutilizado; testes executados; regressões verificadas; dúvidas; riscos; próxima decisão necessária**. Qualquer desvio de escopo exige decisão explícita do titular e atualização versionada desta especificação, nunca uma interpretação silenciosa.

## 10. Registro de decisões e itens ainda abertos

| ID | Estado | Decisão |
| --- | --- | --- |
| D01 | **Decisão do titular** | Áudios manuais enviados pela conta WhatsApp autenticada devem ser compreendidos como fala do proprietário por regra de produto, sem exigência de biometria de voz. |
| D02 | **Invariante de segurança** | D01 não equivale a prova física do aparelho nem delega autoridade de comando; saídas comprovadamente automatizadas prevalecem. |
| D03 | **Decisão do titular** | A Andy deve enxergar os dois lados da conversa para a associação semântica, incluindo texto e voz. |
| D04 | **Decisão do titular** | Contatos do Google são fonte desejada de nomes e números, vinculados à identidade WhatsApp mediante consentimento próprio. |
| D05 | **Invariante técnico** | LID e telefone são identificadores relacionados e fonteados; nome da agenda é alias, não certificação da pessoa. |
| D06 | **Ainda não aprovado** | Política de retenção de áudio e transcrições, exportação/exclusão e uso de provedor externo adicional. |
| D07 | **Ainda não aprovado** | Histórico retroativo em massa, download de áudios antigos, mudanças em rede, deploy ou merge. |
| D08 | **Ainda não aprovado** | Critérios para ativar memória semântica persistente de conversas pessoais e eventual revisão humana. |

## 11. Contratos e fontes existentes a consultar

- `AGENTS.md`: políticas do repositório e validação distribuída.
- `docs/operations/github-first-development-control-loop.md`.
- `docs/architecture/channel-context-dual-track-v1.md`.
- `docs/architecture/andy-live-conversation-archive-v0.md`.
- `docs/adr/0014-recent-history-vs-persistent-memory.md`.
- `docs/memory/provenance.md`.
- `whatsapp-transport-local/src/conversation.js`, `outbound-provenance.js`, `bridge.js`, `inbound.js`, `media.js`.
- `attention_router/application/conversation_live_capture.py` e os módulos atuais de STT e memória.
- Repositório Android: fronteiras de arquitetura, integração SDK, permissões e identidade.
- PRs abertas de outras frentes devem ser examinadas antes de escolher base e migrations, mas **não** se confundem com este piloto.

## 12. Formato obrigatório de conferência nas instruções futuras ao Codex

Toda instrução de implementação associada a este trabalho deverá começar com a exigência:

> Leia integralmente `docs/architecture/andy-whatsapp-semantic-conversation-pilot-v1.md` na revisão/branch exata indicada. Confronte cada decisão com suas regras D01–D08, os contratos reais e os testes existentes; preserve tudo que funciona. Desvios, ambiguidades e conflitos de segurança devem ser reportados antes de qualquer alteração definitiva. Trabalhe em escopo isolado, com evidência e validação, sem merge ou deploy automático.

O documento é **referência versionada de intenção**. Seu avanço de "proposta" a "norma aprovada" exige revisão do titular; não atribuir essa aprovação implicitamente ao Codex ou a esta redação.
