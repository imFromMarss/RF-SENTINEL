# RF Sentinel

RF Sentinel is an autonomous, RX-only RF spectrum monitoring server for Raspberry Pi Compute Module 4 systems. It is intended to continuously observe configured spectrum ranges, detect notable RF activity, retain useful event and historical data, and make daily findings available through a Telegram bot.

The project focuses on spectrum observation and signal characterization. It is not intended to decrypt protected communications, bypass access controls, or intercept private communication content.

## Project status

The project is in the **SYSTEM ARCHITECTURE** phase. See [the project definition](docs/PROJECT.md) and [current status](docs/STATUS.md).

## Scope of the first prototype

The first prototype will establish a safe, repeatable basis for unattended observation with supported SDR hardware. Requirements and the initial system architecture are documented, and the SDR device architecture has been drafted for review. Quantitative thresholds, final persistence technology, device-specific limits, and other evidence-dependent decisions remain unresolved pending architecture review and evidence from CM4 experiments.

## Target environment

- Raspberry Pi Compute Module 4
- Linux ARM64
- RX-only operation
- RTL-SDR and HackRF SDR devices

## Documentation

- [Project definition](docs/PROJECT.md) — mission, scope, non-goals, and initial terminology.
- [System requirements](docs/REQUIREMENTS.md) — normative behavior and acceptance expectations.
- [Project status](docs/STATUS.md) — current phase and review status.
- [Initial system architecture](docs/ARCHITECTURE.md) — accepted responsibility boundaries, components, and flows.
- [Architecture decisions](docs/DECISIONS.md) — decision records and their status.
- [Architecture questions](docs/ARCHITECTURE_QUESTIONS.md) — unresolved questions and required evidence.
- [SDR device architecture](docs/design/SDR_DEVICE_ARCHITECTURE.md) — detailed Device Manager and Acquisition Adapter design, currently under review.
- [Existing solutions research](docs/research/EXISTING_SOLUTIONS.md) — non-binding assessment of relevant SDR approaches.
- [Technology candidates](docs/research/TECHNOLOGY_CANDIDATES.md) — non-binding options register and candidate directions.
- [CM4 hardware experiments](docs/research/EXPERIMENTS.md) — planned evidence for hardware- and performance-dependent decisions.
