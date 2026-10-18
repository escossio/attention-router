# AR-1001 — gate de provisionamento Zabbix (18/10/2026 UTC)

**Estado:** candidato offline. **NÃO instalado no Zabbix real**.

## Resultado de inspeção do runtime

O Zabbix ROC e a sonda `andy-reply-health.timer` permaneceram ativos. Os 8 itens atuais e os 2 triggers da resposta automática da Andy estavam habilitados; o incidente funcional segue em PROBLEM com `probe_ok=1`. O Relay e o MAESTRO standalone estavam saudáveis na última inspeção.

O SHA `b600d7d01c12af7b4f96a6c55832b279c406551c` da PR #375 passou os quatro checks distribuídos: Python, PostgreSQL, Docker e Transport. A consulta integrada já passou no PostgreSQL **descartável** do CI03. Isso **não** atesta seu tempo de resposta no banco operacional.

## Backups e bloqueio

Foi iniciado um backup via API oficial Zabbix 7.4, com credenciais preexistentes, sem imprimir segredos. O `user.login` retornou, mas `host.get` foi rejeitado. A tentativa de obter diagnóstico sanitizado do erro foi **bloqueada pela ferramenta remota**. Não usar outro caminho para contornar esse bloqueio.

Foi criada uma cópia **somente host-private** dos quatro arquivos existentes: `reply_health.py`, unidade, timer e estado do detector; cópias verificadas byte a byte com SHA-256, permissões `0600` sob diretório `0700`. Endereço privado, valores do estado e digest não entram no Git. O diretório foi anotado internamente com `zabbix_api_backup=BLOCKED_NOT_CREATED`. **Essa cópia não autoriza ativação**, pois a parte Zabbix ainda não possui snapshot integral.

Não houve mutação em nenhum item, trigger, template, media, ação ou banco operacional.

## Novo operador offline e caminho de recuperação

`ops/observability/failure_catalog/zabbix_items_admin.py` encapsula acesso à API oficial por métodos permitidos, com três modos:

1. **plan (padrão):** verifica manifesto e imprime apenas intenção offline; não usa a rede.
2. **snapshot:** exige parâmetros privados explícitos; autentica, identifica host exato, compara oito itens antigos e duas triggers, grava cópia com `0600` e checksum fora do Git. Interrompe sem backup se a API falhar.
3. **create-disabled:** exige snapshot anterior, manifesto idêntico e ausência de drift da configuração original. Só então cria **sete itens trapper unsigned DESABILITADOS**; não emite dados, não inicia timer, não ativa triggers e não altera o Relay.

Não existe modo de ativação dos itens nesse operador; a ativação exigirá implantação separada do coletor original, comprovação de consulta real com autorização, e verificação de 3 ciclos. Se algum item novo já existir, o operador recusa criação parcial ao invés de sobrescrever. Somente as ações `host.get`, `item.get`, `trigger.get`, `item.create` são utilizadas após autenticação; login também é permitido, sem API genérica para mutações amplas. Falhas retornam códigos sanitizados, sem `error.data`, tokens ou corpo da resposta.

O teste `tests/test_operational_ar1001_zabbix_admin.py` utiliza exclusivamente API simulada: recusa host/trigger divergentes, impede duplicação, confirma modo plan offline, verifica snapshot `0700/0600`, exige backup antes de item.create e confere status desabilitado. **Nenhuma chamada dessa bateria acessa a API real.**

## Critérios para liberar ativação

- [ ] Corrigir a rejeição `host.get` por procedimento normal e autorizado, preservando os controles da ferramenta remota.
- [ ] Realizar backup privado íntegro pelo caminho oficial Zabbix API e conferir 8 itens + 2 triggers.
- [ ] Comparar execução/custo do SQL integrado em PostgreSQL operacional **somente por execução autorizada**; nunca contornar bloqueio anterior.
- [ ] Só então criar 7 itens desabilitados pela API e registrar os novos IDs exclusivamente em checkpoint privado.
- [ ] Instalar integração do parser à **sonda existente**, sem criar outra instância.
- [ ] Habilitar itens somente após teste de origem e prova de contadores por 3 ciclos; conferir `probe_ok` e trigger antigo não alterado.
- [ ] Se houver divergência, rollback limitado ao script/timer e disable dos 7 itens novos; preservar estado/eventos da falha e não criar recovery artificial.

**Sem mudança do contrato Relay/MAESTRO** enquanto não houver evidências Zabbix validamente preenchidas.

Referências oficiais: https://www.zabbix.com/documentation/7.4/en/manual/api/reference/item/get, https://www.zabbix.com/documentation/7.4/pt/manual/api/reference/item/create e https://www.zabbix.com/documentation/7.4/pt/manual/api.
