# Knowledge Retrieval Compatibility Module

`src/tau2/knowledge/` contains the embedding, BM25, grep, reranking, and sandbox
components retained for reading historical retrieval configurations. The former
`banking_knowledge` domain is not an active registry target in the Gaia2-only
runtime, so new benchmark runs do not use `--retrieval-config`.

Keep serialized retrieval settings readable when changing these modules. Do not
add new domain-specific entry points here; active Gaia2 evaluation uses the
content-addressed task snapshots and application tools under
`src/tau2/domains/gaia2/`.
