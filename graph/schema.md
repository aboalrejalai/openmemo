---
title: Knowledge Graph Ontology & Schema (`graph/schema.md`)
type: core
status: active
tags: [type/core, status/active]
created: 2026-11-16
updated: 2026-11-16
---

# Knowledge Graph Ontology & Schema (`graph/schema.md`)

> Formal entity-relationship schema powering multi-hop graph traversal and associative reasoning for memory systems.

---

## Entity Types (`EntityType`)

1. **`Person`**: Individuals in the user's professional, academic, or collaborative network.
2. **`Organization`**: Companies, agencies, or institutions (employers, partners, universities).
3. **`Client`**: Commercial accounts or contracted engagements.
4. **`Project`**: Systems, repositories, software builds, and tools.
5. **`Technology`**: Programming languages, frameworks, hardware specs, and AI models.
6. **`Hackathon`**: Competition events and challenge tracks.
7. **`Discipline`**: Cross-cutting expertise domains (e.g., software engineering, systems design).
8. **`Profile`**: Public professional presence (GitHub, LinkedIn, portfolio, X).

---

## Relationship Types (`RelationType`)

| Relation | Domain | Range | Description |
| :--- | :--- | :--- | :--- |
| `FOUNDER_OF` | `Person` | `Organization` / `Project` | Person founds an organization or initiates a project. |
| `EMPLOYED_AT` | `Person` | `Organization` | Formal employment or contract engagement. |
| `PARTNER_WITH` | `Person` | `Person` / `Organization` | Strategic alliance or partner relationship. |
| `REFERRED_BY` | `Client` | `Person` | Client account originated via an introducer. |
| `SERVICES_CLIENT`| `Organization` / `Person` | `Client` | Organization or person provides professional services to a client. |
| `BUILT_WITH` | `Project` / `Client` | `Technology` | Tech stack dependency or hardware integration. |
| `INTEGRATES_WITH`| `Project` | `Project` / `External` | Platform interoperability or API connection. |
| `WON_CHAMPIONSHIP` | `Project` | `Hackathon` | Official first-place or championship outcome. |
| `SUBMITTED_TO` | `Project` | `Hackathon` | Formal competition submission. |
| `CANDIDATE_FOR` | `Project` | `Hackathon` | Planned or shortlisted entry. |
| `PRACTICES` | `Person` | `Discipline` | Active professional discipline. |
| `HAS_PROFILE` | `Person` | `Profile` | Owns a public profile channel. |
| `USES` | `Person` | `Technology` | Tool actively used (note in `stack/`). |
| `SAVED` | `Person` | `Technology` | Resource kept in bookmark archive; not a usage claim. |

---

## Traversal Query Patterns

- **Multi-Hop Partner Resolution:**
  `Person(Alice) --[PARTNER_WITH]--> Person(Bob) --[REFERRED_BY]--> Client(AcmeCorp)`
- **Employer Platform:**
  `Person(User) --[EMPLOYED_AT]--> Organization(AcmeCorp)` and `Project(AcmeApp) --[BUILT_WITH]--> Tech(React)`
- **Flagship Technology Stack:**
  `Project(OpenMemo) --[BUILT_WITH]--> Tech(Python) + Tech(SQLite)`
