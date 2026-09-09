# Hand-off: more accurate and more granular time profiling of hosted Coworld episodes

You are a coding agent working in the `Metta-AI/metta` monorepo (`~/coding/metta`; never edit that checkout directly, use a separate clone or worktree, and `git -C ~/coding/metta pull` before reading it). Your task is to evaluate a set of proposed changes to how the platform traces the lifecycle of a hosted Coworld episode, propose any others you find, and deliver a final ranked set of changes with a cost analysis and a validation plan. Do not implement yet; the deliverable is the plan. Read this whole document before opening any file.

## What we want

Every hosted episode currently produces one Datadog trace with a `job.lifecycle` root, three stage spans, four runtime phase spans reconstructed from the worker's `worker_timings.json`, and dispatch spans for the game pod built from kubelet Events. A study on 2026-09-09 with a purpose-built measuring Coworld, joined to the Datadog spans of the same 96 jobs, found that these spans mis-describe where time goes. We want the trace to tell the truth about every second of a job, at the granularity of the real steps (init container run, image pulls, worker startup, player pod startup, gameplay, finalize, teardown), with timestamps rather than reconstructed offsets.

**Hard constraint: do not slow the job down.** Any change must add no measurable latency to the episode lifecycle. Concretely: no new network calls on the dispatch or gameplay path, no new polling loops, no synchronous uploads before the game can start, and no per-turn work inside game or player containers. Budget the total added cost per job at well under one percent of a 59 s baseline job, and state the cost of every change you propose. A change that buys visibility at the price of latency is a rejected change.

**Second constraint: the game-facing contract is frozen.** Spec 0080 §3 and §7 record that the public contract games and players see (env vars, routes, artifacts) does not change for telemetry, and the cluster stays agentless: no Datadog agent, no OTLP, no credentials in episode pods. Platform-owned containers (the init container, the worker, the player pod's init container, the sidecar) and the backend are fair game.

## What the study found (evidence you should verify, not take on faith)

The report is at `~/coding/coworlds/coworld-profiler/docs/reports/hosted-timing-2026-09-09.md` (markdown) with the tools and raw data in that repo. Sections 4, 6, 7, and 8 are the relevant ones. In brief, for the baseline variant (2 slots, 22 s of measured gameplay), the median job is 59 s:

| Stage or phase | Median | What it actually contains |
|---|---|---|
| `pending` | 1.7 s | queue to claim |
| `dispatched` | 13.7 s | dispatcher API call to pod created (1.9 s); pod scheduled immediately; init container image pull (cached, sub-second); `coworld-init-config` run (about 3 s); sidecar, game, worker pulls (cached); worker Python startup to its first stamp (about 4 s) |
| `game_boot_s` | 0.1 s | the worker's own Kubernetes client setup and one `/healthz` request. The game had been listening for 0.4 s already; the game's real boot (0.55 s) is inside `dispatched` |
| `player_launch_s` | 0.15 s | pod-create API calls |
| `first_step_s` (no span) | 7.2 s | player pod scheduling, image pull, init container poll; the player process itself is 0.3 s and its DNS, TCP, and handshake 7 ms. The worker's number is 1.1 s longer than the game's own measure in 68 of 76 episodes, and 10 s longer in the 8 slowest, where 10 s of gameplay was booked as startup |
| `gameplay_s` | 23.3 s | 22.0 s of measured loop plus probe, flush, and artifact-poll slop |
| `artifact_upload_s` | 0.5 s | the worker's S3 uploads |
| running tail | 7.8 s | log collection, child pod deletion, termination observation; no span |

Specific defects in the current instrumentation, with sources:

1. `worker_timings.json` carries five durations and no timestamps (`packages/coworld/src/coworld/runner/phase_timings.py:18-31`). The event processor places the spans by cumulative summation from the `running` stage start (`app_backend/src/metta/app_backend/job_lifecycle_trace.py:284-345`), so any unmeasured interval shifts every later span, and one job's `episode.finalize` ended after its `running` stage did.
2. `game_boot_s` starts at the worker's process start (`packages/coworld/src/coworld/runner/kubernetes_runner.py:687`), which is after the game container has already booted and started listening. It measures the worker, not the game.
3. `first_step_s` ends when the worker's `/global` viewer socket delivers a message after `_ensure_player_pods_started` returns (`kubernetes_runner.py:838-858`). That read landed about 10 s late in 8 of 76 episodes; in two of them `gameplay_s` came out as 0.
4. The `image.pull` spans depend on kubelet `Pulling`/`Pulled` Events captured by the pod watcher's second watch stream (`app_backend/src/metta/app_backend/job_runner/watcher.py:261-430`, `k8s_event_store.py:53-83`). Its position is memory-only, so every watcher restart re-lists the namespace's Events: 169,125 of them on 2026-09-09, seven and a half minutes at 500 per page, during which nothing is watched, after which the resume point is stale and the loop re-lists again. The watcher deployment was synced by ArgoCD 26 times that day and restarted at least three times in the hour studied. Rows that land after a job's terminal transition never become spans, because the processor emits once (`event_processor.py:620-650`).
5. Every container in the game pod except the game uses `imagePullPolicy: Always` (`app_backend/src/metta/app_backend/job_runner/dispatcher.py:1018-1095`). kubelet re-validates a cached image in under a second and reports "Successfully pulled", so the tracer tags `cache_hit=false`, and because Event timestamps have one-second resolution the span has zero length. 78 of 84 captured pull spans were zero.
6. Player pods are outside the watcher's `app=coworld-runner` selector, so they have no dispatch spans at all (spec 0080, 2026-08-25 revision note), yet they are the 4 to 24 s per slot that dominates `first_step_s`.
7. The worker polls health and artifacts once a second (`kubernetes_runner.py:95-96`), so every phase boundary carries up to a second of slop.
8. Nothing inside `coworld-init-config` is timed except the game-hosted player-file stage (`kubernetes_runner.py:355-405`), and nothing it learns reaches the worker.

## Candidate changes to evaluate

Evaluate each for accuracy gained, latency cost, implementation cost, and risk. Reject freely; propose better alternatives.

**A. Timestamps in `worker_timings.json`.** Extend `EpisodePhaseTimings` with a wall-clock anchor (`time.time_ns()` and `time.monotonic_ns()` taken together at worker start) and a monotonic offset for every boundary, keeping the existing duration fields. The emitter places spans by absolute time. Cost: a few integers in a file that is already uploaded incrementally.

**B. Record the worker's own process birth and import time.** Stamp `monotonic_ns` before the heavy imports in `kubernetes_runner.py` (the module imports the Kubernetes client, httpx, urllib3, pydantic; on the study's pods this and the health-server start took about 4 s before the first stamp) and read `/proc/self/stat` for process birth. Emit as a `worker.bootstrap` span. Cost: nil.

**C. Instrument `coworld-init-config`.** It runs the same module with the same imports, then fetches the job spec over a presigned URL, generates tokens, writes the config into the shared `emptyDir`, and for game-hosted worlds stages player files. Stamp: process birth, imports done, job-spec fetch (duration, bytes, scheme), player-file stage (already timed), config write, exit. Write them to `<workdir>/init_timings.json`; the worker reads that file at start and folds it into `worker_timings.json`, so no new upload path exists. The event processor emits `init.config` with children `init.imports`, `init.spec_fetch`, `init.player_files`. Cost: one small file write on a volume that is written anyway; zero on the gameplay path. Consider whether the import cost can be cut rather than merely measured (a lighter entry point for init-config that does not import the Kubernetes client).

**D. Re-anchor `game_boot_s`.** With A and C in place, define the game's boot as pod-created (from the pod object) to the first successful `/healthz` (worker stamp), and separately name the interval from the worker's process start to that first success as `worker.bootstrap`. Alternatively, have the worker read the game container's `state.running.startedAt` from the pod object it already lists.

**E. End `first_step_s` on pod state, not a socket read.** The worker already polls player pod status in `_ensure_player_pods_started`; record the moment the last player pod reached `Running` as the end of player startup, and keep the viewer-message receipt as a separate stamp. Consider a watch instead of a poll if it removes the 1 s slop without adding load; measure before deciding.

**F. Take container timing from pod status, not kubelet Events.** The terminal pod object the watcher stores carries `initContainerStatuses[].state.terminated.startedAt/finishedAt` and `containerStatuses[].state.running.startedAt`. That yields the init container's run and every container's start with no event capture, and it is available at exactly the moment the processor emits. Evaluate whether `image.pull` spans should be dropped in favour of this, or kept with the fixes in G.

**G. If the Event stream stays, make it cheap and durable.** Filter server-side with `field_selector="involvedObject.kind=Pod,reason=Pulled"` (one stream per reason, since selectors only support equality) so a re-list is hundreds of events rather than 169k; persist the watch position the way the pod watch does (`k8s_event_store.py:87-110`); parse the pull duration from the `Pulled` message's "in X (Y including waiting)" clause and use it as the span length; set `cache_hit` from that duration or from the "already present" wording rather than from "Successfully pulled" alone; and re-emit or backfill spans for rows that arrive after the job's terminal transition.

**H. Give player pods dispatch spans.** Extend the pod watch selector to the player pods (they carry `job-id` and `coworld-component=player` labels, `kubernetes_runner.py:1040-1160`), store their terminal objects, and emit per-slot `pod.create`, `node.allocate`, `container.start` under the job trace, joined by the `job-id` label. Watch the volume: at 16 slots this multiplies stored pod objects by 17. Consider storing only the status fields needed.

**I. Instrument the player pod's init container.** `wait-for-game-service` (`kubernetes_runner.py:135-150`) is platform-owned. It can record its own start, the first successful Service poll, and the count of failed polls, and POST that once to the worker's artifact upload server that already runs in the game pod (`PLAYER_ARTIFACT_PORT`), which the worker folds into `worker_timings.json`. That gives per-slot: container start, Service readiness, and the moment the player container was allowed to start, without touching the player image. Cost: one small HTTP POST per player pod to an in-cluster address, before the player starts.

**J. Name the running tail.** After `episode.finalize`, the worker collects logs, deletes child resources, and exits; the processor then observes termination. Stamp those steps in `worker_timings.json` and emit `episode.teardown` children (`logs.collect`, `children.delete`); measure the residual to the `running` stage end and report it as `job.termination_lag`.

**K. Stop the watcher churn.** Twenty-six ArgoCD syncs of `orchestrator-watcher` in one day, plus at least one restart that was not a sync (17:59:18 UTC on 2026-09-09). Find why the Deployment is re-applied that often and whether each apply restarts the pod; a watcher that restarts every twenty minutes cannot keep a position. This is an operational fix, not a code one, but it gates everything in G.

**L. Fix the derived per-step panel.** `devops/datadog/dashboards.py:1691-1707` divides the running-stage duration by episode length. With A in place, divide `gameplay_s` by the game's own step count where a game reports one, or drop the panel.

## What to deliver

1. For each candidate A to L: keep, modify, or reject, with the accuracy gained, the latency cost (measured or bounded, in milliseconds per job), the files touched, and the risk.
2. Any additional changes you find. Look especially at the dispatcher's own path from `dispatched_at` to pod creation (1.9 s median), at whether the coordinator image's import cost can be reduced for both the init container and the worker (it is paid twice per job), and at whether the sidecar's `Always` pull policy is deliberate.
3. A final ranked set of changes, grouped into independent PRs in a sensible order, with the total added latency per job stated and defended.
4. A validation plan. The profiler Coworld (`cow_f4c8de04-7d32-4bc8-a316-8cd8cda6a729`, repo `~/coding/coworlds/coworld-profiler`) plus `tools/fetch_spans.py` and `tools/reconcile_spans.py` reproduce the inside-versus-outside join for any set of episodes in about fifteen minutes. Use it as the before-and-after benchmark: the baseline variant's 59 s median job must not grow, and the reconciliation differences in report section 6 (1.1 s on player startup, 1.4 s on gameplay, the 10 s late boundary) should shrink toward zero.

## Things not to do

- Do not add Datadog, OpenTelemetry, or any credential to episode pods.
- Do not change the game or player contract: no new env vars, routes, or artifacts that a game or player must honour.
- Do not add per-turn instrumentation to the runtime path; per-turn numbers come from the profiler Coworld, not the platform.
- Do not fork the frozen span vocabulary from spec 0080 (`player.connect`, `player.turn`, `game.step`, `player.turn.duration`, `game.step.duration`); new spans get new names.
- Do not implement; plan. When the plan is approved, each PR goes through the normal Graphite stack.

## Where to start reading

- `docs/specs/0080-coworld-round-tracing-schema.md` §1, §3, §6, §7, and the 2026-08-25 note.
- `docs/specs/0062-job-lifecycle-traces.md` for the deterministic-id, retroactive-emission mechanism every span here uses.
- `app_backend/src/metta/app_backend/job_lifecycle_trace.py` (span construction), `job_runner/event_processor.py` (when spans are emitted), `job_runner/watcher.py` (both watch loops), `job_runner/k8s_event_store.py`, `job_runner/dispatcher.py:990-1300` (the game pod spec).
- `packages/coworld/src/coworld/runner/kubernetes_runner.py:355-420` (init-config), `:680-900` (the worker's phase stamps), `:1130-1160` (the player pod's init container).
- `~/coding/coworlds/coworld-profiler/docs/reports/hosted-timing-2026-09-09.md` sections 4 to 8, and `docs/measurement-reference.md` for what the profiler's fields mean.
