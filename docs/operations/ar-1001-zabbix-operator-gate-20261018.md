# AR-1001 — gate de provisionamento Zabbix (18/10/2026 UTC)

**Estado inicial deste registro:** candidato offline. A ativação operacional de 18/10/2026 está documentada ao final.

## Resultado de inspeção do runtime

O Zabbix ROC e a sonda `andy-reply-health.timer` permaneceram ativos. Os 8 itens atuais e os 2 triggers da resposta automática da Andy estavam habilitados; o incidente funcional segue em PROBLEM com `probe_ok=1`. O Relay e o MAESTRO standalone estavam saudáveis na última inspeção.

O SHA `b600d7d01c12af7b4f96a6c55832b279c406551c` da PR #375 passou os quatro checks distribuídos: Python, PostgreSQL, Docker e Transport. A consulta integrada já passou no PostgreSQL **descartável** do CI03. Isso **não** atesta seu tempo de resposta no banco operacional.

## Backups e bloqueio

Foi iniciado um backup via API oficial Zabbix 7.4, com credenciais preexistentes, sem imprimir segredos. O `user.login` retornou, mas `host.get` foi rejeitado. A tentativa de obter diagnóstico sanitizado do erro foi **bloqueada pela ferramenta remota**. Não usar outro caminho para contornar esse bloqueio.

Foi criada uma cópia **somente host-private** dos quatro arquivos existentes: `reply_health.py`, unidade, timer e estado do detector; cópias verificadas byte a byte com SHA-256, permissões `0600` sob diretório `0700`. Endereço privado, valores do estado e digest não entram no Git. O diretório foi anotado internamente com `zabbix_api_backup=BLOCKED_NOT_CREATED`. **Essa cópia não autoriza ativação**, pois a parte Zabbix ainda não possui snapshot integral.

Não houve mutação em nenhum item, trigger, template, media, ação ou banco operacional.

## Novo operador offline e caminho de recuperação

`ops/observability/failure_catalog/zabbix_items_admin.py` encapsula acesso à API oficial por métodos permitidos, inicialmente com três modos:

1. **plan (padrão):** verifica manifesto e imprime apenas intenção offline; não usa a rede.
2. **snapshot:** exige parâmetros privados explícitos; autentica, identifica host exato, compara oito itens antigos e duas triggers, grava cópia com `0600` e checksum fora do Git. Interrompe sem backup se a API falhar.
3. **create-disabled:** exige snapshot anterior, manifesto idêntico e ausência de drift da configuração original. Só então cria **sete itens trapper unsigned DESABILITADOS**; não emite dados, não inicia timer, não ativa triggers e não altera o Relay.

O incremento de ativação acrescenta `enable` e `disable`: ambos exigem os sete IDs do checkpoint privado e conferem tipo e chaves; `enable` também exige hash do script instalado e ausência de drift dos itens/triggers originais. `disable` aceita drift dinâmico desses originais para permitir rollback. Só os sete IDs novos são enviados a `item.update`, com confirmação posterior da API. Se algum item novo já existir, `create-disabled` recusa criação parcial. A lista permitida após autenticação inclui `host.get`, `item.get`, `trigger.get`, `problem.get` (somente leitura), `item.create` e `item.update`; login e consulta da versão também são permitidos. Falhas retornam códigos sanitizados, sem `error.data`, tokens ou corpo da resposta.

O teste `tests/test_operational_ar1001_zabbix_admin.py` utiliza exclusivamente API simulada: recusa host/trigger divergentes, impede duplicação, confirma modo plan offline, verifica snapshot `0700/0600`, exige backup antes de item.create, confere criação desabilitada e limita enable/disable aos IDs do checkpoint. **Nenhuma chamada dessa bateria acessa a API real.**

## Critérios para liberar ativação

- [ ] Corrigir a rejeição `host.get` por procedimento normal e autorizado, preservando os controles da ferramenta remota.
- [ ] Realizar backup privado íntegro pelo caminho oficial Zabbix API e conferir 8 itens + 2 triggers.
- [ ] Comparar execução/custo do SQL integrado em PostgreSQL operacional **somente por execução autorizada**; nunca contornar bloqueio anterior.
- [ ] Só então criar 7 itens desabilitados pela API e registrar os novos IDs exclusivamente em checkpoint privado.
- [ ] Instalar integração do parser à **sonda existente**, sem criar outra instância.
- [ ] Habilitar itens somente após teste de origem e prova de contadores por 3 ciclos; conferir `probe_ok` e trigger antigo não alterado.
- [ ] Se houver divergência, rollback limitado ao script/timer e disable dos 7 itens novos; preservar estado/eventos da falha e não criar recovery artificial.

**Sem mudança do contrato Relay/MAESTRO** enquanto não houver evidências Zabbix validamente preenchidas.

## Retomada operacional — 18/10/2026 UTC

- A API oficial respondeu `apiinfo.version=7.4.14`. O mesmo cliente JSON-RPC com `Authorization: Bearer` concluiu `host.get`, `item.get` e `trigger.get` no host lógico existente. A rejeição anterior não se repetiu; como o erro original foi sanitizado e seu detalhe não foi obtido, sua causa exata permanece **indeterminada**, sem atribuição a permissão, versão ou formato.
- O snapshot privado do operador foi criado em diretório novo com modos `0700/0600`, 8 itens antigos, 2 triggers e checksum SHA-256 verificado. Um segundo arquivo privado, também exclusivo e verificado, guarda as definições completas desses 8 itens e 2 triggers via API oficial (`output=extend`, tags, preprocessamento, funções e dependências). As quatro cópias host-private anteriores continuam byte a byte iguais aos originais. Nenhum backup anterior foi sobrescrito.
- A consulta integrada rodou no PostgreSQL operacional em transação `READ ONLY`, com `statement_timeout=3s`. Em três pares com o SQL da sonda, as sete colunas legadas foram idênticas. Tempos totais cliente+consulta: legado 127,3/150,0/129,1 ms; integrado 135,5/127,4/133,1 ms. Uma agregação independente confirmou os sete contadores adicionais `11/11/11/11/0/6/5` na janela observada. Esses números são **contagens de eventos, decisões, outbox e janelas**, não prova de resposta obrigatória ou causa raiz. Nenhum registro, cursor, schema ou migração foi alterado.
- Só depois dessas provas, `create-disabled` criou os sete itens trapper `uint` do manifesto. Leitura posterior confirmou 7/7 com `status=1`, 8/8 legados habilitados e recentes, `probe_ok=1`, os 2 triggers sem drift e **uma** ocorrência funcional aberta, sem duplicação ou recovery. IDs novos e ID da ocorrência foram gravados apenas em checkpoint privado.
- A sonda e o timer originais seguem intactos; o último `zabbix_sender` observado confirmou `processed: 8; failed: 0`. Portanto as três amostras da **nova coleta Zabbix** ainda não existem. A implantação e a ativação permanecem pendentes.

### Rollback para a etapa de ativação futura

Antes de habilitar os sete itens, registrar hash e modo do script ativo, status/IDs dos 15 itens, valor dos dois triggers e ID da ocorrência aberta. Se a integração à sonda falhar, parar apenas `andy-reply-health.timer`, restaurar atomicamente `reply_health.py` da cópia privada já verificada, conferir SHA-256 e permissão, desabilitar somente os sete IDs do checkpoint privado pela API oficial e religar o mesmo timer. Confirmar um ciclo com `processed: 8; failed: 0`, `probe_ok=1`, os oito valores legados recentes e o mesmo incidente sem recovery artificial. Não restaurar o arquivo de estado, não apagar histórico, não tocar em Relay, MAESTRO, Telegram ou PostgreSQL. Se a API de rollback não estiver acessível, manter o timer parado e registrar o bloqueio antes de qualquer nova publicação.

## Ativação controlada e aceite — 18/10/2026 UTC

1. Pré-checagem repetiu hash do script original e das quatro cópias host-private, inventário API de 8 itens antigos habilitados, 7 novos desabilitados, 2 triggers sem drift e o mesmo problema aberto. Um checkpoint privado `0600` guardou os IDs e estados antes da mudança. O candidato autocontido incorporou a SQL integrada e o parser versionado; comparação AST confirmou `next_state`, `sender_lines`, `send`, `load_state` e `save_state` sem alterações. Regressão simulou 15 métricas e falha fechada com somente `probe_ok=0`. Quatro checks distribuídos do SHA de código `50c8faa1a90031f1587d0f646872445d3eedc9ff` passaram antes do rollout.
2. Somente `andy-reply-health.timer` foi parado durante a troca atômica do `reply_health.py` com modo `0700` e hash verificado. `item.update` habilitou exclusivamente os 7 IDs do checkpoint; os 8 legados e 2 triggers foram comparados antes/depois. O mesmo timer foi religado. Não houve segundo processo permanente, serviço, timer, contêiner ou motor.
3. Ciclos consecutivos da mesma unidade às **02:34:02, 02:36:02 e 02:38:02 UTC** tiveram `zabbix_sender processed: 15; failed: 0`. Os 15 relógios avançaram em intervalos de 120 s. Em cada ciclo, a API confirmou 8 itens antigos e 7 novos habilitados, `probe_ok=1`, `functional_stalled=1`, dois triggers sem drift e **o mesmo ID de problema aberto**. Os sete contadores novos corresponderam às leituras PostgreSQL read-only, respectivamente `[13,13,13,13,0,6,7]`, `[14,14,14,14,0,6,8]` e `[14,14,14,14,0,6,8]`, na ordem do manifesto. `history.get` confirmou os 45 registros exatos (15 itens × 3 ciclos); `event.get` confirmou **zero novos eventos** nos dois triggers desde o checkpoint pré-ativação. A prova privada tem modo `0600` e SHA-256 `30c49e51cdd9062ef4ad048945bd26928b9c847b74d02ca2eeb616b622ee5308`.
4. Uma comparação preliminar após o envio de 02:32:02 mostrou o PostgreSQL uma unidade acima em vários contadores. A última falha daquele momento tinha epoch 16 s posterior ao relógio Zabbix; era uma entrada nova após o ciclo, não discrepância de valores no mesmo instante. O timer foi parado para diagnóstico e religado sem alterar script, itens ou estado. A aceitação usou os três ciclos posteriores, cada um comparado imediatamente e confirmado no histórico oficial.

**Estado final:** enriquecimento AR-1001 operacional no Zabbix; somente agregados numéricos foram publicados. A sonda existente continua responsável pelo estado persistente e recuperação real. Backups, checkpoint e procedimento acima permanecem válidos para rollback futuro. Relay, MAESTRO, Telegram, ações e contratos de transporte não foram alterados. O manifesto original permanece sem edição para manter o hash vinculado ao snapshot e à função `disable` do operador.

Referências oficiais: https://www.zabbix.com/documentation/7.4/en/manual/api/reference/item/get, https://www.zabbix.com/documentation/7.4/pt/manual/api/reference/item/create e https://www.zabbix.com/documentation/7.4/pt/manual/api.
