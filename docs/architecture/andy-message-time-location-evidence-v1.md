# Correlação temporal: mensagens WhatsApp e posições do dispositivo Android

Status: proposta isolada; retenção histórica e rastreamento desligados por padrão.

## Diagnóstico verificado
O arquivo de conversas registra mensagens e o horario original sent_at. A API Android V0.4A registra somente o último ponto por dispositivo/tenant em client_location_snapshots; atualizações sobrescrevem o ponto anterior. O app atual envia o ponto somente quando o usuário escolhe Compartilhar localização. Não existe sequência histórica reconstruível a partir das quatro posições já existentes.

## Evidência, não inferência
Um GPS armazenado registra o lugar estimado do dispositivo, não prova onde uma pessoa estava nem qual aparelho enviou uma mensagem WhatsApp. A associação só é candidata quando:
- Mesma identidade OWNER autenticada e mesmo tenant para posição e mensagem;
- Mensagem WhatsApp outbound com speaker_basis REPRESENTED_OWNER; UNKNOWN_FROM_ME ou autor externo não passam;
- GPS registrado até 60 segundos antes/depois do horário original da mensagem, não baseado no horário de chegada;
- Um único dispositivo Android ativo tem amostras nesse intervalo;
- Precisão PRECISE, com raio declarado de erro máximo de 250 metros.

O retorno DEVICE_POSITION_NEAR_MESSAGE é evidência de aparelho próximo no tempo, não afirma presença humana. Respostas explicitam NO_TIME_MATCHED_LOCATION, AMBIGUOUS_DEVICES, LOCATION_TOO_IMPRECISE e AUTHORSHIP_NOT_PROVEN quando não houver prova suficiente. Sem extrapolação entre dias, sem reconstrução de trajetos e sem copiar coordenadas para o texto das conversas.

## Contratos
- Nova tabela append-only client_location_observations, com deduplicação por tenant/dispositivo/horário, tempo de captura, precisão, identidade e referência do dispositivo.
- O endpoint preexistente PUT /api/v1/client/location/current permanece pontual, sem alimentar histórico.
- Novo PUT /api/v1/client/location/context/observations exige ClientSession, campo context_history_opt_in=true, OWNER ativo único e flag CLIENT_LOCATION_HISTORY_ENABLED=true. O servidor deriva tenant/dispositivo/humano da sessão.
- Novo GET /api/v1/client/location/message/{message_id} exige sessão OWNER e faz correlação em leitura, com status e margem temporal. Não executa ações nem alimenta memória semântica automaticamente.
- Não importar retroativamente posições antigas ao novo histórico, pois o consentimento para compartilhar a posição atual não autoriza rastreamento.

## Android
Controle explícito para ativar/desligar coleta periódica, com notificação persistente, ação Parar coleta e foreground service. Intervalo de coleta aproximadamente 60 segundos. A sessão permanece criptografada no Android Keystore. Encerrar a coleta caso a autoridade/consentimento/permissão deixem de existir. Android não deve recriar o serviço silenciosamente após encerramento do processo. Sem ACCESS_BACKGROUND_LOCATION. Não transmitir por WhatsApp nem inferir autoria.

## Limites de segurança e rollout
Histórico detalhado de localização é dado extremamente sensível. Falta definir retenção, exclusão, exportação, proteção de backup e criptografia em repouso antes de produção. As quatro localizações antigas não servem para extrapolar os pontos de mensagens posteriores. Tests são sintéticos e validação real depende de um Android físico, consentimento e APK validado.

Uma outra branch de revisão, PR #404, também contém uma migração Alembic chamada 0062. Resolver conflito de revisions antes de mesclar as duas frentes. Não subir o novo schema em produção sem certificar PostgreSQL e plano de rollback.

Live continuity: WhatsApp permanece inalterado. Somente amostras novas após opt-in. Historical acceleration: não recriar o passado sem GPS real; não alterar watermark.

Rede: Android inicia conexão HTTPS com a API já existente. API acessa o PostgreSQL atual. Nenhum novo contêiner, IP fixo, VLAN, bridge, porta, roteamento ou autenticação alternativa.
