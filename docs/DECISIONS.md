# RF Sentinel — Architecture Decisions

## ADR-001: Use logical RF data, control, and analytics/presentation planes

- **Status:** ACCEPTED
- **Context:** The requirements span hardware acquisition, unattended orchestration, retained analysis, and Telegram interaction. Treating all responsibilities as one undifferentiated subsystem hides ownership and failure boundaries.
- **Decision:** Define RF data, control, and analytics/presentation planes as logical responsibility boundaries. They are not automatically separate processes.
- **Alternatives:** One unstructured application; process-per-plane; many microservices.
- **Consequences:** Interfaces and data ownership are explicit. The initial runtime remains simple while future process separation is possible.
- **Evidence required:** No hardware experiment is needed for the logical separation; later process separation needs operational evidence.

## ADR-002: Normalize spectrum measurements at an Acquisition Adapter boundary

- **Status:** ACCEPTED
- **Context:** `rtl_power`, `hackrf_sweep`, future SoapySDR, and native APIs expose different data and lifecycles. Detection, event history, analytics, and Telegram must not depend on a specific tool format.
- **Decision:** Require adapters to emit a validated, provenance-rich normalized SpectrumMeasurement contract and explicit observation/sweep coverage outcomes.
- **Alternatives:** Couple all downstream logic to tool output; adopt one SDR API as a permanent requirement; reimplement all acquisition/DSP immediately.
- **Consequences:** An adapter/normalizer must supervise and validate tool output. New adapters can be introduced without changing downstream domain behavior.
- **Evidence required:** Parser/contract fixtures and CM4 acquisition experiments validate each implementation, not the boundary itself.

## ADR-003: Establish command-line acquisition as an experimental baseline

- **Status:** IMPLEMENTED FOR CURRENT BASELINE; broader hardware validation remains evidence-gated
- **Context:** Research identifies `rtl_power` and `hackrf_sweep` as mature candidate sweep tools, but their CM4 performance, output behavior, recovery, and profile sufficiency are not yet demonstrated.
- **Decision:** The current baseline implements supervised `rtl_power` acquisition. `hackrf_sweep` remains a future adapter candidate; broader device/profile validation remains evidence-gated.
- **Alternatives:** Direct device libraries first; SoapySDR first; custom DSP pipeline first.
- **Consequences:** Tool processes require strict timeout, reaping, version/output validation, reservation, and recovery behavior. A validated shortfall may justify an API adapter.
- **Evidence required:** Sustainable workload, FFT/measurement throughput, scan revisit, USB stability, RTL-SDR/HackRF endurance, and recovery experiments in [EXPERIMENTS.md](research/EXPERIMENTS.md).

## ADR-004: Use RFEvent as the central noteworthy-activity entity

- **Status:** ACCEPTED
- **Context:** Operators need a coherent history of notable activity with detector evidence, provenance, optional investigation, capture, classification, and annotation. Measurements remain high-volume source facts.
- **Decision:** Make RFEvent the central domain entity for noteworthy activity, linked to immutable measurement/sweep/observation evidence. Do not make RFEvent the sole data model or a replacement for measurements.
- **Alternatives:** Event-only storage; measurement-only queries with no event entity; a classification-centered model.
- **Consequences:** Event history is explainable and extensible. Events require evidence links and version references; unknown/unclassified is first-class.
- **Evidence required:** Controlled detector tests establish event criteria; no final database schema is implied.

## ADR-005: Separate survey from optional investigation

- **Status:** ACCEPTED
- **Context:** Continuous broad survey and focused event follow-up have different scheduling, resolution, device, storage, and impact-on-coverage concerns.
- **Decision:** Model survey as normal profile-driven observation and investigation as an optional RFEvent-linked workflow. Investigation may reserve a different SDR and appends evidence to the original event.
- **Alternatives:** Run all work as one scan type; require a dedicated investigation SDR for MVP; discard survey/event link after follow-up.
- **Consequences:** MVP can exclude focused work. Future IQ capture/classification does not redesign events. Survey preemption requires explicit policy.
- **Evidence required:** Multi-SDR, capture-impact, and profile experiments before enabling concurrent or preemptive behavior.

## ADR-006: Start with one station service and supervised acquisition child processes

- **Status:** ACCEPTED
- **Context:** A CM4 station benefits from simple deployment and local data consistency. External sweep tools already introduce a useful process-failure boundary. Many services introduce distributed ownership complexity.
- **Decision:** Use one systemd-supervised RF Sentinel service with internal logical components and supervised external acquisition processes. The canonical entrypoint is `python -m rf_sentinel station`; it owns a process-wide exclusive lock at `DATA_DIR/station.lock` so only one station process is active. Preserve interfaces that permit a later small-service split.
- **Alternatives:** Separate acquisition and analytics services now; microservices; a monolith with no child-process supervision.
- **Consequences:** A station-process crash briefly affects all internal functions but systemd restarts it. Tool crashes can be contained and recovered separately. Internal component fault containment must be designed deliberately. Standalone `acquire` and `report-schedule` remain diagnostic modes, not the production topology.
- **Evidence required:** Reboot/automatic-recovery, long-running, resource, and fault-injection experiments demonstrate whether the model is adequate.

## ADR-007: Treat source, domain, derived, and operational data separately

- **Status:** ACCEPTED
- **Context:** Images, reports, and classification are not authoritative substitutes for measurements, configuration, events, and coverage. Unbounded storage is unacceptable.
- **Decision:** Assign authoritative owners and retention behavior separately for source measurements/IQ, domain records, derived artifacts, and logs. Derived artifacts record input provenance and are normally reproducible while inputs remain retained.
- **Alternatives:** Store only images/reports; one undifferentiated data store; retain all data indefinitely.
- **Consequences:** Persistence implementation must support different volume/access/retention needs. Storage pressure degrades optional and derived work before source integrity.
- **Evidence required:** Disk throughput/capacity/endurance and historical-processing experiments before final data-store technology and retention thresholds.

## ADR-008: Keep SoapySDR as evidence-gated future adapter option

- **Status:** EXPERIMENT REQUIRED
- **Context:** SoapySDR could unify future hardware access, but module behavior, package ABI, capability mapping, hot-plug, and CM4 stability may differ by device.
- **Decision:** Do not require SoapySDR for the first baseline. Permit a future SoapySDRAdapter only after its target device/module compatibility matrix is validated.
- **Alternatives:** Require SoapySDR now; reject it permanently; allow direct device APIs only.
- **Consequences:** The normalized contract protects downstream components. A hybrid per-device adapter strategy remains valid.
- **Evidence required:** Device discovery, supported profile settings, sample/stream stability, disconnect/reconnect, USB, CPU/RAM, and packaging tests on target CM4.

## ADR-009: Use SQLite for the local prototype persistence boundary

- **Status:** ACCEPTED FOR LOCAL PROTOTYPE; production retention/deployment validation pending
- **Context:** The continuous acquisition and report consumers need a durable local hand-off. The implementation persists canonical sweeps, measurement payloads, metadata, and bounded incidents in one SQLite database.
- **Decision:** Use `runtime/sweeps.sqlite3` as the persistent local store for acquisition and report queries. Keep report artifacts as separate files and retain storage/production-capacity thresholds as deployment concerns.
- **Alternatives:** Select one embedded database now; CSV-only; time-series database now.
- **Consequences:** Acquisition and reporting share a durable local source without coupling their lifecycles. SQLite does not become a claim that all future domain, IQ, or production-retention needs are solved.
- **Evidence required:** Raspberry Pi endurance, capacity, retention, and recovery validation before production deployment.

## ADR-010: Keep IQ capture and classification optional

- **Status:** DEFERRED
- **Context:** Both add storage, privacy/governance, RF coverage, and validation risk. Research does not demonstrate useful CM4 classification or a locally valid dataset.
- **Decision:** Preserve RFEvent evidence extension points; do not enable capture/classification as baseline requirements or use labels to imply content/identity.
- **Alternatives:** Require capture/classification in MVP; exclude them permanently.
- **Consequences:** Events must support optional links and unknown outcomes. Failure cannot block survey/detection.
- **Evidence required:** IQ-capture impact, local lawful dataset, classification feasibility, accuracy/uncertainty, and policy approval.

## ADR-011: Do not add a message broker in the initial architecture

- **Status:** ACCEPTED
- **Context:** A single CM4 station has local bounded work boundaries but no demonstrated multi-node or durable-distributed messaging need.
- **Decision:** Use conceptual bounded local asynchronous boundaries; do not select or deploy a broker.
- **Alternatives:** Broker-first architecture; fully synchronous processing.
- **Consequences:** Backpressure/queue/retry behavior remains explicit without adding broker operations. A future broker would require a separate ADR and delivery/ownership guarantees.
- **Evidence required:** Operational need beyond local queues, or evidence that local failure isolation cannot satisfy requirements.

## ADR-012: Use Python as the primary station application language

- **Status:** ACCEPTED
- **Контекст:** Station application RF Sentinel переважно виконує orchestration, scheduling, configuration/validation, керування станом, координацію SDR reservations, subprocess supervision, normalization/control flow, orchestration зберігання даних, reporting, Telegram integration та health/recovery. Початкова архітектура не вимагає виконувати весь real-time RF/DSP pipeline основною мовою станції: acquisition implementations можуть бути зовнішніми процесами за Acquisition Adapter boundary. `rtl_power` / `hackrf_sweep` залишаються evidence-gated experimental baseline candidates за ADR-003, а не постійно закріпленими implementations.
- **Запропоноване рішення:** Використовувати Python як основну мову реалізації station application, не обмежуючи repository лише Python. Зберігати Acquisition Adapter та інші архітектурні межі незалежними від мови реалізації. Допускати майбутні C/C++/Rust та інші native implementations через явні контракти, якщо їхню потребу підтвердять profiling та evidence. Не переносити performance-critical DSP у pure Python без вимірювань, які підтверджують його придатність для цільового навантаження.
- **Організація source/package:** Для початкової організації рекомендується `src/` з одним основним application package та окремі `tests/`. Це наслідок рішення для організації repository/package, який можна переглянути, а не незмінне обмеження архітектури системи. Логічні компоненти можуть бути modules/interfaces усередині одного package; відповідність «architecture component = package» не запроваджується. Цей ADR не створює source code або scaffold.
- **Альтернативи:** C++ або Rust для всієї station application; Python package у корені repository; окремі packages для кожного архітектурного компонента.
- **Обґрунтування:** Початкове навантаження переважно orchestration/I/O/control. Python добре відповідає subprocess lifecycle, parsing, integration і reporting та полегшує створення hardware-free tests і fakes. Очікується менший обсяг implementation/review роботи, ніж із C++/Rust для всієї application, що допомагає Product Owner читати реалізацію та контролювати її розвиток. Performance-critical частини можна оптимізувати окремо після profiling; достатня швидкодія Python для всіх майбутніх workloads не гарантується.
- **Позитивні наслідки:** Очікуються простіша й швидша реалізація orchestration layer, зручне hardware-independent testing, невеликі modules, придатні для review, доступ до широкої Linux integration ecosystem і поступове впровадження native implementations лише там, де це виправдано.
- **Ризики та їх зменшення:** Interpreter і objects додають memory overhead; CPU-heavy pure-Python loops можуть стати bottleneck; dynamic typing потребує дисципліни на boundaries. У разі прийняття рішення Python runtime стане deployment dependency. Blocking work, cancellation, subprocess termination/reaping і clean shutdown потребують явного дизайну. Ризики зменшуються bounded queues/buffers, explicit contracts, validation, automated tests і profiling. Native implementations або додаткова process isolation розглядаються лише за підтвердженої потреби та відповідного architecture review.
- **Архітектурні межі:** Пропозиція не приймає draft SDR Device Architecture APIs і не змінює ADR-003, ADR-006, ADR-008, ADR-009 або ADR-010. Зберігаються RX-only, цільова платформа CM4/Linux ARM64, підтримка RTL-SDR/HackRF, одна systemd-supervised station service із supervised acquisition child processes, Device Manager як єдиний reservation authority та контрольована Command/Query boundary для Telegram без прямого SDR/shell authority. Новий runtime process model або message broker не запроваджується; persistence, SoapySDR та optional IQ/classification не приймаються цим рішенням.
- **Свідомо відкладено:** Точна Python version; package name; dependency/package manager; build backend; `pyproject.toml`; formatter; linter; type checker; test framework; CI; concurrency implementation; configuration format/library; persistence technology; native bindings mechanism; SoapySDR adoption; optional IQ/classification implementation. Конкретні libraries та runtime dependencies не обираються.
- **Review та необхідні підтвердження:** Рішення ACCEPTED. CM4 workload, memory, storage/report processing, endurance і subprocess failure/recovery перевіряються за [EXPERIMENTS.md](research/EXPERIMENTS.md); вибір мови не замінює ці перевірки й не встановлює performance thresholds.

## ADR-013: Separate acquisition and reporting storage/lifecycle boundaries

- **Status:** ACCEPTED / implemented in current stable baseline
- **Context:** Acquisition must continue while reports are read, rendered and delivered. A shared logical database does not require a shared connection or lifecycle.
- **Decision:** Open a separate writable SQLite connection for acquisition persistence and a separate writable SQLite connection for report scheduler incident/state-related persistence. `SQLiteReportEngine` opens a separate read-only SQLite connection (`mode=ro`, `PRAGMA query_only=ON`). Report generation is outside the acquisition path. Report generation/delivery failures are isolated to report health/incident handling and must not terminate acquisition. Shutdown drains/stops components with bounded deadlines and closes each connection independently.
- **Consequences:** SQLite remains the durable hand-off, while writer/read lifecycle and failure ownership are explicit. Report readers cannot initialize or mutate the database. A report may fail or be retried without stopping the SDR worker; committed sweeps remain available for later reports.
- **Evidence:** Current `application.py`, `scheduler.py`, `reporting.py` and `storage.py` implementation plus hardware-free station/report concurrency tests.
