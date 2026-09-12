#!/usr/bin/env bash
#
# Prepare a fresh Debian/Ubuntu VPS to run fxagents.
#
# Idempotent: safe to run again after a failure or a reboot.
#
#   curl -fsSL https://raw.githubusercontent.com/xilowyz-jpg/Ii/claude/salut-6jamph/scripts/setup-vps.sh | bash
#
# or, having cloned the repo already:
#
#   bash scripts/setup-vps.sh

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/xilowyz-jpg/Ii.git}"
BRANCH="${BRANCH:-claude/salut-6jamph}"
TARGET="${TARGET:-$HOME/fxagents}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

log "Checking the machine"
if ! have apt-get; then
    echo "This script expects Debian or Ubuntu. On another distro, install" >&2
    echo "python3 (3.10+), python3-venv and git by hand, then skip to step 3." >&2
    exit 1
fi

disk_free_gb=$(df -BG --output=avail "$HOME" | tail -1 | tr -dc '0-9')
if [ "${disk_free_gb:-0}" -lt 10 ]; then
    echo "WARNING: only ${disk_free_gb}GB free. Three years of gold ticks needs" >&2
    echo "         1-2GB of cache plus room for the CSVs. 20GB is comfortable." >&2
fi

log "Installing packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git tmux

python_version=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
case "$python_version" in
    3.1[0-9]|3.[2-9]*) ;;
    *) echo "Python $python_version is too old; fxagents needs 3.10+." >&2; exit 1 ;;
esac
echo "python $python_version, $(git --version)"

log "Fetching the code into $TARGET"
if [ -d "$TARGET/.git" ]; then
    git -C "$TARGET" fetch --quiet origin "$BRANCH"
    git -C "$TARGET" checkout --quiet "$BRANCH"
    git -C "$TARGET" pull --quiet origin "$BRANCH"
else
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$TARGET"
fi

log "Creating the virtualenv"
if [ ! -d "$TARGET/.venv" ]; then
    python3 -m venv "$TARGET/.venv"
fi
"$TARGET/.venv/bin/pip" install --quiet --upgrade pip
"$TARGET/.venv/bin/pip" install --quiet -e "$TARGET[live,dev]"

log "Running the test suite"
cd "$TARGET"
if ! "$TARGET/.venv/bin/python" -m pytest tests/ -q; then
    echo "The tests did not pass. Do not trust any result from this install." >&2
    exit 1
fi

cat <<EOF

$(printf '\033[1mReady.\033[0m')

  cd $TARGET
  source .venv/bin/activate

Next, download real gold history. It takes roughly 45 minutes for three
years, so run it inside tmux -- closing your SSH session will not kill it:

  tmux new -s fetch
  fxagents fetch --instruments XAU_USD --granularity M5 \\
      --from 2022-01-01 --to 2024-12-31

  (detach with ctrl-b then d; come back later with: tmux attach -t fetch)

Start smaller if you want to see it work first:

  fxagents fetch --instruments XAU_USD --granularity M5 \\
      --from 2024-05-01 --to 2024-05-31
  head -3 data/XAU_USD_M5.csv     # gold should read ~2300-2400 for May 2024

Then look at what the detector finds:

  fxagents smc-scan --instruments XAU_USD --granularity M5 \\
      --source csv --bars 200000 --near-misses 4

EOF
