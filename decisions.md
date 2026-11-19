---
title: Decisions
type: core
status: active
tags: [type/core, status/active]
created: 2026-11-16
updated: 2026-11-16
---

# Decisions

A structured record of architectural, tooling, and workflow decisions.

## Decision Log

### D-001: Plain Markdown Vault with YAML Frontmatter

- **Status**: Accepted
- **Context**: Ensuring universal portability across Markdown editors and AI toolchains.
- **Decision**: All notes standardize on YAML frontmatter with consistent type, status, and tag fields.
- **Consequence**: Full offline searchability and zero vendor lock-in.

### D-002: Graph Ontology Separation

- **Status**: Accepted
- **Context**: Separating note prose from relational knowledge graph models.
- **Decision**: Define ontology schema in `graph/schema.md` and persist extracted triples in `graph/entities.json`.
- **Consequence**: Structured traversal queries without parsing Markdown on every request.
