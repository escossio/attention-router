# Investigador V0 — VLAN 219, transporte /29 e roteamento de loopback

**Data da configuração:** 2026-10-15 · **Estado:** RouterOS **CONFIGURADO E VERIFICADO**, Linux/Docker **CONFIGURADOS E VERIFICADOS**, contêiner/loopback do Investigador **AINDA NÃO CRIADOS**. Registros de execução NET-ROUTEROS-001–007 na [janela #356](https://github.com/escossio/attention-router/issues/356). PR documental #344.

## 1. Regra de endereçamento

- Cada contêiner recebe **IP de transporte dentro do /29 da VLAN** e **loopback próprio `/32`** dentro de seu namespace de rede. O IPv4 de loopback não deve ser atribuído ao AGT.
- Convenção do endpoint principal de uma VLAN `N`: `200.0.0.N/32`; cada outro contêiner na mesma VLAN recebe **outro `/32` disponível, globalmente único e registrado no inventário**, e uma rota host correspondente, sem duplicar identidade de outra VLAN.
- Com gateway consumindo o primeiro IP utilizável do `/29`, restam **cinco endereços utilizáveis para contêineres**.
- **Rotear apenas `/32` realmente especificado para cada dono**, sem rota resumida `/24`. `200.0.0.0/24` é bloco público e não deve ser anunciado na WAN, em VPN/peering ou mascarado como origem pública. Permitir somente os caminhos internos autorizados.
- MGMT/Zabbix/OTel/Grafana devem monitorar origem, caminho de rede, ARP, TCP e destino, distinguindo erro de permissão de erro de conectividade.

## 2. Alocação VLAN 219

| Elemento | Valor | Estado |
| --- | --- | --- |
| Função | Investigador Operacional V0 | Implantação do motor pendente |
| MikroTik | RB3011UiAS / RouterOS 7.23.5 | Autenticado e verificado |
| VLAN Interface RouterOS | `ANDY219-GW`, VLAN ID `219`, parent `br-andy-l2` | Configurada/renomeada |
| Descrição RouterOS | `ANDY-219 Investigator` | Configurada |
| Switch chip | `switch1`, VLAN219, portas `ether2,switch1-cpu` | Publicada e verificada |
| Endereço gateway MikroTik | `10.77.10.73/29` | Configurado e verificado |
| Sub-rede | `10.77.10.72/29` | Ativa no roteador e Docker IPAM |
| Contêiner previsto | `10.77.10.74/29` | Reservado, não atribuído |
| Outros IPs possíveis da VLAN | `10.77.10.75–10.77.10.78` | Livres/reserváveis após novo inventário |
| Broadcast da VLAN | `10.77.10.79` | Não atribuível |
| Loopback do contêiner | `200.0.0.219/32` | Reservado, **não atribuído** |
| Rota estática no RouterOS | `200.0.0.219/32` via `10.77.10.74` | Criada e verificada; sem destino ainda |
| Subinterface Linux | `enp1s0.219` | UP, sem endereço L3 no AGT |
| Persistência Linux | `/etc/network/interfaces.d/andy-vlan219-investigator` | Existente |
| Docker Network | `andy-vlan219-investigator-stage` | macvlan, parent `enp1s0.219`, gateway `.73`, IPAM `/29`, zero anexos |

**Configuração do RouterOS foi aplicada explicitamente com autorização delegada pelo operador.** O script remoto conferiu contagens prévias (uma VLAN218, uma VLAN219, nenhum switch entry/IP/rota .219); os cinco comandos da mudança retornaram RC=0 e as cinco verificações posteriores PASS. Nenhuma mudança de firewall, NAT, rota default, gerenciamento, serviços externos ou VLANs 210–217.

## 3. Padronização da VLAN 218

Interface RouterOS já existente `vlan218` renomeada para **`ANDY218-GW`** e comentário padronizado para **`ANDY-218 Channel Sync`**. IP já existente **`10.77.10.65/29`** e publicação switch1 VLAN218 foram preservados. Nenhuma VLAN extra foi criada no lugar dela.

## 4. Situação das outras VLANs e de seus loopbacks

Inspeção Linux read-only dos namespaces `lo` de contêineres macvlan:

| VLAN | IPs de contêineres observados | Loopbacks 200.0.0.x já existentes |
| --- | --- | --- |
| 210 | `10.77.10.2/29` | Nenhum |
| 211 | `10.77.10.10/29` | Nenhum |
| 212 | `10.77.10.18/29`, `10.77.10.19/29` | Nenhum — dois proprietários distintos |
| 213 | `10.77.10.26/29` | Nenhum |
| 214 | `10.77.10.34/29` | Nenhum |
| 215 | `10.77.10.42/29` | Nenhum |
| 216 | `10.77.10.50/29` | Nenhum |
| 217 | `10.77.10.58/29` | Nenhum |
| 218 | Sem contêiner anexado à macvlan de staging | Nenhum |
| 219 | Sem contêiner anexado à macvlan de staging | `200.0.0.219/32` reservado |

**Não foram criadas rotas `/32` adicionais para 210–218**, pois os endpoints ainda não detêm seus endereços de loopback. Criá-las antes da atribuição persistente fabricaria conectividade inexistente. A alocação secundária (caso VLAN212) requer um identificador global único no Host Registry, sem conflitos com a convenção de outras VLANs.

## 5. Pendência operacional limitada

1. Criar o contêiner Investigador com `10.77.10.74/29`, conectado à macvlan da VLAN219, sem exposição ampla de serviços.
2. Configurar `200.0.0.219/32` **no loopback do namespace do contêiner**, de modo persistente após reinícios e sem conceder `CAP_NET_ADMIN` permanente ao processo de investigação.
3. Testar efetivamente L2/ARP, ida e volta por TCP/ICMP com rota host, observabilidade e isolamento. RouterOS/AGT estão preparados; **não declarar conectividade funcional com contêiner inexistente**.
4. Separadamente, provisionar loopbacks exclusivos e persistentes nos contêineres antigos, registrar seus IPs no Host Registry e criar somente suas rotas host `/32` quando confirmados.
5. A ADR 0022 da admissão permanece Proposed/REQUEST_CHANGES; não ativar modelo real nem ingress sem gate próprio.

**Regra de governança:** mudanças de laboratório executadas não equivalem a autorização de acesso operacional do modelo. Nenhum merge foi feito.