# Bruce — architecture

> **Honest status (2026-07-16).** Solid arrows are implemented and tested. Dashed arrows are
> implemented but **never executed against the live third party**, because the Qwen Cloud account is
> under a risk-control hold and no Alibaba deployment exists yet. See
> [deployment-verification.md](deployment-verification.md). Nothing on this diagram should be read
> as "working in production" unless that document says so.

## The demonstrated flow

```mermaid
flowchart TB
    subgraph client["Client surfaces"]
        S["Student"]
        IOS["iOS app (SwiftUI)<br/>Share sheet · photo · paste"]
        DI["Dynamic Island /<br/>Live Activity"]
    end

    subgraph ali["Alibaba Cloud — ap-southeast-1 (Singapore)"]
        FC["Function Compute 3.0<br/>custom container · bruce-engine<br/><i>NOT YET DEPLOYED</i>"]
        subgraph api["Bruce engine (FastAPI)"]
            AUTH["JWT verification<br/>user_id only from verified token"]
            INTAKE["/v1/intake<br/>atomic · idempotent"]
            POLICY["Policy + approval<br/>server-authoritative"]
            MISSION["Mission engine<br/>phases · optimistic concurrency"]
        end
        PG[("ApsaraDB PostgreSQL 16<br/>RLS + FORCE RLS · bruce_app role<br/>sources → spans → tasks<br/>missions · approvals · receipts")]
    end

    subgraph qwen["Qwen Cloud (Model Studio)"]
        Q["qwen3.7-plus<br/>multimodal · non-thinking · JSON"]
        RR["qwen3-rerank<br/><i>not on OpenAI-compat endpoint</i>"]
    end

    subgraph ext["External execution"]
        GCAL["Google Calendar<br/>events.insert · events.get"]
    end

    S --> IOS
    IOS -->|"flyer image + JWT"| FC
    FC --> AUTH --> INTAKE
    INTAKE -.->|"1· transcribe pixels"| Q
    Q -.->|"verbatim text"| INTAKE
    INTAKE -->|"2· extract + ground<br/>drop unverifiable spans"| INTAKE
    INTAKE -->|"3· one transaction"| PG
    INTAKE --> MISSION --> PG
    MISSION --> POLICY
    POLICY -->|"one decision:<br/>exact calendar proposal"| IOS
    IOS -->|"approve (idempotent)"| POLICY
    POLICY -->|"4· execute once<br/>caller-supplied event id"| GCAL
    GCAL -.->|"409 on retry = executed once"| POLICY
    POLICY -->|"5· READ BACK"| GCAL
    GCAL -.->|"event resource"| POLICY
    POLICY -->|"6· verified ONLY if<br/>read-back matches"| MISSION
    MISSION --> PG
    MISSION -->|"real mission state"| DI
    MISSION -->|"receipt: source · spans · evidence"| IOS

    classDef blocked stroke-dasharray: 5 5,stroke:#b45309,color:#b45309
    class Q,RR,FC,GCAL blocked
```

## The one thing this architecture is built around

**A write is a claim; a read-back is evidence.** Two places enforce it, and neither can be bypassed:

- **Grounding.** The model never gets to assert a deadline. Every extracted deadline carries the
  verbatim `source_span` it came from, and `_verify_deadlines` drops any span not literally present
  in the source text. The image path transcribes pixels *first* precisely so this same gate has a
  real source text to check against — a single image→JSON call would produce spans checkable only
  against the model's own claim about the image, making a hallucinated deadline unfalsifiable.
- **Execution.** A calendar event is `verified` only after an independent `events.get` returns a
  matching title and start. Missing, mismatched, or cancelled → not verified.

## Where the guarantees actually live

| Guarantee | Enforced by | Not by |
|---|---|---|
| Tenant isolation | Postgres RLS + `FORCE RLS`, restricted `bruce_app` role | application `WHERE` clauses |
| Intake idempotency | `UNIQUE(user_id, idempotency_key)` on `sources` | check-then-insert |
| Calendar execute-once | Google rejecting a duplicate caller-supplied event id (409) | local "already done" state |
| Mission concurrency | optimistic `version` column | in-process locks |
| Evidence lineage | FK chain `sources → source_spans → tasks` | log lines |

Each of these is a remote/database arbiter on purpose: a process crash, a retry, or a redelivered
webhook cannot corrupt them.

## Qwen Cloud's exact role

Qwen is the **multimodal intake brain** — it reads flyers, screenshots, forms and PDF pages that
have no text layer, and turns them into grounded structure. It is deliberately *not* trusted to
execute anything: it never calls a tool, never writes to the database, and never decides whether an
action is allowed. Extraction is data; policy and execution are Bruce's.

- `qwen3.7-plus`, **non-thinking** (`enable_thinking: false`) — a thinking-mode response must never
  be relied on to *be* the action JSON.
- `qwen3-rerank` (planned, Q3) — ranks *already-eligible* opportunities only, after deterministic
  filters. It can never override a hard constraint (grade, citizenship, deadline, cost).
- **No silent fallback.** If Qwen is unavailable the intake path returns `503 provider_unavailable`.
  Answering with a different provider while claiming a Qwen-powered workflow would be a lie.
