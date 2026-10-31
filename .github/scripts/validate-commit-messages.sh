#!/usr/bin/env bash
set -euo pipefail

validate_subject() {
    local subject="$1"
    local context="$2"

    if [[ ! "$subject" =~ ^\[(feat|fix|docs|chore)\][[:space:]]+.+$ ]]; then
        printf 'Invalid commit subject%s: %s\nExpected prefix: [feat], [fix], [docs], or [chore].\n' \
            "$context" "$subject" >&2
        return 1
    fi
}

if [[ $# -gt 0 ]]; then
    if [[ $# -ne 1 || ! -f "$1" ]]; then
        printf 'Usage: %s [commit-message-file]\n' "$0" >&2
        exit 2
    fi

    IFS= read -r subject < "$1" || true
    validate_subject "$subject" "" || exit 1
    exit 0
fi

base="${BASE_SHA:-}"
head="${HEAD_SHA:?HEAD_SHA must be set}"

if [[ -z "$base" || "$base" =~ ^0+$ ]]; then
    commits="$head"
else
    commits="$base..$head"
fi

invalid=0
while IFS= read -r commit; do
    [[ -z "$commit" ]] && continue
    subject="$(git show -s --format=%s "$commit")"
    if ! validate_subject "$subject" " in $commit"; then
        invalid=1
    fi
done < <(git rev-list "$commits")

if [[ "$invalid" -ne 0 ]]; then
    exit 1
fi
