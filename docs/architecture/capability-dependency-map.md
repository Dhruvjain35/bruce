# Capability Dependency Map (v1)

How the shared platform primitives depend on each other, so we build leverage nodes first and avoid
fake breadth. Solid = built/deployed (Bite 1); dashed = planned. IDs map to `product/capabilities.yaml`.

```mermaid
graph TD
  %% built (Bite 1) — solid
  SRC[1 Universal source intake<br/>relay + attachment transport]:::done
  RUNTIME[29 Conversation + reasoning router]:::done
  MODEL[Provider-neutral reasoner<br/>llm.py + pydantic_ai]:::done
  VOICE[26 ConversationStyleEngine<br/>+ fact-preservation guard]:::done
  MISSION[5 Durable mission engine<br/>+ at-most-once outbound]:::done
  EVID[10 Evidence / provenance]:::done
  RLS[25 Permission / privacy / RLS]:::done
  LINK[Account linking]:::done

  SRC --> RUNTIME
  LINK --> RUNTIME
  MODEL --> RUNTIME
  VOICE --> RUNTIME
  RUNTIME --> MISSION
  EVID --> RUNTIME
  RLS --- RUNTIME
  RLS --- MISSION

  %% P1 school core — built (offline, fake Canvas adapter) — solid
  SCH[2 SchoolConnector framework<br/>protocol + capability matrix]:::done
  ACG[3 Canonical academic graph<br/>+ sync cursors, RLS]:::done
  CHG[4 Change-detection engine<br/>created/updated/deleted]:::done
  QUERIES["What's due / changed / missing / next"]:::done

  %% planned — dashed
  CANVAS[Canvas adapter<br/>FAKE only — real OAuth blocked]:::plan
  DECIDE[7 Decision + approval engine]:::plan
  VERIFY[14 External verification]:::plan
  COMMS[12 Communication adapters<br/>Gmail/Outlook]:::plan
  CAL[34 Calendar execution]:::plan
  NOTIFY[20 Notification policy]:::plan
  BROWSER[11 Browser-action adapter]:::plan
  PAY[30 Payment authorization envelope]:::plan

  SCH --> ACG
  ACG --> CANVAS
  CANVAS --> CHG
  ACG --> QUERIES
  CHG --> QUERIES
  RUNTIME -.-> QUERIES
  EVID --> ACG
  RUNTIME -.-> DECIDE
  DECIDE --> VERIFY
  DECIDE --> COMMS
  DECIDE --> CAL
  DECIDE --> BROWSER
  DECIDE --> PAY
  MISSION --> NOTIFY
  RUNTIME -.-> NOTIFY
  EVID --> VERIFY

  classDef done fill:#1f6f3f,stroke:#0c3,color:#fff;
  classDef plan fill:#2a2f3a,stroke:#667,color:#ccd,stroke-dasharray:5 4;
```

## Reading it
- **Everything routes through the conversation runtime (29)** — it's the single entry point the
  built primitives feed and the planned ones will hang off of.
- **SchoolConnector (2) → academic graph (3) → change detection (4) → the north-star queries are now
  BUILT** (provider-neutral, RLS-isolated, migration 0012), validated end-to-end against a FAKE Canvas
  adapter. The real Canvas leaf stays dashed: it needs founder OAuth credentials + an institution to read
  against. We built the primitive, not the leaf — a real adapter drops in behind the same Protocol.
- **The decision + approval engine (7) + external verification (14)** are the safety spine every
  consequential action (email send, calendar write, browser action, payment) must pass through. They
  are prerequisites for anything with `risk: high/critical`.
- **Notification policy (20)** gates any promise of a follow-up; Bite 1 deliberately makes no such
  promise because this node isn't built.
- Payment (30/31/36), browser (11), and travel/commerce (32/33) are intentionally the deepest / last
  — high risk, low leverage until the academic core exists.
