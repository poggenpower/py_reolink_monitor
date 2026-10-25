#!/usr/bin/env bash
set -euo pipefail

if [[ "${REF_TYPE:-}" == "tag" ]]; then
    version="${REF_NAME#v}"
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
        echo "Version tag '$REF_NAME' must be a semantic version such as v1.2.3." >&2
        exit 1
    fi
    tags="$REF_NAME"
    if [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        tags+=$'\nlatest'
    fi
else
    latest_tag=""
    while IFS= read -r candidate; do
        if [[ "$candidate" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)(-[0-9A-Za-z.-]+)?$ ]]; then
            latest_tag="$candidate"
            break
        fi
    done < <(git tag --merged HEAD --sort=-version:refname)

    if [[ -n "$latest_tag" ]]; then
        base_version="${latest_tag#v}"
    else
        base_version="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
    fi

    if [[ "${REF_NAME:-}" == "${DEFAULT_BRANCH:-}" ]]; then
        version="${base_version}-build.${BUILD_NUMBER}"
    else
        branch="$(printf '%s' "${REF_NAME:-unknown}" \
            | tr '[:upper:]' '[:lower:]' \
            | sed -E 's/[^a-z0-9.-]+/-/g; s/^-+//; s/-+$//')"
        version="${base_version}-${branch}.build.${BUILD_NUMBER}"
    fi
    tags="$version"
fi

{
    printf 'version=%s\n' "$version"
    printf 'tags<<EOF\n%s\nEOF\n' "$tags"
} >> "${GITHUB_OUTPUT:-/dev/stdout}"
