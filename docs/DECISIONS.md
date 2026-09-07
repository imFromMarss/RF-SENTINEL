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

- **Status:** EXPERIMENT REQUIRED
- **Context:** Research identifies `rtl_power` and `hackrf_sweep` as mature candidate sweep tools, but their CM4 performance, output behavior, recovery, and profile sufficiency are not yet demonstrated.
- **Decision:** Evaluate supervised `rtl_power` and `hackrf_sweep` adapters as first baseline implementations; do not make either a permanent dependency yet.
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
- **Decision:** Use one systemd-supervised RF Sentinel service with internal logical components and supervised external acquisition processes. Preserve interfaces that permit a later small-service split.
- **Alternatives:** Separate acquisition and analytics services now; microservices; a monolith with no child-process supervision.
- **Consequences:** A station-process crash briefly affects all internal functions but systemd restarts it. Tool crashes can be contained and recovered separately. Internal component fault containment must be designed deliberately.
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

## ADR-009: Do not select final persistence technology yet

- **Status:** DEFERRED
- **Context:** Requirements distinguish structured records, high-volume measurements, binary captures, artifacts, and logs, but CM4 disk/load/query evidence is absent.
- **Decision:** Define repository/artifact boundaries and authoritative data categories only. Defer database/file-format selection and retention values.
- **Alternatives:** Select one embedded database now; CSV-only; time-series database now.
- **Consequences:** Architecture work remains technology-neutral. Storage interfaces must accommodate source-volume and artifact differences.
- **Evidence required:** Disk, retention, historical query/report, and recovery experiments.

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
