# Changelog

## 0.1.0 - 2026-10-25

### Added

- Docker image build and GitHub Actions publishing to GitHub Container Registry.
- Versioned container tags derived from release tags or the latest version tag and build number.
- Configurable recent-recording lookback window; the sensor reports the total matching clip count while retaining details for the 10 most recent clips.
- Fixed local Baichuan UDP ports with per-camera configuration.

### Fixed

- Honor each camera's configured UDP port without allowing one camera's port to affect another camera's connection.
- Qualify published container tags with the GHCR image name so pushes target GitHub Container Registry.
