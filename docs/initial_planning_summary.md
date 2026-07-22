# Satay Runtime — Product and Architecture Planning Summary

**Working project name:** Satay  
**Previous placeholder name:** Abang AI  
**Status:** Early-stage open-source project concept  
**Primary language:** Python  
**Document purpose:** Handoff summary for continuing product planning and beginning implementation in a new chat

---

## 1. Product thesis

Satay should be:

> **A transparent, durable Python runtime for AI-enabled applications and workflows.**

The product should make ordinary Python application code durable, inspectable, resumable, and replayable without forcing developers into a heavy framework-specific programming model.

The main product strategy is:

- **Primary wedge:** Ordinary Python code becomes durable.
- **Immediate user experience:** Exceptional local debugging, execution history, replay, and failure visibility.
- **Design constraint:** Portability across model providers, databases, tools, and application architectures.

A useful positioning statement is:

> **Write ordinary async Python. Satay records every step, survives failures, and shows you exactly what happened.**

Satay should be an application runtime that understands AI, not merely an agent framework. Agents, chains, routers, and multi-agent systems should be patterns built on top of the runtime rather than mandatory core abstractions.

---

## 2. Initial target users

The first target users are:

- Python engineers at small AI startups
- Developers who have outgrown scripts
- Teams that need reliability without adopting a heavyweight orchestration platform
- Application developers building AI-enabled applications and pipelines
- Developers who want durable execution, local inspection, and clear failure behavior

The initial audience is not limited to autonomous-agent developers. Satay should also work well for document pipelines, background jobs, long-running business processes, human approvals, model calls, and conventional API workflows.

---

## 3. Competitive baseline

Satay must eventually support the capabilities that make LangChain and LangGraph useful:

- Plug-and-play model, tool, vector database, and service integrations
- Durable state and checkpointing
- Human-in-the-loop pauses and approvals
- Retries and recovery
- Historical inspection and replay
- Multi-agent and dynamic routing patterns
- Parallel execution
- Long-running workflows

However, Satay should not attempt to match LangChain's full integration breadth in the initial release. The core runtime and execution model should be validated first.

---

## 4. Product weaknesses Satay aims to address

### Over-engineered abstractions

Satay should prefer:

- Native async functions
- Decorators
- Type hints
- Dataclasses
- Pydantic models
- Ordinary conditionals and loops
- Native exception handling

It should avoid custom pipeline operators, graph construction DSLs, and large object hierarchies in the MVP.

### Obscured debugging

Satay should preserve:

- Native Python stack traces
- User-code frames
- Task inputs and outputs
- Attempt histories
- Retry reasons
- Node-by-node execution timelines
- Clear parent-child relationships
- Local debugging without requiring a hosted paid service

### Framework lock-in

Satay should accept and return ordinary values:

- Dictionaries
- Lists
- JSON-compatible data
- Dataclasses
- Pydantic models
- Native provider SDK objects inside tasks

Users should not need to rewrite their entire application around Satay-specific message, model, chain, or state classes.

### Linear-to-graph migration friction

Satay should support progressive complexity through ordinary Python:

```python
@workflow
async def example(request):
    first = await step_one(request)
    second = await step_two(first)

    if second.needs_review:
        review = await wait_for_event(...)

    results = await satay.map(
        process_item,
        second.items,
        key=lambda item: item.id,
    )

    return await finalize(results)
```

Sequential workflows, branching, loops, retries, parallel execution, event waits, and child workflows should all use the same programming model.

---

## 5. First reference application

The first complete showcase application will be a:

> **Human-reviewed vendor research and due-diligence dossier workflow**

Example flow:

```text
Request
  ↓
Validate inputs
  ↓
Create research plan
  ↓
Run document and web research in parallel
  ↓
Extract claims and supporting evidence
  ↓
Identify missing information
  ↓
Repeat research when necessary
  ↓
Draft structured dossier
  ↓
Run critic and validation tasks
  ↓
Pause for human review
  ↓
Revise or approve
  ↓
Export or publish
```

Example output model:

```python
class Dossier(BaseModel):
    company: str
    summary: str
    findings: list[Finding]
    risks: list[Risk]
    unresolved_questions: list[str]
    sources: list[Source]
```

### Why this application was chosen

It demonstrates:

- Sequential tasks
- Parallel fan-out
- Dynamic task creation
- Loops
- Conditional routing
- LLM calls
- External tools
- Checkpointing
- Long-running execution
- Human approval
- Retries
- Crash recovery
- Replay and forking

It also has lower-risk side effects than payment, account modification, or customer-support automation.

A key demonstration should intentionally terminate a worker during parallel research. After restart, Satay should preserve completed work, retry only unresolved work, show the interruption in the timeline, and continue to the approval step.

A likely second reference application is:

> **Document intake and decision pipeline**

```text
Upload → extract → classify → validate → human correction → persist
```

---

## 6. Core programming model

Satay will use explicit durable boundaries.

```python
@satay.workflow
async def create_vendor_dossier(request: DossierRequest) -> Dossier:
    plan = await create_research_plan(request)

    evidence = await satay.map(
        research_source,
        plan.sources,
        key=lambda source: source.id,
        concurrency=8,
    )

    dossier = await write_dossier(request, evidence)

    review = await satay.wait_for_event(
        ReviewDecision,
        key=f"dossier-review:{request.request_id}",
    )

    if review.approved:
        return dossier

    return await revise_dossier(
        dossier,
        review.comments or "",
    )
```

### Explicit boundaries

Satay will require:

- `@workflow` for deterministic orchestration functions
- `@task` for nondeterministic work

Network access, model calls, clocks, randomness, filesystem access, database calls, and external APIs must occur inside tasks or durable runtime primitives, not directly inside workflow functions.

This creates reliable replay boundaries.

### Ordinary Python remains ordinary

The following should remain native Python:

- `if` statements
- `for` and `while` loops
- `try` and `except`
- Function composition
- Local variables
- Typed inputs and outputs

Satay should not require developers to express these concepts through a graph DSL.

---

## 7. Workflow state model

Satay will not require a shared mutable state object.

The authoritative state model will be:

- Workflow input
- Typed task inputs
- Typed task outputs
- External events
- Timers
- Task attempts
- Final workflow output
- Append-only execution history

Example journal:

```text
WorkflowStarted
TaskScheduled: create_research_plan
TaskCompleted: create_research_plan
TaskScheduled: research_source[source-1]
TaskScheduled: research_source[source-2]
TaskCompleted: research_source[source-2]
TaskCompleted: research_source[source-1]
TaskScheduled: write_dossier
TaskCompleted: write_dossier
WorkflowWaiting: human_review
EventReceived: human_review
TaskScheduled: revise_dossier
TaskCompleted: revise_dossier
WorkflowCompleted
```

The debugger should reconstruct a useful state view from the journal rather than requiring users to maintain framework-specific global state.

---

## 8. Execution guarantees

Satay will not claim universal exactly-once execution.

### Workflow invocation

Satay should support an optional stable idempotency key:

```python
run = await satay.start(
    create_vendor_dossier,
    request,
    idempotency_key=f"dossier:{request.request_id}",
)
```

Repeated starts with the same key should return the same logical workflow run within one persistence store.

### Workflow replay

During recovery, Satay re-executes workflow orchestration logically.

Previously completed task calls return their stored results rather than physically executing again.

The user-facing guarantee is:

> A completed logical task result will normally be reused during workflow replay.

### Task attempts

Task execution will use:

> **At-least-once physical execution with once-recorded logical completion.**

A task may physically run more than once when completion is ambiguous, especially if a worker crashes after an external side effect but before Satay records success.

Once Satay has durably recorded a successful task result, replay should reuse it.

### External side effects

Satay should generate a stable idempotency key for every logical task invocation.

The key should remain stable across retries of the same logical task and differ between distinct logical invocations.

Example:

```python
@satay.task(
    retries=3,
    side_effect=True,
)
async def publish_report(report, ctx: TaskContext):
    return await external_client.publish(
        report,
        idempotency_key=ctx.idempotency_key,
    )
```

Satay cannot guarantee exactly-once behavior for arbitrary external systems. Safety depends on:

- Provider-supported idempotency keys
- Application database transactions
- Transactional outbox patterns
- Explicit compensation
- Provider-specific integrations

### Recommended guarantee statement

> Satay durably records workflow progress and completed task results. After interruption, workflows resume using recorded results rather than repeating completed logical tasks. Individual task attempts use at-least-once execution and may run more than once when completion is ambiguous. Satay provides stable idempotency keys and execution records to help developers make external effects safe.

---

## 9. Side-effect safety policy

Satay should support project-level safety modes:

```python
SatayConfig(effect_safety="off")
SatayConfig(effect_safety="warn")
SatayConfig(effect_safety="strict")
```

The agreed behavior is:

- Development default: `warn`
- Optional production mode: `strict`
- In strict mode, retryable tasks marked as side-effecting must declare an idempotency or compensation strategy

Compensation and Saga-like behavior are useful later but are not required for the core MVP.

---

## 10. Serialization model

All durable boundaries will be JSON-compatible by default.

Supported values should include:

- Primitive values
- Lists
- Dictionaries
- Dataclasses
- TypedDict values
- Pydantic models
- Enums
- Datetimes and timedeltas through tagged representations
- Explicit file and binary references

No implicit `pickle` persistence.

Reasons:

- Better security
- Better inspectability
- Better long-term compatibility
- Easier SQL querying
- Future TypeScript interoperability
- Reduced coupling to Python module paths

Custom serializers may be supported later through an explicit codec registry.

---

## 11. Runtime architecture

### Local-first MVP

The first version should run locally with:

```bash
satay dev
```

A local development process may start:

- One application worker
- SQLite persistence
- A local control API
- The local debugger

### Persistence backends

- SQLite: default local development backend
- PostgreSQL: first production backend
- Redis: not the primary durable execution store

### Worker boundary

The MVP may execute work in-process, but task execution should pass through an internal executor interface from the beginning.

```python
class TaskExecutor(Protocol):
    async def execute(
        self,
        invocation: TaskInvocation,
    ) -> TaskAttemptResult:
        ...
```

Initial implementation:

```python
LocalTaskExecutor
```

Possible later implementation:

```python
PostgresTaskExecutor
```

The goal is to preserve a clean worker boundary without building a full distributed system too early.

---

## 12. Core durable primitives

The MVP will have five main durable primitives:

1. Task
2. Durable sleep
3. External event wait
4. Parallel map/gather
5. Child workflow

Examples:

```python
result = await perform_research(request)
```

```python
await satay.sleep(timedelta(hours=4))
```

```python
decision = await satay.wait_for_event(
    ReviewDecision,
    key=f"dossier-review:{dossier_id}",
)
```

```python
evidence = await satay.map(
    research_source,
    sources,
    key=lambda source: source.id,
    concurrency=8,
)
```

```python
legal_report = await satay.start_child(
    perform_legal_review,
    vendor,
)
```

The core MVP should not introduce separate primitives for:

- Agents
- Chains
- Supervisors
- Routers
- Swarms
- Teams
- Graph nodes
- Graph builders

These should emerge as higher-level patterns or later libraries.

---

## 13. Task identity

### Sequential calls

Ordinary sequential task identity will be implicit and based on:

- Workflow run
- Workflow invocation path
- Task definition
- Logical call sequence

Developers should not need to pass task IDs into normal calls.

### Dynamic fan-out

Dynamic maps require explicit stable keys:

```python
results = await satay.map(
    research_source,
    sources,
    key=lambda source: source.url,
)
```

This allows stable replay identity even when item ordering or completion ordering changes.

Special framework arguments such as `__satay_task_id` should not pollute normal task function signatures.

---

## 14. Code versioning

Every workflow run should record a code version.

Possible identifiers:

- Git commit
- Package version
- Developer-provided deployment ID

When a workflow resumes under a different version:

- Development mode should warn clearly
- Strict mode may reject automatic resume
- Users may explicitly fork the workflow
- Automatic workflow migration is out of scope for the MVP

The MVP does not need to manage historical deployment artifacts. It only needs to detect and surface version mismatch honestly.

---

## 15. Execution history and replay

The execution history should be append-only and immutable.

Satay should use the concept of a **fork** rather than rewriting history.

Example:

```text
Fork run from before write_dossier
```

A fork may modify:

- Task implementation
- Model
- Prompt
- Input
- Retry policy
- Provider configuration

The original run remains unchanged.

---

## 16. Local debugger and observability

The local debugger is a core product surface.

For every logical task, it should show:

```text
write_dossier
Status: completed
Attempts: 2
Duration: 18.4 seconds
Model: provider/model
Tokens: ...
Estimated cost: ...
Input hash: ...
Output schema: DossierDraft
```

Expandable details should include:

- Every physical attempt
- Native exception and application stack trace
- Inputs and outputs
- Parent and child relationships
- Retry reason and delay
- State changes
- Model request metadata
- Tool calls
- Token usage
- Latency
- Estimated cost
- Idempotency status
- Code version
- Side-effect warnings

Important debugger views:

- Timeline
- Execution tree
- Logical tasks versus physical attempts
- Run comparison
- Replay and fork controls
- Code-version mismatch warnings
- Secret and sensitive-data redaction

---

## 17. Open-source and commercial posture

For the current project phase, assume everything is open source.

Potential components include:

- Runtime
- Persistence interfaces
- SQLite and PostgreSQL support
- Local debugger
- CLI
- Model adapters
- Tool integrations
- Execution protocol
- Managed execution features
- Team observability
- Evaluation tools
- Access controls
- Audit logs
- Deployment tooling

A commercial split may be considered only after the project gains traction.

---

## 18. Naming decision

The previous placeholder name, Abang AI, is no longer being used.

The two main candidates were:

- Satay
- Ondeh

The working recommendation is:

> **Satay**

Preferred product naming:

- Project: **Satay**
- Full product description: **Satay Runtime**
- Python package: `satay`
- CLI: `satay`
- Local command: `satay dev`
- Debugger: **Satay Studio**
- Integration collection: possibly **Satay Connect**

Avoid **SatayGraph** because:

- The core product is not graph-first
- The MVP intentionally avoids a graph DSL
- The name would position the project too narrowly as a LangGraph clone

The Satay metaphor works because individual pieces represent units of work while skewers represent ordered execution paths. Multiple skewers can suggest parallel workstreams.

Ondeh remains visually appealing as a node metaphor, but Satay is clearer as the overall product name.

This naming remains provisional and still requires package, domain, repository, and trademark checks before public release.

---

## 19. Current example API

```python
from dataclasses import dataclass
from datetime import timedelta

import satay


@dataclass
class DossierRequest:
    request_id: str
    company_name: str
    objective: str


@dataclass
class ReviewDecision:
    approved: bool
    comments: str | None = None


@satay.task(retries=3)
async def create_research_plan(
    request: DossierRequest,
) -> ResearchPlan:
    ...


@satay.task(retries=3)
async def research_source(
    source: ResearchSource,
) -> Evidence:
    ...


@satay.task(retries=2)
async def write_dossier(
    request: DossierRequest,
    evidence: list[Evidence],
) -> Dossier:
    ...


@satay.task(retries=2)
async def revise_dossier(
    dossier: Dossier,
    comments: str,
) -> Dossier:
    ...


@satay.workflow
async def create_vendor_dossier(
    request: DossierRequest,
) -> Dossier:
    plan = await create_research_plan(request)

    evidence = await satay.map(
        research_source,
        plan.sources,
        key=lambda source: source.id,
        concurrency=8,
    )

    dossier = await write_dossier(request, evidence)

    review = await satay.wait_for_event(
        ReviewDecision,
        key=f"dossier-review:{request.request_id}",
        timeout=timedelta(days=7),
    )

    if review.approved:
        return dossier

    return await revise_dossier(
        dossier,
        review.comments or "",
    )
```

Starting a workflow:

```python
run = await satay.start(
    create_vendor_dossier,
    request,
    idempotency_key=f"dossier:{request.request_id}",
)
```

Sending an event:

```python
await satay.send_event(
    key=f"dossier-review:{request.request_id}",
    event=ReviewDecision(
        approved=False,
        comments="Add a deeper security analysis.",
    ),
)
```

---

## 20. Current project phase

The project has completed:

- Product thesis
- Initial target user definition
- First reference workflow selection
- Core programming model
- Workflow state model
- Execution guarantee philosophy
- Side-effect safety direction
- Serialization direction
- Local-first architecture direction
- Core durable primitive selection
- Task identity approach
- Code-version mismatch policy
- Working product name

The project is now moving from:

> **Product definition**

into:

> **Detailed technical design and implementation planning**

---

## 21. Recommended next discussion topics

### A. Execution lifecycle and state machines

Define precise states for:

- Workflows
- Tasks
- Task attempts
- Timers
- Event waits
- Child workflows

Questions include:

- Cancellation semantics
- Timeout semantics
- Retry behavior
- Failure propagation
- Parent-child cancellation
- Worker crash recovery
- Lease expiration
- Heartbeats
- Paused workflow resource usage

### B. Canonical execution journal

Define event types such as:

```text
WorkflowCreated
WorkflowStarted
TaskScheduled
TaskClaimed
TaskAttemptStarted
TaskAttemptFailed
TaskCompleted
TimerCreated
TimerFired
EventWaitStarted
ExternalEventReceived
WorkflowWaiting
WorkflowResumed
WorkflowCompleted
WorkflowFailed
WorkflowCancelled
RunForked
```

Determine:

- Required event fields
- Atomic transaction boundaries
- Ordering guarantees
- Event IDs
- Idempotency
- Snapshot strategy
- Journal compaction, if any

### C. Public Python API

Finalize:

- `@workflow`
- `@task`
- `satay.start`
- Run handles
- `result()`
- `status()`
- `cancel()`
- `satay.sleep`
- `satay.wait_for_event`
- `satay.send_event`
- `satay.map`
- `satay.gather`
- `satay.start_child`
- Retry and timeout configuration
- Task context
- Testing utilities
- Dependency injection
- Progress streaming

### D. Persistence schema

Design SQLite and PostgreSQL tables for:

- Workflow runs
- Journal events
- Logical tasks
- Task attempts
- Event waits
- Timers
- Leases
- Code versions
- Serialized inputs and outputs
- Large payload references
- Idempotency records

### E. Worker model

Define:

- Work claiming
- Leases
- Heartbeats
- Retry scheduling
- Backoff
- Concurrency limits
- Process crashes
- Duplicate attempts
- Graceful shutdown
- Local in-process execution
- Future distributed workers

### F. Replay engine

Specify how deterministic replay works:

- How task calls match journal entries
- How mismatches are detected
- How loops and branches replay
- How map items are matched
- How child workflows replay
- What happens after code changes
- How non-determinism errors appear

### G. Local debugger

Design the initial UI and local API:

- Timeline
- Tree
- Task detail view
- Attempt detail view
- Stack traces
- Inputs and outputs
- Event waits
- Timers
- Run forking
- Run comparison
- Redaction
- Model and tool instrumentation

### H. Implementation plan

Produce:

- Repository layout
- Python package structure
- Core domain models
- Persistence abstractions
- First vertical slice
- Test strategy
- Failure-injection tests
- Milestones
- MVP definition
- Release criteria

---

## 22. Recommended first implementation slice

A practical first end-to-end milestone is:

> Run a workflow containing two tasks, persist every transition to SQLite, kill the process after the first task completes, restart it, reuse the first task result, execute the second task, and show the complete execution timeline locally.

This proves the most important runtime property before adding model integrations, distributed workers, or a sophisticated debugger.

Suggested first workflow:

```python
@satay.task
async def step_one(value: int) -> int:
    return value + 1


@satay.task
async def step_two(value: int) -> int:
    return value * 2


@satay.workflow
async def demo(value: int) -> int:
    intermediate = await step_one(value)
    return await step_two(intermediate)
```

Minimum success criteria:

- Durable workflow creation
- Stable run ID
- Journal persistence
- Task scheduling
- Task completion persistence
- Process interruption
- Workflow replay
- Reuse of completed task result
- Completion of remaining task
- Queryable timeline
- Native error visibility

---

## 23. Suggested prompt for the next chat

Copy the following into a new chat together with this document:

> I am continuing the design and implementation of Satay Runtime, a transparent, durable Python runtime for AI-enabled applications and workflows. The attached planning document contains all decisions made so far. Treat those decisions as accepted unless a serious contradiction is discovered. Act as an expert distributed-systems architect, Python library designer, and technical lead. Start by reviewing the summary, then help me define the execution lifecycle and canonical event journal. After that, help me design the first end-to-end implementation slice and repository structure. Prefer concrete state machines, schemas, APIs, invariants, and tests over broad product discussion.

---

## 24. Non-goals for the first MVP

The first MVP should not attempt to include:

- A full LangChain-scale integration ecosystem
- A graph-building DSL
- A general-purpose agent abstraction
- Distributed multi-region execution
- Universal exactly-once side effects
- Automatic migration of long-running workflows
- Full compensation and Saga orchestration
- Hosted commercial infrastructure
- Enterprise access controls
- Large-scale evaluation infrastructure
- TypeScript parity
- Automatic instrumentation of arbitrary Python calls
- Implicit persistence through pickle

The goal is to prove a small, rigorous durable runtime with excellent transparency.

---

## 25. Core principles to preserve

1. Ordinary Python first.
2. Explicit durable boundaries.
3. Honest execution guarantees.
4. Append-only history.
5. Native errors and stack traces.
6. Local-first development.
7. JSON-compatible durable data.
8. No mandatory shared state object.
9. Minimal durable primitives.
10. Portability over framework lock-in.
11. AI-aware, but not agent-only.
12. Build the runtime before building the ecosystem.
