# RF Sentinel — System Requirements

## Purpose and requirement conventions

These requirements define the intended behavior of an unattended, RX-only RF spectrum monitoring station on a Raspberry Pi Compute Module 4 (CM4) running Linux ARM64. They guide later architecture work; they do not prescribe an implementation.

Requirements are prioritized using MoSCoW:

- **MUST**: required for the first operational system.
- **SHOULD**: important, but may be deferred if evidence or prototype constraints require it.
- **COULD**: valuable optional capability.
- **OUT OF SCOPE**: explicitly excluded from this project scope.

Where a numeric threshold depends on the specific CM4, storage, SDR, antenna, spectrum range, or scan profile, acceptance is based on a documented configuration and measured experimental result rather than an invented universal number. Required experiments are listed in [EXPERIMENTS.md](research/EXPERIMENTS.md).

## MUST

### RF monitoring and scheduling

- The system MUST operate RX-only and MUST NOT intentionally transmit, jam, decrypt protected communications, bypass access controls, or collect communication content.
- The system MUST monitor one or more operator-configured frequency ranges using a supported connected SDR.
- Each configured range MUST state its frequency bounds and an associated scan profile.
- The system MUST support scan profiles that define, at minimum, the intended range or ranges, measurement settings, activity-detection settings, and scan behavior.
- The system MUST support scheduled scanning, including enabled/disabled periods and a defined timezone or time basis.
- The system MUST record which scan profile and configuration version produced each measurement and event.
- The system MUST retain a timestamped spectrum measurement record sufficient to support later activity, occupancy, and trend analysis.
- The system MUST estimate and retain a noise-floor or baseline value associated with the relevant measurement context; the method and limitations MUST be documented.

**Acceptance:** Given a valid configured range and active scheduled scan profile, the station performs observations within the schedule and retains timestamped measurement records that identify the range, profile, SDR, and configuration version. A review of retained records demonstrates a documented noise-floor estimate for the same observation context.

### SDR support

- The system MUST support RTL-SDR hardware in receive-only operation.
- The system MUST support HackRF hardware in receive-only operation.
- The system MUST identify the SDR used for every measurement, event, optional IQ capture, and health record.
- The system MUST validate that selected scan-profile settings are compatible with the connected SDR before attempting an observation, and report an actionable configuration or device error when they are not.
- The system MUST support a configured SDR selection for each scan profile. Simultaneous use of multiple SDRs is not required unless later promoted from a SHOULD requirement.

**Acceptance:** On the target CM4 and supported Linux environment, each listed SDR can independently complete a documented representative scan profile in RX-only mode. Invalid device/profile combinations are rejected or reported without silent data mislabeling.

### Detection, events, and characterization

- The system MUST detect signal activity using configured rules that compare measurements with a documented threshold, baseline, or other stated criterion.
- The system MUST create an RF event when configured activity criteria are met.
- Each RF event MUST retain, at minimum, event time, observed frequency or range, triggering criterion, relevant measurement values, SDR identity, scan-profile identity, and configuration version.
- The system MUST maintain event history and allow retained events to be retrieved in time order and filtered by time range, frequency range, scan profile, and event state or type where applicable.
- The system MUST characterize events using observable, non-content-based properties available from its measurements, such as frequency, apparent bandwidth, duration, signal level relative to its baseline, spectral shape, or recurrence. Fields unavailable for an event MUST be represented as unavailable, not fabricated.
- The system MUST handle unrecognized signals by retaining them as unknown or unclassified events without implying an identity or content.

**Acceptance:** In controlled test observations containing a documented activity condition and a documented non-activity condition, the configured detector creates events only according to its configured criterion. Retrieved event history includes the required provenance and characterization fields, and an unrecognized event is shown as unknown/unclassified.

### Historical information, visualizations, and reports

- The system MUST calculate historical statistics from retained measurements and events, including spectrum occupancy for configured frequency bins or intervals under a documented definition.
- The system MUST provide a time-versus-frequency visualization (heatmap) from retained spectrum measurements for a selectable historical interval where data exists.
- The system MUST generate a daily report for the configured reporting period. It MUST identify the period covered and summarize monitored scan profiles, significant events or activity, occupancy or trend information where available, and material system-health or data-coverage exceptions.
- The system MUST distinguish no observed activity, unavailable data, and disabled or unscheduled observation in historical output and reports.

**Acceptance:** From a retained representative dataset, the system produces an occupancy result whose calculation can be reproduced from the underlying documented measurement criteria, a heatmap covering an available interval, and a daily report with the required period and coverage distinctions.

### Telegram control and notifications

- The system MUST provide a Telegram bot interface for authorized users to retrieve the latest daily report and a current system-status summary.
- The system MUST restrict Telegram commands and report access to an explicit allowlist or equivalent authorized identity mechanism.
- The system MUST send Telegram notifications for configured material operational conditions, including an unavailable SDR, failed recovery after a defined retry policy, critically constrained retained-storage capacity, and failure to generate or deliver a scheduled daily report.
- The system MUST make notification delivery failures visible in local logs and system health information.

**Acceptance:** An authorized test identity can retrieve the latest report and status. An unauthorized test identity cannot access protected responses. Each configured test health condition produces a notification attempt and a retained record of its delivery outcome.

### Reliability, health, storage, and recovery

- The system MUST be designed for long-running unattended operation and MUST expose its operational health locally and through the authorized Telegram status summary.
- System health MUST include, at minimum, process/service state, current or most recent scan state, SDR connection state, last successful observation time, storage state, and last material error.
- The system MUST log operational lifecycle events, configuration changes, scan failures, SDR errors, recovery attempts, report generation/delivery outcomes, and security-relevant access denials. Logs MUST contain timestamps and be protected from uncontrolled growth by a documented retention or rotation policy.
- The system MUST manage disk usage through a documented retention policy covering measurements, events, optional IQ captures, reports, and logs. It MUST record and report when data is removed or cannot be retained because of this policy.
- The system MUST avoid unbounded disk growth and MUST preserve system-operability reserve behavior according to a configurable and documented storage policy.
- The system MUST automatically start the monitoring service after a normal system reboot when monitoring is enabled.
- The system MUST detect an SDR disconnect or unavailable SDR, record the condition, and attempt recovery according to a documented retry policy. It MUST resume the applicable scan after the SDR becomes usable, or surface a persistent failure through health status and configured notification.
- The system MUST recover from an individual scan or processing failure without requiring an operator to restart the entire station when recovery is possible.

**Acceptance:** A documented endurance test and fault-injection test demonstrate that service restart after reboot, simulated or physical SDR disconnect/reconnect, failed scan recovery, log retention, and storage-retention behavior operate as specified. Endurance duration and acceptable recovery behavior are set from recorded CM4 experiments and approved operational requirements.

### Configuration and security

- The system MUST use a versioned, validated configuration source for scan profiles, schedules, detector settings, retention policy, Telegram authorization, and operational settings.
- The system MUST reject invalid configuration without replacing the last known valid active configuration, and MUST record a clear validation error.
- Configuration changes MUST be auditable with timestamp, actor or source when available, previous and new version identifiers, and outcome.
- The system MUST keep secrets, including Telegram credentials, out of reports, events, ordinary logs, and source-controlled configuration.
- The system MUST follow least-privilege principles for service execution and data access, subject to target-platform capabilities.
- The system MUST provide an authenticated and authorized path for configuration-changing operations; Telegram read access alone MUST NOT imply configuration-change authority.

**Acceptance:** Invalid test configuration leaves the prior valid configuration active and records a validation result. Authorized configuration changes create an audit record. Inspection of test outputs confirms secrets are redacted or absent, and unauthorized configuration attempts are denied and logged.

## SHOULD

- The system SHOULD support more than one attached SDR and independently assign configured scan profiles to available devices, subject to CM4 resource validation.
- The system SHOULD support optional, bounded short IQ capture associated with a qualifying event, only when explicitly enabled in its scan profile or event policy.
- IQ captures SHOULD contain metadata sufficient to interpret them, including time, center frequency, sample rate or equivalent acquisition setting, SDR identity, duration, triggering event, and configuration version.
- IQ capture policy SHOULD support bounds for duration, storage use, and retention, and SHOULD allow capture to be disabled globally.
- The system SHOULD offer optional signal classification based only on authorized, non-content-based observable features. Classification output SHOULD identify the model or rule version, confidence or uncertainty, and an unknown/unclassified outcome.
- The system SHOULD preserve unknown events for later review and support operator annotation without overwriting original evidence or provenance.
- The system SHOULD report measurement coverage, missed scans, and scan revisit characteristics so that absence of data is not mistaken for absence of RF activity.
- The system SHOULD support configurable alert rules for RF events, including suppression or aggregation to prevent notification storms.
- The system SHOULD provide daily-report delivery through Telegram in addition to on-demand retrieval.
- The system SHOULD provide health alerts for repeated scan failures, thermal/resource conditions that impair valid operation, clock/time synchronization issues, and prolonged absence of measurement coverage.
- The system SHOULD expose a backup/export path for reports, event metadata, and selected historical statistics, with retention and access controls defined later.
- The system SHOULD document device-specific calibration limits, frequency-reference assumptions, and measurement uncertainty where those affect interpretation.

**Acceptance:** Each adopted SHOULD requirement is accepted through a representative end-to-end test on target hardware, with its configuration, evidence, and limitations recorded. IQ capture and classification are accepted only after their privacy, storage, and accuracy criteria are separately defined.

## COULD

- The system COULD support adaptive scan profiles that change priority using historical occupancy, recent events, or operator-defined rules.
- The system COULD support configurable event grouping to represent repeated activity as a single incident with constituent observations.
- The system COULD provide comparative visualizations across days, scan profiles, or selected frequency intervals.
- The system COULD support export formats intended for offline analysis, subject to approved data-governance requirements.
- The system COULD support a local read-only status view in addition to Telegram.
- The system COULD provide configurable anomaly detection based on learned baselines once its behavior can be validated on representative deployments.
- The system COULD support operator-approved labeling workflows to improve future classification, without using protected communication content.

## OUT OF SCOPE

- Decrypting, decoding protected payloads, or bypassing encryption or access controls.
- Intercepting, transcribing, storing, or distributing private communication content.
- Any transmit capability, jamming, active probing, or interaction with RF devices or networks.
- Automated regulatory enforcement, legal conclusions, or certification-grade spectrum measurement claims.
- Guaranteed identification of signal source, transmitter location, owner, or user from passive observations alone.
- A guarantee that every RF transmission within a configured range will be detected; coverage depends on validated hardware, antenna, environment, scan profile, and operating conditions.
- Detailed architecture, library, database, message-bus, user-interface, or deployment-technology selection at this requirements stage.

## Open acceptance baselines

The following values require evidence from target-hardware experiments and approval before they become binding acceptance thresholds: supported scan ranges and bandwidths, scan revisit time, measurement resolution, detector false-positive/false-negative behavior, maximum sustainable workload, endurance duration, storage reserve and retention horizons, recovery time, heatmap/report generation capacity, and classification quality. See [EXPERIMENTS.md](research/EXPERIMENTS.md).
