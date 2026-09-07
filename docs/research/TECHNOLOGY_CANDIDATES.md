# Technology Candidates

## Scope

This document turns research into an options register. It does not select an architecture or authorize implementation. ARM64/Raspberry Pi statements mean likely compatibility or documented support, not an RF Sentinel performance guarantee; the CM4 experiment plan remains controlling.

## FACTS

### SDR hardware abstraction

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SoapySDR + device modules | Mature, active core | Common discovery/config/stream API; runtime modules | Module/device differences and packaging ABI risk | Linux ARM64 plausible; must test target image | Module-dependent; intended RTL-SDR/HackRF modules | Medium | Candidate common abstraction API |
| `librtlsdr` | Mature | Native RTL support; ecosystem tools | RTL-only; direct processing ownership | Widely used on Linux/Pi | RTL-SDR | Low–medium | Direct fallback/API for RTL-specific needs |
| `libhackrf` | Mature vendor stack | Native HackRF capability and utilities | HackRF-only; direct processing ownership | Linux ARM64 package/source availability must be validated | HackRF | Low–medium | Direct fallback/API for HackRF-specific needs |

### Spectrum acquisition

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `rtl_power` subprocess | Mature | Sweep/FFT/integration and CSV output already exist | RTL-only; subprocess/schema/recovery management | Demonstrated Raspberry Pi tutorial use; validate CM4 | RTL-SDR | Low | First measured RTL sweep baseline |
| `hackrf_sweep` subprocess | Mature vendor utility | Sweep controls, bounded runs, structured output | HackRF-only; range/cadence artifacts must be tested | Linux ARM64 needs target validation | HackRF | Low–medium | First measured HackRF sweep baseline |
| SoapySDR streaming | Mature API, module-dependent | Unified continuous IQ path and device controls | Requires station-owned tune/PSD/stream logic | Test on target | Module-dependent | Medium | Candidate when CLI sweep limits are proven insufficient |
| GNU Radio flowgraph | Mature, active ecosystem | Broad DSP/device blocks, rapid prototype capability | Large runtime/dependency footprint | Can run on ARM, CM4 workload unknown | Broad via sources/modules | Medium–high | Offline/specialized prototype and benchmark option |

### DSP

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FFTW | Very mature | General C FFT library; planner/wisdom; ARM NEON support documented | Planner/build tuning and licensing review required | ARM NEON documented; benchmark CM4 | Device-neutral | Low–medium | Candidate only for station-owned PSD pipeline |
| VOLK | Mature, active | Portable SIMD kernels with runtime dispatch; ARM NEON target | Not a complete FFT/PSD pipeline | ARM NEON explicitly targeted | Device-neutral | Medium | Candidate optimization layer through GNU Radio or focused use |
| liquid-dsp | Mature DSP library | SDR-oriented C primitives | Broader custom-DSP responsibility; target benchmarks needed | Potential ARM/NEON support, validate | Device-neutral | Medium | Evaluate only if specific needed primitives exceed FFT/tool capabilities |

### Detection

| Candidate / technique | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| PSD threshold relative to baseline/noise floor | Established technique | Explainable, auditable, inexpensive | Threshold/noise drift and intermittent-signal tradeoffs | Strong candidate; measure | Device-neutral | Low | First event-detection method |
| Robust rolling baseline / percentile noise estimate | Established technique | Adapts to local spectrum and gain context | Requires explicit windows, contamination handling, and coverage semantics | Strong candidate; measure | Device-neutral | Low–medium | Baseline candidate alongside fixed threshold |
| Spectral change / occupancy rules | Established technique | Useful for recurring, broad, or persistent activity | Depends on scan cadence/resolution | Strong candidate; measure | Device-neutral | Low–medium | Event enrichment and historical metrics |
| ML anomaly/classification | Research/deployment dependent | May discover patterns beyond simple thresholds | Validation, false confidence, training-data drift, resource cost | Unknown on CM4 | Device-neutral IQ/features | High | Offline research only initially |

### Storage

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SQLite | Very mature | Embedded, transactional, simple backup/query path | Write/concurrency/retention design still required | Strong Linux ARM64 fit | Device-neutral | Low | Candidate metadata/events/config audit store |
| Time-partitioned PSD files (CSV/Parquet or equivalent) | Established pattern | Simple append/archive and external analysis | Format/schema/compaction and query strategy needed | Depends on chosen tooling and disk results | Device-neutral | Medium | Candidate raw/aggregated measurement archive |
| SigMF for optional IQ | Active standard | Portable raw IQ + metadata provenance | Not event/history database; large files | Strong format fit; storage cost must be bounded | Device-neutral | Low | Candidate optional IQ evidence format |

### Visualization

| Candidate / technique | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| Offline raster heatmap from persisted PSD | Established | Simple daily/history visual; decouples capture from UI | Binning, color scale, missing-data semantics must be explicit | Strong candidate; benchmark generation | Device-neutral | Low | First heatmap/report visual |
| Interactive browser chart/waterfall | Mature ecosystem pattern | Exploration and zoom/filter benefits | More service/UI/security complexity | Unknown workload; browser can be remote | Device-neutral | Medium | Later optional view |
| Gqrx visualization behavior | Active GUI reference | Mature examples of plot/waterfall modes | Desktop GUI, not server component | Not a headless station choice | Broad SDR support | Low | Reference for semantics and manual validation |

### Classification

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| Rule-based observable characterization | Established | Explainable; no model training; unknown is natural | Limited labels/nuance | Strong candidate | Device-neutral | Low | Initial characterization, not identity claims |
| TorchSig | Active research toolkit | Synthetic generation, transforms, classification/detection data support | Research dependency; model training/inference not proven on CM4 | Test only; offline host preferable | Device-neutral | High | Offline dataset/model feasibility research |
| RadioML public datasets | Historical research datasets | Common benchmarks, labelled examples | Synthetic/historical; publisher discourages production reliance | Offline only | Device-neutral | High | Benchmark/reference only |
| Locally recorded, lawfully labeled SigMF corpus | Project-owned research asset | Matches real hardware/environment; provenance | Requires governance, labeling effort and leakage controls | Capture may be CM4; training likely elsewhere | Device-neutral | Medium–high | Future validation corpus, after policy approval |

### Telegram

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| Telegram Bot API via maintained client library or direct HTTPS | Mature external platform | Meets report/status/notification requirement | Token management, authorization, delivery/retry semantics | Strong Linux ARM64 fit | Device-neutral | Medium | Authorized control/report notification boundary |

### Service management

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| systemd service units | Mature Linux standard | Boot start, restart policy, logs, dependency ordering, watchdog support | Linux-specific; application must expose useful health | Strong on target Linux | Device-neutral | Low | Primary service supervisor candidate |
| udev device rules/monitoring | Mature Linux subsystem | Stable permissions and device-arrival signals | Device identity and races still need design/testing | Strong on Linux | USB SDRs | Low–medium | Device permission/identity/reconnect support |
| Container runtime | Mature but deployment dependent | Packaging isolation | USB/device, storage, boot, and resource complexity | Possible but needs evidence | Device-dependent passthrough | Medium | Defer pending operational requirement |

### Testing / simulation

| Candidate | Maturity | Advantages | Disadvantages | ARM64 / Raspberry Pi suitability | Hardware compatibility | Project risk | Recommended role |
| --- | --- | --- | --- | --- | --- | --- |
| Recorded SigMF fixtures | Active format/established practice | Deterministic replay, regression cases, no live RF dependency | Does not test USB/device behavior | Strong | Device-neutral | Low | Primary DSP/event regression fixtures |
| Controlled RF source / shielded test setup | Established lab practice | Known activity/absence and calibration-style repeatability | Hardware/setup effort; legal controls | Strong | SDR/antenna dependent | Medium | Hardware acceptance experiments |
| GNU Radio synthetic sources/flowgraphs | Mature | Repeatable generated IQ and impairment scenarios | May not reproduce hardware impairments | Strong enough for offline generation; validate | Device-neutral | Medium | Test-signal generator and analysis reference |
| `rtl_test`, `hackrf_info`, SoapySDR discovery | Mature utilities | Device readiness, enumeration, basic fault diagnosis | Not monitoring acceptance tests | Strong | Respective devices | Low | Provisioning and health-test tools |

## ARCHITECTURAL RECOMMENDATIONS

These are non-binding research recommendations derived from the facts above. They do not authorize implementation; adoption requires the applicable architecture decision and any identified evidence-gated approval.

1. Establish a measured baseline by wrapping `rtl_power` for RTL-SDR scans and `hackrf_sweep` for HackRF scans. Treat both command contracts as versioned external interfaces and supervise them as potentially failing processes.
2. Keep SoapySDR as the leading candidate for a later unified acquisition API, but gate adoption behind a CM4 test matrix covering discovery, configured gains/rates, stream stability, disconnect/reconnect, and packaging.
3. Do not reimplement vendor driver behavior or generic FFT kernels in the first prototype. If a custom PSD pipeline is justified, evaluate FFTW/VOLK and benchmark it against the CLI baselines.
4. Make timestamped PSD/measurement records the source of truth for detection, occupancy, heatmaps, and reports; generate visualizations as derived artifacts.
5. Use explainable baseline-relative PSD and occupancy/change detection first. Preserve unknown/unclassified outcomes.
6. Use SigMF for bounded, explicitly enabled optional IQ captures; keep raw IQ outside the ordinary measurement/event database and subject to retention and governance controls.
7. Use systemd plus Linux device-management facilities for boot/restart and SDR lifecycle support, while keeping station-specific health/recovery logic in the station domain.
8. Keep RFML offline and optional until a locally relevant, legally governed dataset and validation plan exist. Do not infer communication content, transmitter identity, or certainty from a model label.
9. Avoid runtime dependencies on archived SatNOGS client code, unverified `rx_tools`, and desktop GUI applications.

## OPEN QUESTIONS

- Are the CLI sweep tools sufficient for every first-prototype scan profile, including required frequency coverage, revisit behavior, evidence fields, and recovery?
- Does SoapySDR expose compatible RTL-SDR and HackRF behavior on the selected ARM64 distribution and device revisions, or is a hybrid per-device strategy required?
- Which measurement data representation and aggregation granularity best balance disk endurance, report queries, and heatmap quality on CM4 storage?
- Which baseline estimator and detector policy gives acceptable results in the intended RF environments, especially with intermittent and pulsed signals?
- What constitutes a legal and operationally appropriate optional IQ capture duration, retention limit, and authorization process?
- Is simultaneous multi-SDR scanning necessary for the first architecture, and can the selected USB/power topology sustain it?
- What Telegram operations are read-only versus configuration-changing, and what authorization/audit model is sufficient?
- What systemd/udev behavior is available in the chosen CM4 OS image, and how will persistent physical SDR identity be established?
- Is there a justified classification use case with a locally relevant labeled corpus, repeatable test protocol, unknown handling, and CM4 inference budget?

## Key sources

- [SoapySDR](https://github.com/pothosware/SoapySDR)
- [RTL-SDR `rtl_power` source](https://github.com/keenerd/rtl-sdr/blob/master/src/rtl_power.c)
- [HackRF documentation](https://hackrf.readthedocs.io/)
- [SigMF](https://github.com/sigmf/SigMF)
- [FFTW](https://www.fftw.org/)
- [VOLK](https://github.com/gnuradio/volk)
- [TorchSig](https://github.com/TorchDSP/torchsig)
- [DeepSig public dataset notice](https://www.deepsig.ai/datasets/)
- [systemd watchdog documentation](https://github.com/systemd/systemd/blob/main/man/sd_watchdog_enabled.xml)
