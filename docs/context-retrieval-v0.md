# Context Retrieval V0

Context Retrieval V0 selects a small, relevant subset of the represented owner's Personal Context for a specific query.

## Contract

- Retrieval is tenant-scoped.
- The represented owner is resolved through Personal Context, not through transport identifiers.
- V0 searches only Personal Context claims and facts already admitted by the Personal Context boundary.
- SECRET claims, inactive claims and expired values therefore remain excluded fail-closed.
- Retrieval is bounded and deterministic.
- V0 uses lexical evidence only: normalized query terms are matched against predicate names and structured/text values.
- A candidate with no lexical evidence is not returned.
- Result ordering is deterministic and favors stronger term coverage and higher confidence.
- The result preserves provenance metadata so later reasoning can explain where knowledge came from.

## Knowledge is not authority

A retrieved item means only that Andy found knowledge relevant to the current query. Retrieval does not authorize disclosure, execution, sharing, or mutation. Disclosure and action authority remain separate boundaries.

For example, retrieving `payment.pix.primary` does not mean the value may be sent to the current contact. A later disclosure decision must independently evaluate whether sharing that information is authorized in the current context.

## Why lexical first

This boundary is intentionally provider-agnostic. Embeddings, vector indexes or a semantic reranker can later replace or augment the ranking implementation without changing the caller contract.

Starting deterministic gives us:

- explainable matches;
- stable tests;
- no new provider dependency;
- no accidental cross-tenant vector search;
- a clear baseline against which semantic retrieval can be measured.

## Non-goals

V0 does not:

- create embeddings;
- use an LLM to rank context;
- inject Personal Context automatically into prompts;
- grant disclosure authority;
- search artifacts/documents;
- search other people's context;
- mutate memory;
- add a UI.

Artifact retrieval and semantic/vector retrieval are separate subsequent boundaries.
