# Existing SDR Monitoring Solutions and Approaches

## Research scope and reading notes

This is a technical-research inventory, not an architecture decision. “Maintenance” is an observation from public project activity and release/documentation state checked on 2026-09-04; it is not a support guarantee. A project may be mature yet unsuitable as a runtime dependency.

The terms below distinguish integration mechanisms:

| Mechanism | Meaning | Consequence for RF Sentinel |
| --- | --- | --- |
| Command-line subprocess | RF Sentinel starts a version-pinned external executable, supervises it, and parses a documented output contract. | Low DSP implementation effort; requires timeout, exit, output-format, resource, and device-lock handling. |
| Library/API | RF Sentinel calls a stable device or DSP API in-process. | More control and lower process overhead; introduces ABI, lifecycle, error-handling, and binding risk. |
| Implement ourselves | RF Sentinel owns the relevant processing and behavior. | Appropriate for station-specific scheduling, policy, provenance, event history, and reports—not as a default replacement for mature SDR drivers or FFT code. |

## Comparison

| Solution / approach | Primary role | Devices | Maintenance observation | Integration strategy | Why |
| --- | --- | --- | --- | --- | --- |
| rtl-sdr / `rtl_power` | RTL spectrum sweep and FFT logging | RTL2832-based RTL-SDR | Mature upstream; `rtl_power` is maintained in the RTL-SDR codebase | WRAP | Provides CSV PSD sweep output; ideal baseline to validate before considering a custom acquisition path. |
| HackRF / `hackrf_sweep` | HackRF frequency sweep | HackRF | Vendor project/docs are active | WRAP | Mature command-line sweep tool with explicit structured output; validate its limits and recovery behavior on CM4. |
| SoapySDR | Device-neutral SDR API | Depends on installed modules; RTL-SDR/HackRF modules available | Active core project | USE DIRECTLY | It is specifically a vendor/platform-neutral device API; use as a candidate abstraction, after module/device testing. |
| `librtlsdr` | RTL-SDR device API | RTL2832-based RTL-SDR | Mature upstream | USE DIRECTLY | Direct API option where `rtl_power` cannot meet a validated need. Do not recreate USB tuner control. |
| `libhackrf` | HackRF device API | HackRF | Vendor-maintained | USE DIRECTLY | Direct API option where `hackrf_sweep` is insufficient; do not recreate device protocol handling. |
| GNU Radio | DSP flowgraph framework | Broad, through drivers/Soapy/gr-osmosdr | Active, large ecosystem | REUSE CONCEPT | Capable but heavyweight for an unattended survey station; retain as a validation/prototyping and specialized-flowgraph option. |
| Gqrx | Interactive receiver, FFT and waterfall | RTL-SDR, HackRF and others through gr-osmosdr/SoapySDR | Active releases | REFERENCE ONLY | Valuable UI and waterfall/peak-detection reference, but a desktop GUI receiver is not the station backend. |
| SigMF | IQ-data metadata interchange format | Device-neutral | Active standard/community | USE DIRECTLY | Standardizes optional IQ capture provenance and enables offline tools; does not acquire or process signals. |
| `rtl_power` heatmap workflow | Long-term CSV logging to waterfall image | RTL-SDR | Tutorial/example, not a monitored station product | REUSE CONCEPT | Reuse the append/aggregate/heatmap idea, not shell-script operational assumptions or image-only storage. |
| `rx_tools` | Convenience command-line SDR tooling | Article claims RTL-SDR, HackRF, bladeRF and more | Underlying project status requires source verification | REFERENCE ONLY | Useful inspiration for uniform CLI ergonomics; not yet sufficiently verified for a core dependency. |
| SatNOGS client | Scheduled remote SDR observations | Historically SoapySDR/GNU Radio-based | Public GitHub organization lists client as archived | REFERENCE ONLY | Its station provisioning, scheduled-observation, metadata and operations ideas are relevant; mission and archived status preclude a dependency. |
| TorchSig / RadioML | RFML training/dataset tooling | Offline IQ data, device-neutral | TorchSig appears active; RadioML public sets are historical | REFERENCE ONLY | Use for offline feasibility studies and test-data concepts, not live CM4 classification or unvalidated production labels. |

The integration-strategy labels above are research classifications, not accepted architecture decisions or implementation authorization. Any adoption requires the applicable architecture decision and, where identified, evidence-gated approval.

## Detailed assessments

### RTL-SDR `rtl_power`

**Purpose.** `rtl_power` is a wideband spectrum-monitor utility and general-purpose FFT integrator for RTL2832-based receivers. It emits timestamped CSV rows containing frequency bounds, bin step, sample count, and power values.

**Architecture.** It tunes across the requested range in chunks, captures samples with the RTL-SDR driver, performs FFT/integration, and writes a sweep-oriented CSV stream. Separate utilities can turn the CSV into a heatmap.

**Hardware support.** RTL2832-based RTL-SDR devices only.

**Useful concepts.** Stable tabular PSD records; each scan record carrying its frequency geometry; integration/peak/trough choices; and retaining data rather than only images.

**Limitations.** RTL-only; frequency coverage and cadence depend on tuning hops and resolution; CSV is a useful interchange format but not automatically a robust event/history store. Its documented source contains unfinished ideas such as noise correction and multiple-dongle support, so those must not be assumed.

**Maintenance.** Mature upstream RTL-SDR component. Its source and current Debian manpage identify it as the project’s spectrum monitor.

**Integration strategy.** **WRAP**.

**Reason.** Start by supervising a pinned `rtl_power` subprocess and parsing its known output. Revisit direct `librtlsdr` acquisition only if experiments show missing required controls, latency, metadata, or recovery behavior.

Sources: [`rtl_power` source](https://github.com/keenerd/rtl-sdr/blob/master/src/rtl_power.c), [`rtl_power` manpage](https://manpages.debian.org/testing/rtl-sdr/rtl_power.1.en.html).

### HackRF `hackrf_sweep`

**Purpose.** `hackrf_sweep` is HackRF’s command-line spectrum analyser/sweep utility.

**Architecture.** The tool controls a selected HackRF, sweeps a requested range, produces FFT bins, and emits rows containing date/time, frequency bounds, bin width, sample count, and dB values. It supports one-shot/count-bounded operation and text or binary output.

**Hardware support.** HackRF.

**Useful concepts.** Explicit device selection by serial number; one-shot sweep invocation; structured sweep provenance; selected FFT planning; and treating sweep range and bin width as explicit profile fields.

**Limitations.** HackRF-specific. Sweep geometry, retune artifacts, gain behavior, output semantics, and achievable cadence require on-target validation. A third-party visualizer highlights practical 20 MHz chunk boundary considerations; that observation is useful but not a substitute for vendor documentation and experiments.

**Maintenance.** Appears actively maintained by the HackRF project; the current documentation and package manpage cover the tool.

**Integration strategy.** **WRAP**.

**Reason.** Its subprocess boundary fits a first monitoring prototype and avoids reimplementing vendor sweep/DSP behavior. Pin and test output schema, termination, device-loss recovery, and CM4 load.

Sources: [HackRF tool documentation](https://hackrf.readthedocs.io/), [`hackrf_sweep` manpage](https://manpages.debian.org/unstable/hackrf/hackrf_sweep.1.en.html), [chunk-boundary visualizer discussion](https://github.com/G4EA5/hackrf_sweep).

### SoapySDR

**Purpose.** SoapySDR is a vendor- and platform-neutral SDR support library that presents discovery, capability, configuration, and stream APIs through runtime-loadable device modules.

**Architecture.** Applications call the core API; installed device-support modules discover and operate compatible hardware. The application owns stream lifecycle, samples, errors, and normalization of device differences.

**Hardware support.** Determined by modules. The intended devices can be exposed by SoapyRTLSDR and SoapyHackRF modules; actual supported settings remain module/version/device dependent.

**Useful concepts.** Capability discovery before profile activation; a device identity distinct from a friendly name; normalized stream lifecycle; a plugin/module boundary; and explicit handling of unsupported settings.

**Limitations.** “Portable API” does not equal identical hardware behavior. Module discovery, distribution package versions, ABI compatibility, error codes, gain naming, timestamp support, and hot-plug behavior must be tested on the deployed ARM64 image.

**Maintenance.** Active public core project.

**Integration strategy.** **USE DIRECTLY**.

**Reason.** It is the strongest candidate for a future device-abstraction API. Its use must be conditional on a CM4 module compatibility matrix, not treated as a guarantee.

Source: [SoapySDR project](https://github.com/pothosware/SoapySDR).

### `librtlsdr` and `libhackrf`

**Purpose.** These are the native device libraries beneath the RTL-SDR and HackRF ecosystems.

**Architecture.** An in-process caller enumerates a device, configures tuner/stream parameters, and reads samples or uses the project’s tools built on that API.

**Hardware support.** Respectively RTL2832-based RTL-SDR and HackRF.

**Useful concepts.** Stable physical-device identity; explicit configuration validation; stream/device lifecycle; native error reporting; and vendor/project utility behavior as a test oracle.

**Limitations.** Device-specific code paths reduce commonality; direct stream processing shifts tuning, buffering, PSD, scheduling, and recovery responsibility to RF Sentinel. Binding choice and ABI discipline matter on ARM64.

**Maintenance.** Both are mature upstream device stacks; HackRF documentation publishes `libhackrf` and tools.

**Integration strategy.** **USE DIRECTLY**.

**Reason.** They are valid escape hatches for capabilities not available through a wrapper or SoapySDR. They should not be used merely to duplicate functioning sweep tools.

Sources: [RTL-SDR source](https://github.com/keenerd/rtl-sdr), [HackRF documentation](https://hackrf.readthedocs.io/).

### GNU Radio and Gqrx

**Purpose.** GNU Radio supplies reusable DSP blocks and flowgraphs. Gqrx is a desktop receiver/spectrum application built with GNU Radio and Qt.

**Architecture.** GNU Radio connects source, transform, detector/visualization, and sink blocks. Gqrx uses GNU Radio, Qt, and hardware through gr-osmosdr/SoapySDR or device drivers; it can run as an FFT-only instrument.

**Hardware support.** GNU Radio depends on source blocks/drivers. Gqrx documents RTL-SDR and HackRF among gr-osmosdr-supported hardware and can also use SoapySDR.

**Useful concepts.** Stream graph separation; FFT and windowing controls; average/min/max/peak-hold display modes; dropped-frame visibility; and recorded-IQ workflows. Gqrx’s own issue history also illustrates that timer-driven display FFTs can miss pulsed activity—measurement acquisition should not be designed as a GUI refresh loop.

**Limitations.** GNU Radio has a large dependency and operational footprint; Gqrx is GUI-first and not unattended-station software. Neither supplies RF Sentinel’s domain policy, data model, scheduling, event history, retention, Telegram controls, or safety boundaries.

**Maintenance.** GNU Radio and Gqrx appear active; Gqrx publishes recent releases.

**Integration strategy.** GNU Radio: **REUSE CONCEPT**. Gqrx: **REFERENCE ONLY**.

**Reason.** Use them for benchmarking, prototyping, and ideas rather than making the first station dependent on a desktop GUI or a broad DSP runtime without a demonstrated need.

Sources: [GNU Radio](https://www.gnuradio.org/), [Gqrx project](https://github.com/gqrx-sdr/gqrx), [Gqrx releases](https://github.com/gqrx-sdr/gqrx/releases), [Gqrx FFT/waterfall issue](https://github.com/gqrx-sdr/gqrx/issues/1118).

### SigMF and IQ recording

**Purpose.** SigMF is an open metadata specification for recorded signal samples. It pairs a data file with JSON metadata and captures/annotations.

**Architecture.** IQ samples are recorded separately from namespaced metadata. The metadata describes global recording parameters, capture segments, and annotations; extensions add domain-specific fields. Official Python/C++ and GNU Radio integrations exist.

**Hardware support.** Device-neutral.

**Useful concepts.** Reproducible, portable IQ evidence; capture provenance; annotation without altering raw data; sample format/rate/center-frequency metadata; schema validation; and test fixtures.

**Limitations.** It does not define station event history, retention, authorization, streaming, PSD aggregation, or classification validity. Raw IQ can be storage-intensive and must remain optional, bounded, and privacy-governed.

**Maintenance.** Active specification/community project.

**Integration strategy.** **USE DIRECTLY**.

**Reason.** Use the standard for any retained optional IQ evidence rather than inventing a private file+metadata format; map RF Sentinel event provenance to appropriate metadata/extensions after governance review.

Source: [SigMF specification project](https://github.com/sigmf/SigMF).

### Raspberry Pi `rtl_power` heatmap workflow

**Purpose.** The cited tutorial demonstrates long-term RTL-SDR sweep logging on a Raspberry Pi followed by heatmap generation.

**Architecture.** A scheduled shell workflow invokes `rtl_power`, accumulates CSV logs, and calls a separate heatmap generator to create a graphical waterfall.

**Hardware support.** RTL-SDR, Raspberry Pi in the example.

**Useful concepts.** Separate acquisition from visualization; time/frequency/power rows as the heatmap source; periodic processing; and the value of monitoring disk use in long-running logging.

**Limitations.** It is a tutorial, not a resilient station: it does not define validated health, device recovery, access control, configuration history, event semantics, data retention, or multi-SDR operation. Image output alone cannot support reproducible statistics.

**Maintenance.** Article/tutorial; not a maintained station framework.

**Integration strategy.** **REUSE CONCEPT**.

**Reason.** Retain the proven sweep-to-time/frequency visualization pattern, but implement station lifecycle and historical data behavior independently.

Source: [RTL-SDR.com tutorial](https://www.rtl-sdr.com/automatic-heatmap-logging-raspberry-pi-using-rtl-sdr-rtl_power/).

### `rx_tools`

**Purpose.** The cited article presents a command-line family for common SDR tasks across several devices, including spectrum/power tools.

**Architecture.** CLI programs abstract common radio tasks behind commands such as receive and power/spectrum operations.

**Hardware support.** The article claims RTL-SDR, HackRF, bladeRF, and other device support; exact current support and a canonical maintained repository require verification before dependency selection.

**Useful concepts.** Consistent CLI contracts across SDR backends; shell-friendly composition; and selecting specialized tooling rather than rebuilding every radio operation.

**Limitations.** The reference article is not enough to establish release cadence, ABI/API stability, ARM64 packaging, exact device behavior, or a supported integration surface.

**Maintenance.** Unverified for this research pass.

**Integration strategy.** **REFERENCE ONLY**.

**Reason.** Do not add a core dependency until its upstream repository, licensing, release history, device matrix, and ARM64 test results are verified.

Source: [cited `rx_tools` article](https://medium.com/@rxseger/rx-tools-command-line-sdr-tools-for-rtl-sdr-bladerf-hackrf-and-more-rx-fm-rx-sdr-rx-power-2e74f59a9e79).

### Neural-network signal identification, RadioML, and TorchSig

**Purpose.** The cited RTL-SDR article illustrates experimental neural-network signal identification. RadioML supplies historical labeled modulation datasets. TorchSig supplies signal-processing/ML dataset generation and transformation tooling.

**Architecture.** A typical RFML path segments IQ or time-frequency observations, extracts/features or feeds examples to an ML model, and returns a label/confidence. TorchSig focuses on synthetic generation, augmentations, transforms, classification, and detection data structures.

**Hardware support.** Device-neutral input data. None makes a model valid for a particular SDR, antenna, band, or local RF environment by itself.

**Useful concepts.** Separate detection from classification; train/evaluate on controlled, provenance-rich data; include an unknown class/rejection policy; preserve confidence and model/dataset versions; assess confusion and calibration, not only headline accuracy.

**Limitations.** Public RadioML data is historical, largely synthetic, and explicitly not recommended by its publisher as current production data. Classification results may fail to generalize across channel, hardware, bandwidth, SNR, frequency offset, and unlabeled real-world signals. Running model development/inference on CM4 may be resource-constrained. Labels must not suggest decoded content or source identity.

**Maintenance.** The cited article is experimental inspiration. TorchSig appears active; DeepSig labels the public RadioML datasets historical/educational.

**Integration strategy.** **REFERENCE ONLY**.

**Reason.** Classification is optional and requires a separately validated offline research program. Do not make live classification a first-prototype dependency or claim semantics beyond observed features.

Sources: [cited RTL-SDR article](https://www.rtl-sdr.com/deep-learning-neural-network-based-signal-identification-software-for-the-rtl-sdr/), [DeepSig datasets notice](https://www.deepsig.ai/datasets/), [TorchSig](https://github.com/TorchDSP/torchsig).

### SatNOGS client

**Purpose.** SatNOGS historically operated scheduled SDR ground-station observations for satellite reception.

**Architecture.** A station is provisioned/configured for scheduled observations; the client historically uses GNU Radio and SoapySDR and records observation metadata/results for a networked workflow.

**Hardware support.** SDR support is tied to the station’s GNU Radio/SoapySDR path rather than an RF Sentinel device matrix.

**Useful concepts.** Declarative observations; provisioning/runbooks; device readiness checks; reproducible observation metadata; scheduling separate from acquisition; and resilient station operations.

**Limitations.** Satellite mission assumptions, network coupling, rotator/decoding context, and historical client design do not match a general spectrum-observation server. The public organization labels `satnogs-client` as archived.

**Maintenance.** Archived public client repository according to the organization listing.

**Integration strategy.** **REFERENCE ONLY**.

**Reason.** Reuse operating lessons but do not depend on an archived client or force its domain model into RF Sentinel.

Source: [SatNOGS repositories](https://github.com/orgs/satnogs/repositories).

## Cross-cutting findings

- Mature device drivers and FFT/sweep utilities should be reused or wrapped before custom DSP is considered.
- A subprocess is attractive for `rtl_power`/`hackrf_sweep` because it contains vendor-tool failures and offers a quick measured baseline; it is not a substitute for a station supervisor and schema validator.
- A library/API is attractive when RF Sentinel needs continuous IQ, bounded event-triggered captures, or controls unavailable through a sweep CLI. It requires a tested device-capability and failure model.
- RF Sentinel-specific work remains necessary: profile scheduling, provenance, baseline/detection policy, events, aggregation, retention, authorization, health, recovery, and reports.
- GUI waterfalls are useful references but cannot define survey coverage or pulse detection correctness. Acquisition cadence and aggregation semantics must be explicit.
- Signal classification must stay optional, evidence-backed, uncertainty-aware, and separate from content interception.
