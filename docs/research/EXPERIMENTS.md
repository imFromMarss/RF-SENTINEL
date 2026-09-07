# RF Sentinel — CM4 Hardware Experiments

## Purpose

This document lists experiments required to replace assumptions with evidence before setting performance and reliability acceptance thresholds for RF Sentinel. Run each experiment on representative Raspberry Pi Compute Module 4 hardware, Linux ARM64 image, power supply, storage medium, enclosure/cooling arrangement, USB topology, antenna or RF test setup, and SDR firmware/driver versions. Record all of those conditions with the result.

The experiments must not involve transmitting, decrypting protected communications, bypassing access controls, or collecting private communication content. Use shielded, legal, and controlled RF sources where a known signal is needed.

## Common result record

For every experiment, record:

- CM4 and carrier-board variant; RAM size; OS/kernel; storage medium; power and cooling setup.
- SDR make/model, hardware revision, serial identifier or stable test identifier, driver/firmware versions, USB topology, and antenna or controlled RF setup.
- Scan profile or acquisition settings, including frequency range, sample-rate or acquisition bandwidth, gain settings, FFT/measurement settings, and scheduling behavior where relevant.
- Test start/end time, duration, ambient temperature where available, CPU utilization, RAM utilization, disk capacity/use, temperatures, throttling or error indications, USB/SDR errors, and recovered/unrecovered failures.
- Raw logs, resulting measurement/event data, analysis method, and a clear pass/fail or observation conclusion.

## Required experiments

### Sustainable acquisition and processing workload

Measure the maximum sustainable SDR sample rate or equivalent acquisition workload for each RTL-SDR and HackRF test device while performing the intended spectrum measurement pipeline on CM4.

Record CPU utilization, RAM utilization, dropped samples or measurement gaps where observable, processing backlog, thermal state, and stability over a duration sufficient to expose sustained behavior. Repeat for candidate scan profiles rather than assuming a single setting applies universally.

**Decision enabled:** supported profile limits and CPU/RAM acceptance thresholds.

### FFT and spectrum-measurement throughput

Measure FFT throughput and complete spectrum-measurement throughput under representative FFT sizes, averaging/baseline methods, and frequency-span workflows. Establish the relationship among measurement resolution, processing load, and usable output cadence.

**Decision enabled:** measurement settings, scan-profile feasibility, and visualization data volume.

### Scan revisit time and coverage

For representative configured ranges and profiles, measure actual scan revisit time, sweep duration, gaps between observations, and percentage of planned observations completed. Include scheduled transitions and multiple configured profiles where applicable.

**Decision enabled:** coverage reporting, event-detection limitations, and schedule acceptance criteria.

### Noise-floor estimation and activity detection

In a controlled lawful RF environment, characterize noise-floor estimate stability across relevant gain settings, frequency regions, temperatures, and SDRs. Test activity-detection rules with known present/absent or changed RF conditions, recording false events, missed events, detection latency, and environmental limitations.

**Decision enabled:** detector criteria, baseline method, event-evidence fields, and test acceptance cases.

### RTL-SDR long-term stability

Run each representative RTL-SDR through a sustained monitoring workload using realistic schedules and storage policies. Track USB resets, disconnects, retuning failures, measurement gaps, thermal effects, device recovery attempts, and data integrity.

**Decision enabled:** RTL-SDR supported profiles, endurance expectations, and disconnect-recovery policy.

### HackRF long-term stability

Run each representative HackRF through a sustained monitoring workload using realistic schedules and storage policies. Track the same stability, thermal, USB, recovery, and data-integrity indicators as for RTL-SDR.

**Decision enabled:** HackRF supported profiles, endurance expectations, and disconnect-recovery policy.

### USB stability and multiple-SDR behavior

Measure USB stability with each SDR independently and with candidate combinations of attached SDRs, storage devices, and other required peripherals. Test the actual carrier-board ports, hubs, cables, and power arrangement intended for deployment. Observe enumeration, bandwidth contention, resets, reconnect behavior, and whether profiles can run independently.

**Decision enabled:** USB topology, simultaneous-SDR support decision, and device-recovery behavior.

### Thermal behavior

Under worst credible sustained workloads, measure CM4, SDR, and enclosure temperatures; CPU throttling; errors; and post-throttle measurement behavior for candidate cooling arrangements and ambient conditions. Include periods of high disk activity and Telegram/report activity if they materially add load.

**Decision enabled:** cooling requirements, health-alert criteria, and workload limits.

### Disk write throughput, capacity, and endurance implications

Measure sustained and burst disk write throughput and latency for spectrum measurements, event records, heatmap inputs, reports, logs, and optional short IQ captures. Measure storage growth for representative profiles and data-retention behavior when storage limits are approached. Validate that cleanup does not corrupt retained records or impair monitoring.

**Decision enabled:** storage medium choice, retention policy, reserved-capacity policy, IQ-capture bounds, and report/history retention criteria.

### Reboot and automatic-recovery behavior

Test normal reboot, unexpected process termination, temporary storage-pressure conditions, temporary network loss, SDR disconnect/reconnect, and device-unavailable-at-boot conditions. Record startup behavior, time to usable monitoring, data gaps, recovery attempts, error visibility, and whether notifications are queued, sent, or recorded as failed.

**Decision enabled:** restart behavior, retry policy, health model, and notification expectations.

### Historical processing and report generation

Using retained representative or generated non-content measurement data, measure the resource cost and completion behavior of occupancy calculations, heatmap generation, event-history queries, and daily-report generation across increasing retention periods and configured frequency coverage.

**Decision enabled:** aggregation intervals, retention scalability, report schedule, and visualization scope.

### Optional IQ capture impact

If IQ capture remains in scope, measure the storage, CPU, memory, USB, and monitoring-coverage impact of candidate bounded capture settings. Verify metadata integrity, retention cleanup, and that capture does not silently cause unacceptable gaps in priority observations.

**Decision enabled:** whether IQ capture is enabled, its policy bounds, and its interaction with scans.

### Signal classification feasibility

If classification remains in scope, assemble lawful, non-content reference observations for proposed categories and unknown cases. Evaluate reproducibility across devices, environments, and time, including confusion with unknown signals and confidence calibration.

**Decision enabled:** approved classes, confidence presentation, unknown handling, and whether classification is suitable for the prototype.

## Exit criteria for experiments

Architecture and binding performance acceptance thresholds may be set only after the relevant experiments have documented reproducible results, identified constraints, and recorded the hardware/software configurations to which those conclusions apply. Unsupported combinations remain experimental rather than implied supported configurations.
