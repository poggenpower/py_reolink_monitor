#!/usr/bin/env bash
set -euo pipefail

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
    if [[ ! "$subject" =~ ^\[(feat|fix|docs|chore)\][[:space:]]+.+$ ]]; then
        printf 'Invalid commit subject in %s: %s\nExpected prefix: [feat], [fix], [docs], or [chore].\n' \
            "$commit" "$subject" >&2
        invalid=1
    fi
done < <(git rev-list "$commits")

if [[ "$invalid" -ne 0 ]]; then
    exit 1
fi
