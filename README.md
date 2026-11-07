# Reolink Monitor

Monitor Reolink cameras through [`reolink-aio`](https://github.com/starkillerOG/reolink_aio) and publish battery percentage, charging status, and recent recordings to MQTT. Home Assistant entities are created through MQTT discovery.

## Run in the dev container

1. Open this folder in VS Code and choose **Reopen in Container**.
2. Copy `.env.example` to `.env` and set the camera and MQTT credentials.
3. Copy `config.example.yaml` to `config.yaml`; add a camera entry for each camera to monitor.
4. Start the monitor in MQTT mode:

   ```sh
   reolink-monitor --config config.yaml
   ```

The dev container forwards port 1883 for a broker running on the host. Change `MQTT_HOST` in `.env` if your broker is elsewhere. Reolink credentials and MQTT credentials should be supplied through environment variables, not committed in the YAML file.

## Run with Docker

Build the image locally:

```sh
docker build -t reolink-monitor .
```

Mount your configuration file and environment file when starting the container:

```sh
docker run --rm \
  --env-file .env \
  -v "$PWD/config.yaml:/config/config.yaml:ro" \
  reolink-monitor
```

When using Baichuan UDP, enable `udp: true` and explicitly configure a fixed, unique `udp_local_port` for each camera. The protocol communicates this local port as metadata and is very sensitive to port changes: the port must remain unchanged end to end. Do not translate it with NAT or port mapping, including changing the UDP source port. With Docker's default bridge networking, publish each configured UDP port to the container using `-p HOST_PORT:CONTAINER_PORT/udp` (for example, add `-p 44230:44230/udp` to `docker run`), keeping host, container, and configured ports identical. Publishing 1:1 does not prevent other NAT from rewriting the source port. Docker Desktop can be especially challenging because its VM/network NAT may alter UDP ports; host networking avoids Docker's port publishing layer but does not necessarily bypass every platform or network NAT.

The image is published to `ghcr.io/poggenpower/py_reolink_monitor` for both `linux/amd64` and `linux/arm64`; Docker selects the matching architecture when pulling. Version-tagged commits use their tag (for example, `v1.2.3`); other builds use the most recent reachable version tag followed by the GitHub Actions build number. Builds from branches other than the repository's default branch include a normalized branch name in the image version. Pushing a version tag also updates `CHANGELOG.md` on the default branch from commit messages since the previous version tag.

## Commit messages

Commit subjects must start with one of these tags: `[feat]`, `[fix]`, `[docs]`, or `[chore]`, followed by a short description. GitHub Actions validates commit messages on pushes, pull requests, and version tags. To check the format before each local commit, enable the repository hook with `git config core.hooksPath .githooks`; Git then runs the `commit-msg` hook.

## Stdout mode

To emit the Home Assistant discovery configuration and camera state records as JSON Lines instead of connecting to MQTT, run:

```sh
reolink-monitor --config config.yaml --mode stdout
```

Each output line contains a `topic`, a `payload` (discovery payloads are JSON objects and state payloads are strings), and a `retain` flag. Output can be piped to a file or another program. In this mode the MQTT configuration is ignored, so `mqtt:` can be omitted from the YAML and MQTT environment variables are not needed. Camera credentials and camera environment variables are still required.

Add `--debug` to enable verbose application and `reolink-aio` logging while troubleshooting:

```sh
reolink-monitor --config config.yaml --mode stdout --debug
```

Debug logs are written to standard error, leaving stdout JSON Lines usable for piping.

## Configuration

Each camera entry has a required `name`, `host`, `username`, and `password`, plus optional `port` (default `9000`) and `channel` (default `0`). Set `udp: true` to use Baichuan UDP; its local port is fixed at `udp_local_port` (default `44230`, range `1`–`65535`). Configure a different fixed port for each camera if their UDP connections need to coexist. The MQTT section requires a broker `host`; its port defaults to `1883`. Set `mqtt.tls: true` to use TLS with normal certificate and hostname validation. Optionally set `mqtt.ca_cert` to a PEM CA certificate or `mqtt.server_cert` to a PEM broker certificate to validate against that certificate instead of the system trust store; paths are relative to `config.yaml` unless absolute. The two certificate options are mutually exclusive. The `--mqtt-insecure` (or `--insecure`) CLI switch enables TLS while disabling broker certificate and hostname validation; use it only for troubleshooting or brokers with an otherwise untrusted certificate. Polling defaults to every 300 seconds. `recent_recordings_lookback_minutes` controls the recent-recordings window in minutes and defaults to 1440 (24 hours). `recent_recordings_types` optionally limits results to `person`, `other`, `vehicle`, and/or `animal`; an empty list (the default) includes all types. The sensor state reports the total matching count, while its attributes contain details for at most 10 clips. `${VARIABLE}` references in YAML are resolved from the process environment and missing variables fail with an error.

For local execution, install the project and development dependencies and start the module:

```sh
python -m pip install -e '.[dev]'
cp .env.example .env
cp config.example.yaml config.yaml
python -m reolink_monitor --config config.yaml
```

Add `--mode stdout` to the final command to run without an MQTT broker.

## Home Assistant entities

The monitor publishes a **Reolink Bridge** device with a connectivity diagnostic entity. Each configured camera/channel appears as a child device of the bridge. All camera topics are under `<state_prefix>/camera/<camera_id>/state/`, outside the bridge branch, and are exposed as Home Assistant entities:

- Battery percentage (`sensor`, device class `battery`, unit `%`)
- Battery state (`sensor`: `discharging`, `charging`, or `charged`)
- Recent recordings (`sensor` count with a `recordings` attribute containing details for up to 10 clips from the configurable lookback window and type filter; each entry includes filename, start time, duration, and trigger). The sensor count reflects all matching recordings. The Baichuan file-list search includes scheduled as well as event-triggered recordings.
- Camera Clock Offset (`sensor`, seconds): the measured difference between the camera's local clock and UTC, published at `camera/<camera_id>/state/clock_offset`. Its attributes are published at `camera/<camera_id>/state/clock_offset/attributes` and include the sampled camera time, server UTC time, and raw timezone setting reported by the camera.
- Last scraped time (`sensor`, timestamp) and its Unix timestamp (`sensor`, seconds)
- Camera model (`sensor`, diagnostic) published at `camera/<camera_id>/state/model`; `unknown` until the camera reports its model. The detected model is also set as the Home Assistant device model.
- Camera UID, Hardware No, and Firmware Version (`sensor`, diagnostic) published at `camera/<camera_id>/info/uid`, `camera/<camera_id>/info/hardware_no`, and `camera/<camera_id>/info/firmware_version`; `unknown` until the camera reports them.

Bridge topics are grouped under `<state_prefix>/bridge/`:

| Topic | Purpose |
| --- | --- |
| `availability` | Bridge online/offline state and MQTT LWT |
| `metadata/poll_interval_seconds` | Configured poll interval; diagnostic sensor in Home Assistant |
| `metadata/recent_recordings_lookback_minutes` | Configured default lookback; diagnostic sensor in Home Assistant |
| `onetime_lookback_minutes/state` | Current one-time lookback; retained state of the Home Assistant Number |
| `onetime_lookback_minutes/set` | Number command topic; accepts integer minutes and is not retained |
| `onetime_recording_types/state` | Current one-time type filter (`person,vehicle` or `all`); retained state |
| `onetime_recording_types/set` | Text command topic; accepts comma-separated types or `all`, and is not retained |
| `scrape/state` | Current scrape status (`idle`, `running`, `completed`, `partial`, `failed`, or `cancelled`); retained sensor state |
| `scrape/attributes` | Retained JSON attributes for the scrape-status sensor |
| `scrape/trigger` | **Scrape Now** button command (`PRESS`); not retained |
| `scrape/finished` | Non-retained JSON completion event, exposed as a Home Assistant MQTT Event |

Home Assistant Discovery configuration remains under the configured `discovery_prefix` (normally `homeassistant/`). Discovery adds entities for every configured camera and bridge topic, including the camera clock-offset diagnostic, metadata sensors, the one-time lookback Number and recording-types Text, the scrape status sensor, and the scrape-finished Event. The Number and Text use separate state and `/set` command topics so Home Assistant displays values confirmed by the monitor. Their initial and reset values come from configuration. Set them before pressing the button to use those overrides for the next button-triggered scrape; both reset to their configured values when that scrape starts. Use `all` as the one-time filter to include every type. The number accepts values from 1 to 525600 minutes (or the configured default if it is higher).

Discovery config, bridge availability, bridge metadata, one-time lookback and recording-type states, scrape status, and scrape attributes are retained. Camera states and attributes, command messages, and scrape-finished events are not retained; camera sensors expire after three polling intervals. A reachable camera with no battery reading publishes `unknown`. If recording search fails, the recording count is `unknown` and its attributes include the error; battery readings are still published.

Scrapes never overlap: a button press while an interval scrape is running marks that active run as triggered by `both`, and does not start another. That run keeps the lookback and recording types selected when it started; pending one-time values remain set for the next button-triggered scrape that can start. A button press during a button-triggered scrape does not start another scrape. The scrape-finished event contains `event_type`, `status`, and `trigger`, plus the run ID, lookback, recording types, timestamps, and camera failure details. The trigger is `interval`, `button`, or `both`.

## Tests and lint

```sh
pytest
ruff check .
```

If `config.yaml` exists, `pytest` also runs a real-camera integration test against its first configured camera. This test connects to the camera, reads battery state, searches recent recordings, and logs out; it does not connect to MQTT or change camera settings. Without `config.yaml`, that test is skipped. To run it by itself with debug output:

```sh
pytest -s -o log_cli=true --log-cli-level=DEBUG tests/test_real_camera.py
```
