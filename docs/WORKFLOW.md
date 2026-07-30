# Baselane → Lofty Workflow

This is the whole operating flow, including its data and authority boundaries. Solid arrows are deterministic data/workflow flow; dashed arrows are review-only or approval-gated paths.

```mermaid
flowchart TD
    human[Human-authenticated visible browser] --> cdp[Visible browser/CDP evidence]
    baselane[(Baselane workspace)] --> cdp
    cdp --> export[Ledger, statement, transaction, and split evidence]

    policies[Versioned accounting policies\nconfig/*.json] --> guards
    export --> daily[Daily guarded sync]
    export --> weekly[Weekly review and mortgage evidence]
    export --> accruals[Monthly 28th accrual lane]
    export --> close[Monthly finance close]

    daily --> guards[Deterministic validation gates]
    weekly --> guards
    accruals --> guards
    close --> guards

    guards --> ecogl[(Canonical Baselane/ECO GL)]
    ecogl --> cashflow[Vendored Cashflow propagation\nper-property Cash Flow workbooks]
    cashflow --> loftyreview[lofty-pm reviewed financial payloads]

    guards --> reports[Local JSON/Markdown evidence reports]
    reports -. bounded untrusted evidence .-> cpai[Local CPAI shadow supervisor\nQwen2.5 strict JSON envelope]
    cpai -. advisory only; no dispatch .-> review[Human + deterministic review gate]

    review -. exact preview digest + scoped approval .-> mutation[Guarded Baselane mutation\ninternal transfer, tag, split, or manual row]
    mutation -. independent refresh + mirror verification .-> export
    review -. reviewed payload + explicit publish flag .-> lofty[(Lofty live financials / communications)]

    classDef external fill:#fff3cd,stroke:#8a6d3b,color:#333;
    classDef truth fill:#d9edf7,stroke:#31708f,color:#333;
    classDef guard fill:#dff0d8,stroke:#3c763d,color:#333;
    classDef ai fill:#f2dede,stroke:#a94442,color:#333;
    class baselane,lofty,human external;
    class ecogl,cashflow truth;
    class guards,review guard;
    class cpai ai;
```

## Authority rules

- Baselane, the canonical ECO GL, and verified source documents establish facts.
- Policies and deterministic guards decide whether evidence is complete enough for the next lane.
- The CPAI can summarize and triage reports only; it cannot create facts or trigger actions.
- Any financial or external action requires the existing preview, scoped approval, independent refresh, and evidence-record protocol in [AGENTS.md](../AGENTS.md).
