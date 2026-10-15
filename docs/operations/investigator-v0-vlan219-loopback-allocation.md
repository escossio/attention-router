# Rede do Investigador V0 — alocação VLAN 219 e loopback

**Data:** 2026-10-15 · **Estado:** lado AGT Linux PREPARADO; lado MikroTik sob gestão do operador; contêiner e sua interface loopback AINDA NÃO CRIADOS. Janela [#356](https://github.com/escossio/attention-router/issues/356); PR documental #344.

## Regra de endereçamento do laboratório

- Um **serviço com VLAN exclusiva N** utiliza loopback lógico `200.0.0.N/32`, colocado **no namespace de rede do contêiner**, nunca diretamente no loopback do host AGT.
- O endereço do transporte é independente: gateway/contêiner em rede VLAN de ponto-a-ponto /30, com CIDR reservado previamente. Para um `/30`, há somente dois endereços utilizáveis.
- Uma VLAN compartilhada não pode entregar o mesmo `/32` a dois contêineres. Exemplo observado: a VLAN 212 já tem duas instâncias de ingress; **não aplicar `200.0.0.212` às duas**. Para migrar esses serviços, exigir VLAN exclusiva por identidade, ou VIP com único proprietário/gateway documentado.
- Manter MGMT/Zabbix/OTel/Grafana como observabilidade da origem, rota e destino, incluindo testes L3, ARP/TCP separados. Nenhuma permissão operacional é concedida por estar numa VLAN.

## Alocação do Investigador

| Item | Valor | Estado |
| --- | --- | --- |
| Papel | Investigador Operacional V0 | Planejado |
| VLAN | `219` | Interface no AGT criada |
| Trunk físico Linux | `enp1s0` | Existente, preservado |
| Subinterface | `enp1s0.219` | Criada/UP sem IPv4 |
| Configuração persistente | `/etc/network/interfaces.d/andy-vlan219-investigator` | Arquivo novo, incluído por `/etc/network/interfaces` |
| Rede Docker | `andy-vlan219-investigator-stage` | macvlan criada, 0 contêineres anexados |
| CIDR de transporte | `10.77.10.72/30` | IPAM Docker reservado; não ativo no roteador por este trabalho |
| Endereço de rede | `10.77.10.72` | Reservado |
| Gateway MikroTik | `10.77.10.73` | **A configurar pelo operador** |
| Endereço do contêiner | `10.77.10.74/30` | Reservado, **não atribuído** |
| Broadcast | `10.77.10.75` | Reservado |
| Loopback do contêiner | `200.0.0.219/32` | **Ainda não atribuído; contêiner não existe** |
| Rota de retorno na MikroTik | `200.0.0.219/32` via `10.77.10.74` | **A configurar pelo operador** |

**Validações executadas:** rede `10.77.10.72/30` sem sobreposição com Docker networks existentes; interface 219 ausente antes da criação; após criação, interface presente sem endereço IPv4, rede macvlan com parent/CIDR/gateway exatos e nenhum contêiner conectado. As VLANs 210–218 já eram usadas, incluindo uma VLAN 212 compartilhada. Não modificar configurações e conexões dessas VLANs por inferência.

## Isolamento e limitação crítica de endereços

`200.0.0.0/24` **não é espaço privado RFC 1918**. O nome 'loopback' descreve a interface em que um `/32` é atribuído, mas não transforma a faixa em privada. Seu uso no laboratório pode sobrepor destinos globais reais. Não anunciar, exportar rotas para WAN/peers/VPN não autorizados, fazer NAT dessa origem para a internet ou disponibilizar a rede pública sem decisão explícita. Restringir por firewall/roteamento; considerar renumeração futura para bloco realmente privado quando o operador revisar o endereçamento.

## Pendências antes de ligar o motor

1. Operador configura a VLAN 219 no trunk MikroTik, IP `10.77.10.73/30`, rota host `/32` via `10.77.10.74`, e políticas de isolamento do endereço global. Nada disso foi executado via AGT.
2. Construir contêiner do Investigador com imagem/versionamento e controles; conectar somente à macvlan `andy-vlan219-investigator-stage` com IP estático `10.77.10.74`. Não anexar o contêiner de um serviço não aprovado.
3. No instante de criação do contêiner, injetar `200.0.0.219/32` na interface `lo` **do namespace de rede desse contêiner**, preferencialmente por um bootstrap supervisionado no host com `nsenter`, sem conceder `CAP_NET_ADMIN` permanente ao processo Investigador. Garantir reinjeção após recriação, sem tocar no host `lo`.
4. Validar ida e volta com verificações L2/ARP, ping e TCP por destino, logs MGMT/Zabbix/OTel e uma investigação somente-leitura sintética; não declarar pronta a rota com base na existência do Docker network.
5. Submeter registro de IPAM/Host Registry e o contrato positivo de admissão do Investigador à revisão. ADR 0022 permanece Proposed; sem deploy real por este documento.

**Autoridade:** usuário administra MikroTik; assistente cria/verifica somente componentes Linux autorizados. **Sem merge automático.**