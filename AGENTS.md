# Project Guidelines

## MQTT Topic Layout

- Put Home Assistant discovery configuration under `<discovery_prefix>/<component>/<object_id>/config`.
- Put per-camera state under `<state_prefix>/camera/<camera_id>/state/<entity_key>`. Use the same camera state root for every entity on that camera.
- Publish JSON attributes, when needed, to `<state_prefix>/camera/<camera_id>/state/<entity_key>/attributes`; configure that exact topic as `json_attributes_topic`.
- Put bridge-wide state and commands under `<state_prefix>/bridge/`. Use `<state_prefix>/bridge/metadata/<key>` only for bridge-wide metadata; per-camera values belong in that camera's state namespace, not nested under bridge metadata.
- Keep state payloads consistent with their entity: scalar values on `state_topic`, structured details on the corresponding `/attributes` topic.
- Follow the established retain policy: discovery and bridge metadata/state are retained; camera state and attributes, commands, and scrape-finished events are not retained. Camera entities should expire after three polling intervals.
- When adding or moving a topic, update its Home Assistant discovery config, MQTT command subscription if applicable, README topic documentation, and tests together.

# Commit messages

Commit subjects must start with one of these tags: `[feat]`, `[fix]`, `[docs]`, or `[chore]`, followed by a short description. GitHub Actions validates commit messages on pushes, pull requests, and version tags. To check the format before each local commit, enable the repository hook with `git config core.hooksPath .githooks`; Git then runs the `commit-msg` hook.

## Tests and lint

```sh
pytest
ruff check .
```

If `config.yaml` exists, `pytest` also runs a real-camera integration test against its first configured camera. This test connects to the camera, reads battery state, searches recent recordings, and logs out; it does not connect to MQTT or change camera settings. Without `config.yaml`, that test is skipped. To run it by itself with debug output:

```sh
pytest -s -o log_cli=true --log-cli-level=DEBUG tests/test_real_camera.py
```