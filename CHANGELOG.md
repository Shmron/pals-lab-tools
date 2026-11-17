# Changelog

All notable changes to this repository are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-11-17

### Added
- Initial public release of the PALs Lab tools as a single repository: Climate & Health Atlas, PALs Earth, Harare Flood & Access Tool, CASCADE flood-accessibility model, HE²AT and PRECISE catalogues, Africa road density and proximity maps, Cohort Exposures, ECRI platform and poster judging, DHS Explorer, data access services, course notebooks, and the source of placealert.org and the PALs Portal.
- Documentation: per-tool READMEs, architecture and data-and-privacy notes, security policy, contributing guide, code of conduct and citation file.
- CI: Python syntax check and secret scan on every push and pull request.

### Changed (compared with the deployed code)
- Personal and server-specific details replaced with placeholders (email addresses, home paths, IP addresses, Earth Engine project and service-account names).
- ECRI Poster Judging: the default chief password is now read from `POSTER_CHIEF_PASSWORD`, or generated randomly if unset.
- Notebook outputs cleared.
