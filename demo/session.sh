#!/usr/bin/env bash
# One agent turn against the synthetic demo vault, as a runnable script.
#
# Everything the recorded terminal image in the README shows comes from here:
# this file prints the prompt lines, and the lines under each of them are the
# real output of the command above them. Nothing in that image is written by
# hand. `tools/render_terminal_svg.py` runs this script and draws its output;
# `tests/test_demo_recording.py` runs it again and fails if the recording no
# longer matches, so a stale image cannot survive a change in behaviour.
#
# Requirements: `noetrail` on PATH, and this repository as the working
# directory. The vault is a disposable copy in a temporary directory, so the
# checked-in demo data is never modified and no personal data is involved.
#
#   noetrail_demo=$(mktemp -d) && demo/session.sh
set -euo pipefail

# Installed from a wheel, the built-in packs come with the distribution. From
# a source checkout they are in this repository.
if [ -z "${NOETRAIL_BUILTINS_ROOT:-}" ] && [ -d ".knowledge" ]; then
  NOETRAIL_BUILTINS_ROOT="$PWD/.knowledge"
  export NOETRAIL_BUILTINS_ROOT
fi

workspace="$(mktemp -d)"
trap 'rm -rf "$workspace"' EXIT
cp -R demo/data "$workspace/data"
cp -R demo/config "$workspace/config"
cd "$workspace"

# The roots can be passed per call as `--data-root` and `--config-root`; the
# environment variables are the same surface without repeating them.
export NOETRAIL_DATA_ROOT=data
export NOETRAIL_CONFIG_ROOT=config
printf '$ export NOETRAIL_DATA_ROOT=%s NOETRAIL_CONFIG_ROOT=%s\n\n' \
  "${NOETRAIL_DATA_ROOT}" "${NOETRAIL_CONFIG_ROOT}"

# Prints the command it is about to run, then runs it. The printed form is
# derived from the actual argument vector -- there is no second copy of the
# command that could drift away from the one that executed.
step() {
  local rendered=""
  local argument
  for argument in "$@"; do
    case "${argument}" in
      *[!A-Za-z0-9._/=-]*) rendered="${rendered} '${argument}'" ;;
      *) rendered="${rendered} ${argument}" ;;
    esac
  done
  printf '$ noetrail%s\n' "${rendered}"
  noetrail "$@"
  printf '\n'
}

# The agent looks up what the user is asking about. One bounded page with a
# total, a revision for safe follow-up writes, and the pack-defined attributes.
step search harbor

# It captures the new thought immediately, without asking for metadata first.
step capture \
  --type thought \
  --title "Markdown beats a database here" \
  --text "The vault stays readable without the program." \
  --tag design

# The capture lands in the review inbox instead of being silently filed.
step review

# And the vault still validates.
step validate
