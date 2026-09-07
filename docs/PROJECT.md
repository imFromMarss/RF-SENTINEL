# RF Sentinel — Project Definition

## 1. Project mission

Develop an autonomous, RX-only RF spectrum monitoring server that continuously observes configured portions of the radio spectrum, identifies noteworthy activity, records evidence and context for events, maintains historical observations, and presents useful daily findings through a Telegram bot.

The system is for lawful spectrum observation and signal characterization. It must respect privacy, applicable law, and the operational limits of receive-only monitoring.

## 2. Problem being solved

RF activity is often intermittent, spread across wide frequency ranges, and difficult to observe manually over long periods. Operators need a compact system that can monitor selected spectrum ranges unattended, preserve observations when activity occurs, and turn accumulated measurements into understandable historical trends and reports.

## 3. High-level capabilities

- Continuously observe configurable portions of the RF spectrum.
- Detect activity and changes that meet configured or later-defined interest criteria.
- Record interesting events with sufficient time, frequency, and measurement context for later review.
- Characterize observed signals using non-content-based properties such as frequency, bandwidth, duration, power, occupancy, and spectral shape.
- Optionally classify signals where appropriate, subject to later requirements and validated methods.
- Maintain historical statistics and support trend analysis.
- Produce visualizations and daily reports.
- Provide access to summaries and reports through a Telegram bot.
- Operate unattended for extended periods, with operational health and recoverability considered as project requirements.

## 4. Non-goals

- Decrypting protected communications.
- Bypassing access controls, encryption, or other communication protections.
- Intercepting, extracting, or presenting private communication content.
- Transmitting, jamming, or otherwise interacting with RF systems; operation is RX-only.
- Replacing regulatory monitoring equipment or making compliance determinations.
- Making detailed implementation, storage, processing, or deployment architecture decisions at this phase.

## 5. Hardware platform

The primary target is a Raspberry Pi Compute Module 4 running Linux ARM64. The system should be designed with the practical constraints of embedded, low-power, long-running operation in mind, including CPU, memory, storage endurance, USB connectivity, thermal behavior, and power reliability. Specific hardware configurations and performance targets are not yet decided.

## 6. SDR hardware

Initial SDR device support is planned for:

- RTL-SDR
- HackRF

Both devices will be used in receive-only mode. Device-specific supported frequency ranges, bandwidths, sample rates, gain behavior, calibration, USB requirements, and coexistence constraints need to be validated during requirements engineering and prototype work.

## 7. Major system concepts

- **Observation:** A planned or continuous measurement of a configured frequency range.
- **Spectrum range:** A frequency interval selected for observation.
- **Activity:** Measurable RF energy or a material change in spectral conditions within an observation.
- **Event:** A bounded record created when activity satisfies an interest criterion.
- **Characterization:** Describing a signal or event through observable RF properties without relying on communication content.
- **Classification:** Optional assignment of an observed signal to a defined category with a stated confidence or uncertainty.
- **Historical statistics:** Aggregated measurements used to describe occupancy, recurrence, trends, and anomalies over time.
- **Visualization:** A human-readable presentation of current or historical observations.
- **Daily report:** A periodic summary of significant observations, trends, and system status.
- **Telegram bot:** The user-facing channel for requesting or receiving approved summaries and reports.
- **Unattended operation:** Continued operation with limited human intervention, including visibility into health and failures.

## 8. Initial terminology

| Term | Initial meaning |
| --- | --- |
| RF | Radio-frequency electromagnetic spectrum relevant to configured observations. |
| SDR | Software-defined radio receiver hardware used to collect RF measurements. |
| RX-only | Receive-only operation; no transmission or RF interaction. |
| Scan | A sequence of observations across one or more spectrum ranges. |
| Sweep | A measurement pass over a frequency interval. |
| Occupancy | The proportion of time or observations in which a frequency region is active under a defined threshold or rule. |
| Baseline | Expected or typical spectrum behavior used to help identify deviations. |
| Anomaly | Activity differing materially from a baseline or configured expectation. |
| Event evidence | Measurements and metadata retained to explain why an event was recorded. |
| Retention | The policy governing how long measurements, events, and reports are preserved. |

These terms are provisional and will be refined as requirements are agreed.

## 9. Known uncertainties

- Which frequency ranges, regions, and monitoring schedules are required initially?
- What constitutes “interesting” activity for the first users and use cases?
- Which RF measurements and event evidence are necessary, and what data must be excluded for privacy and legal reasons?
- What performance is feasible on the Compute Module 4 for each SDR, including practical bandwidth and scan cadence?
- Is simultaneous use of multiple SDR devices required for the first prototype?
- Which signal categories, if any, should optional classification address, and how will confidence and errors be represented?
- What data retention period, storage capacity, and export requirements are appropriate?
- What visualizations and daily-report content are most valuable?
- How should Telegram access, report delivery, authorization, and operational alerts work?
- What unattended-operation expectations apply to power loss, device disconnects, thermal conditions, software faults, and network outages?
- Which jurisdictions, operators, and regulatory requirements will govern deployment?

## 10. Success criteria for the first prototype

The first prototype is successful when it demonstrates, on the target platform, that RF Sentinel can:

- Use at least one supported SDR in RX-only mode to observe one or more configured spectrum ranges.
- Run an observation session for a meaningful unattended duration to be defined during requirements engineering.
- Detect and record a defined set of observable RF-activity events with basic context.
- Preserve enough measurement history to show at least a simple activity or occupancy trend.
- Produce at least one understandable visualization or periodic summary based on captured observations.
- Make a prototype report or summary available through the intended Telegram-bot interface.
- Expose basic evidence of operational state and failures for review.
- Demonstrate that the prototype remains within the project’s privacy, legal, and RX-only boundaries.

Exact thresholds, test cases, acceptance durations, and supported configurations will be defined later.
