# Investigador V0 — orçamento de volume das evidências

**Status:** PROPOSED · **referência:** AR-OPS-INV-V0 (PR #344), implementação offline proposta no PR #352.

## Problema e decisão

Mesmo uma consulta autorizada pode devolver um documento gigantesco ou payload impossível de serializar. O broker deve impedir que observações desse tipo sejam armazenadas indefinidamente ou apresentadas ao modelo, sem apagar evidências válidas coletadas antes.

O protótipo V0 acrescenta duas cotas explícitas ao orçamento de investigação: `max_evidence_bytes` (limite por resposta, default 16 KiB) e `max_total_evidence_bytes` (limite agregado de respostas retidas, default 64 KiB). Os defaults são **parâmetros de laboratório**, não limites de produção aprovados.

## Medição e comportamento

- O tamanho é medido em bytes de uma representação JSON canônica simplificada da observação junto com `source_reference`, após o conector retornar e antes de guardar uma cópia independente.
- Resposta maior que a quota individual: `EVIDENCE_BYTES_PER_QUERY_EXCEEDED`.
- Soma das evidências que ultrapassa a quota acumulada: `EVIDENCE_BYTES_TOTAL_EXCEEDED`.
- Observação sem serialização JSON segura (inclusive NaN): `EVIDENCE_ENCODING_INVALID`.
- Para estouro de volume, o estado é `limited`; consultas anteriores válidas continuam em `partial_evidence` e o encerramento é auditado.
- O registro genérico de auditoria inclui apenas identificador da chamada e código de limitação; nunca o conteúdo da resposta, segredo ou mensagem livre.

## Limites da medida

Este mecanismo **não impede** que uma ferramenta real já tenha recebido, copiado ou acumulado o payload antes de responder. Cada conector futuro precisa também de limites na própria origem, na conexão e na paginação, com abortos e tetos de tamanho. A representação JSON mede um envelope lógico; não é uma métrica geral de uso de RAM.

O tamanho do prompt do LLM também é controlado à parte pelo `max_tool_response_bytes` do SDK. Volume total, custos de tokens, duração total, concorrência e timeouts preemptivos permanecem gates independentes. Um limite de retenção aprovado não equivale a garantia de custo do modelo.

## Gate de revisão

- [ ] Testar respostas maiores que cada limite e verificar falha fechada.
- [ ] Testar quota agregada com múltiplas consultas e retenção parcial.
- [ ] Garantir que erros e logs não copiem bytes das evidências.
- [ ] Validar coerência com restrição `tools` e `source_reference` no perfil tipado do PR #351.
- [ ] Definir cifras para piloto somente após medições e revisão de segurança; nenhuma credencial ou dado real nesta fase.

Sem autorização de acesso operacional, deploy no AGT, alterações em rede ou MikroTik.