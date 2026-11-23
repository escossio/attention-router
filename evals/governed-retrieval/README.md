# Comparação pareada de leitura contextual

O experimento usa o E5 local fixado pelo projeto e um PostgreSQL descartável no CI03. O corpus contém 12 fontes julgadas, 188 distratores e dois controles fora do escopo. São 200 segmentos elegíveis no mesmo tenant e conta. O arquivo `cases.json` fixa as 16 perguntas, os controles sem suporte e as referências esperadas antes da execução.

No SHA publicado, envie `tests/integration/g7_paired_reader_writer.py` como alvo explícito de pytest ao worker PostgreSQL. O teste usa o esquema da #435 e cria seu próprio banco descartável. A coleta convencional ignora o nome do arquivo. A linha `G7_PAIRED` no log contém somente rótulos sintéticos, contagens e medições.

Os dois braços usam o mesmo corpus, política, consentimentos, 16 sessões de cliente, relógios em avanço e orçamento de três resultados. O primeiro consulta a busca híbrida e confere as permissões de cada fonte. O segundo materializa os candidatos governados e revalida na entrega ao callback. O consentimento é o registro SQLite isolado da fixture. O teste registra os candidatos que cada braço entregou. Geração livre, revisão humana e transporte de produção têm validações próprias.

Cada braço executa uma rajada de 16 consultas. Ao observar a primeira busca no pool, um escritor tenta atualizar um metadado neutro de uma fonte. O relatório inclui tentativas, latência e confirmação do commit. O prazo de 15 s delimita a observação. O teste pós-rajada corrige o texto de uma fonte e retira o recibo de outra, verificando hash e referência em ambos os braços.

A precisão divide acertos pelo número de referências retornadas. Uma resposta vazia, uma recusa e uma execução com erro têm estados distintos. Os percentis de latência usam a ordem das 16 tentativas, incluindo recusas. CPU e máximo de RSS se referem ao processo de teste e o máximo de RSS acumula a partir da inicialização do processo. O resultado é uma amostra sintética, sem limite artificial de p95 ou critério de promoção.
