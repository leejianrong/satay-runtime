# ADR-0032 — Products on top of Satay: a pipeline-graph builder as a separate product, and the compile-down contract

- **Status:** Accepted
- **Date:** 2026-08-27
- **Deciders:** Jian (leejianrong2@gmail.com)

Reaffirms the graph-DSL non-goal in [REQS](../REQS.md) R0.2 and
[ADR-0025](0025-positioning-agents-first.md) decision 4, and records — for the first time —
what is *permitted* outside the runtime. Extends
[ADR-0026](0026-license-and-hosted-journal-plane.md) decision 5 (product-agnostic
tenancy, sibei-flow as second tenant) by naming a third product on the same engine.
Adds the `D-products` entry to [CONTEXT.md](../CONTEXT.md)'s decision register.

## Context

A visual pipeline builder — draw a graph, deploy it, run it managed, in the shape of
Kubeflow Pipelines or Airflow — has been proposed repeatedly and rejected each time as
out of scope for Satay. The rejection is correct and the record is missing, which is the
worst combination: a recurring decision with no citation gets re-litigated from scratch
every time, and each round reads as an arbitrary veto rather than as a decision already
made.

The rejections were also answering a narrower question than the one being asked. Two
different proposals were collapsed into one:

1. **A graph DSL inside the runtime.** Decided long ago, in the negative, and load-bearing
   for everything else. "No graph DSL" is not a preference, it is the problem statement:
   [FRAME](../FRAME.md) and [PRD](../PRD.md) both define the alternative to Satay as a
   framework that "forces ordinary code into graph DSLs", R0.2 makes native
   `if`/`for`/`while`/`try` a must-have, and D25.4 restates the non-goal. A graph DSL in
   `satay` refutes the pitch in the reader's first paragraph.
2. **A separate product that compiles a drawn graph down to Satay code.** Never actually
   decided. This is the same relationship sibei-flow already has — an independent product
   with its own buyer, sharing one execution engine — and ADR-0026 decision 5 went out of
   its way to keep the hosted plane free of Satay-specific assumptions *so that* a second
   product could sit on it.

Four facts constrain what (2) can be, and all four are properties of the code as it
stands rather than matters of taste:

- **Satay cannot host execution today**, and the pitch "deploy them" means exactly that.
  One process, one journal writer, SQLite (ADR-0012, ADR-0017). ARCHITECTURE §9 phases 2
  and 3 (PostgreSQL, then multi-worker leasing) are the prerequisites, and D25 deferred
  both until after the agents-first launch. A managed deploy plane on today's engine is
  one SQLite box per tenant.
- **Hosted execution is tier 3** in ADR-0026's own table, deliberately out of scope: it is
  the first tier that holds customer model keys, warehouse credentials and code. Tier 1
  was chosen precisely because it holds none of them.
- **Loops are not DAGs.** Agentic pipelines with loops are explicitly wanted, and a
  directed *acyclic* graph cannot express them. Copying Airflow's or Kubeflow's graph model
  and then bolting a loop node onto it is how visual builders become unreadable.
- **Bulk data through task arguments fights the journal.** Payloads are JSON-compatible
  (ADR-0005) and spill to blob files past 256 KiB (ADR-0004). Streaming warehouse rows
  through durable-call arguments would spill a blob per row and journal the customer's
  data as a side effect.

## Decision

**1. The non-goal in the core is reaffirmed, and now has a citation.** `satay` ships no
graph DSL, no pipeline DSL, no node/operator registry, no visual-authoring affordance, and
no graph interpreter. R0.2 and D25.4 stand unchanged. "Out of scope for Satay" means *the
runtime*, and from here it means only that.

**2. A pipeline-graph builder is sanctioned as a separate product, in a separate
repository**, on the sibei-flow relationship: independent product, independent roadmap,
independent buyer, one shared execution engine. Its name, licence and packaging are its
own decisions and are not settled here.

**3. The coupling is one-way: the builder compiles down to Satay, and Satay never learns
what a graph is.** The builder's output is ordinary, readable `@workflow` / `@task` Python
that the user owns and can eject with. No import from the builder into `satay`; `satay`
must stay buildable, testable and releasable with the builder deleted. The design test,
mirroring ADR-0026's *"could a non-Satay producer send us a journal?"*:

> **Could the emitted module have been hand-written by a developer who has never heard of
> the builder?**

If the answer is no, the compiler is wrong. This is what keeps the builder from being the
lock-in framework Satay exists to argue against, and it makes ejection a real feature
rather than a marketing line.

**4. What the canvas edits is a control-flow graph, not a DAG.** Sequence, branch, loop
and fan-out, chosen because they map one-to-one onto the Python control flow the compiler
emits (`if`, `while`, `map`/`gather`, `start_child`). Copy Kubeflow's and Airflow's
*deployment and operations* affordances — scheduling, run history, retries, parameterised
runs — not their dependency-graph model.

**5. Workload fit is bounded, and the bound is stated up front rather than discovered by a
customer.** Three classes were proposed:

| class | fit | shape |
|---|---|---|
| Agentic pipelines, including loops | **Native.** This is what D25 aimed the runtime at. | Satay runs the work. |
| ML training / batch inference | **Fit as orchestration only.** Durability and idempotency keys are exactly right for submit-and-poll. | Satay submits to external compute and waits; it does not run the job. |
| ETL / ELT | **Push-down only.** | Satay orchestrates warehouse or engine jobs; rows never travel through durable-call arguments. |

The builder must not offer a node type that streams bulk rows through task arguments, per
the journal constraint above. Launch on the agentic class; the ML and ETL node families
are later, separately justified additions.

**6. Sequencing: the builder does not start in earnest before Satay's own launch.** In
order — (a) the remaining D25 launch-blocker cards; (b) the tier-1 hosted plane of D26;
(c) ARCHITECTURE §9 phase 2 (PostgreSQL) and phase 3 (multi-worker), which are what make
"deploy" mean anything at all. A **prototype spike outside this repository is explicitly
permitted and encouraged before then**, so that the builder's own ADRs get written from
evidence instead of ambition. The spike does not license changes to `satay` in support of
it; those go through cards like any other.

**7. Hosted execution stays a separate, later decision.** "Deploy the pipeline you drew"
is ADR-0026 tier 3 and needs its own ADR plus a compliance surface Satay currently has
none of. Note that the builder does **not** have to wait for it: a local or self-hosted
authoring tool that emits code into the user's own repository has no credential surface at
all, and that version is both the cheapest to build and the fastest way to learn whether
anyone wants to draw these graphs.

## Consequences

- **The recurring argument ends with a citation.** Requests for graph authoring inside
  `satay` are answered with decision 1; the idea itself is answered with decisions 2 and 6.
- **The builder becomes a legitimate input channel**, like sibei-flow, and inherits the
  same guardrail: its needs must not pull the runtime's roadmap away from app developers,
  and where they conflict, **D25 wins**.
- **Expect gravitational pull toward graph-shaped features in the core** — a node registry,
  a step-metadata slot on events, a graph id on runs, a "step name" alongside the call-site
  ordinal. Naming it now so it is recognisable later: those are builder-side concerns and
  the default answer is no. A durable-call identity scheme that grows a second, graph-shaped
  variant (ADR-0002) is the specific failure to watch for.
- **Emitted-code quality *is* the product.** If the generated Python is unreadable, the
  ejection promise in decision 3 is false and the builder loses its only structural
  advantage over Kubeflow and Airflow.
- **Three workload classes is three products' worth of surface**, which is why decision 5
  picks one to launch with. Shipping all three shallowly would compete with mature tools on
  their strongest ground and with nothing differentiated.
- **ADR-0026 decision 5 stops being speculative.** The plane now has a third tenant
  candidate, so product-agnostic tenancy is load-bearing, and the "could a non-Satay
  producer send us a journal?" test applies to the builder's runs too.
- Nothing here changes the core-dependency boundary (ADR-0013/0016), the licence (D-license)
  or any execution semantics. No code in `src/` changes as a result of this ADR.

## Alternatives considered

- **A graph DSL in the core** — rejected; see decision 1. It contradicts R0.2 and the
  problem statement both documents lead with.
- **A graph *interpreter* in the core** (the runtime walks a graph structure at run time)
  rather than a compiler — rejected, and it is the tempting one. It puts the graph model
  into the execution path, so nondeterminism detection (ADR-0003) and durable-call identity
  (ADR-0002) would each need a second, graph-shaped scheme alongside the call-site ordinal.
  Compiling to source keeps exactly one execution model, and the debugger keeps showing
  Python.
- **Fork Satay into the builder's repository** — rejected: two engines, two sets of replay
  semantics, and the fork/replay/compare wedge maintained twice.
- **Build the builder first and launch on it** — rejected for the same reason D25 rejected
  platform-first: it designs for a buyer nobody has spoken to, and it defers the runtime's
  own first user by quarters.
- **Wait for tier 3 before starting anything** — rejected: decision 7's local, code-emitting
  version carries no credential surface and answers the demand question years earlier and
  for a fraction of the cost.
- **Keep rejecting the idea informally with no ADR** — rejected: it is what produced this
  ADR. An unrecorded recurring rejection is indistinguishable from an arbitrary one.
