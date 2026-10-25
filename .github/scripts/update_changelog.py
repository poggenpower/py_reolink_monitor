import datetime
import os
import re
import subprocess
from pathlib import Path


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


tag = os.environ["TAG_NAME"]
if not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", tag):
    raise SystemExit(f"Tag {tag!r} must be a semantic version such as v1.2.3.")

tag_commit = git("rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}", check=False)
if not tag_commit:
    raise SystemExit(f"Tag {tag!r} is not available locally.")

changelog_path = Path("CHANGELOG.md")
changelog = changelog_path.read_text()
if any(
    line == f"## {tag}" or line.startswith(f"## {tag} - ")
    for line in changelog.splitlines()
):
    print(f"CHANGELOG.md already contains a section for {tag}; leaving it unchanged.")
    raise SystemExit(0)

previous_tag = ""
for candidate in git("tag", "--merged", tag_commit, "--sort=-version:refname").splitlines():
    if candidate == tag or not re.fullmatch(
        r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", candidate
    ):
        continue
    candidate_commit = git("rev-parse", f"{candidate}^{{commit}}")
    is_ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate_commit, tag_commit],
        check=False,
    )
    if is_ancestor.returncode == 0:
        previous_tag = candidate
        break

revision_range = f"{previous_tag}..{tag_commit}" if previous_tag else tag_commit
subjects = git("log", "--no-merges", "--format=%s", revision_range).splitlines()

categories = (
    ("feat", "Added"),
    ("fix", "Fixed"),
    ("docs", "Documentation"),
    ("chore", "Maintenance"),
)
sections: list[str] = []
for prefix, heading in categories:
    items = []
    pattern = re.compile(rf"^\[{prefix}\]\s+(.+)$")
    for subject in subjects:
        match = pattern.match(subject)
        if match and not re.match(
            r"Update changelog for v?\d+\.\d+\.\d+", match.group(1)
        ):
            items.append(f"- {match.group(1)}")
    if items:
        sections.append(f"### {heading}\n\n" + "\n".join(items))

if not sections:
    sections.append("No categorized commits found since the previous version tag.")

release = f"## {tag} - {datetime.datetime.now(datetime.UTC):%Y-%m-%d}\n\n"
release += "\n\n".join(sections) + "\n"

lines = changelog.splitlines()
if not lines or not lines[0].startswith("# "):
    raise SystemExit("CHANGELOG.md must start with a top-level heading.")

unreleased_index = next(
    (index for index, line in enumerate(lines) if line == "## Unreleased"),
    None,
)
if unreleased_index is not None:
    insertion_index = next(
        (
            index
            for index in range(unreleased_index + 1, len(lines))
            if lines[index].startswith("## ")
        ),
        len(lines),
    )
else:
    insertion_index = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.startswith("## ")),
        len(lines),
    )

lines[insertion_index:insertion_index] = [release.rstrip(), ""]
changelog_path.write_text("\n".join(lines).rstrip() + "\n")
