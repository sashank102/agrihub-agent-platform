#!/usr/bin/env bash
# Install the external binaries the heavy-tier tools use.
#
#   PLINK2      LD with the lead SNP and LD-mode loci (ld_with_lead, define_locus mode=ld)
#               -> $AGRIHUB_DATA_DIR/_tools/bin/plink2 (or set AGRIHUB_PLINK2)
#   VEP         variant consequences from the Ensembl Plants 63 cache (annotate_variants)
#               -> Docker image ensemblorg/ensembl-vep:release_116.2 (or set AGRIHUB_VEP_IMAGE)
#
# Both are optional: without them the tools report "unavailable" evidence gaps
# and the study still runs. The data they read comes from the heavy tier:
#
#   uv run agrihub-data fetch --species soybean --tier heavy
#   uv run agrihub-data build --species soybean --tier heavy
#
# Usage: scripts/setup_heavy_tools.sh [--no-vep] [--no-plink2]
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="${AGRIHUB_DATA_DIR:-$ROOT_DIR/var/data}"
TOOLS_DIR="$DATA_DIR/_tools/bin"
PLINK2_URL="${PLINK2_URL:-https://s3.amazonaws.com/plink2-assets/alpha7/plink2_linux_x86_64_20261001.zip}"
VEP_IMAGE="${AGRIHUB_VEP_IMAGE:-ensemblorg/ensembl-vep:release_116.2}"

install_plink2=1
install_vep=1
for argument in "$@"; do
  case "$argument" in
    --no-plink2) install_plink2=0 ;;
    --no-vep) install_vep=0 ;;
    *) echo "unknown option: $argument" >&2; exit 2 ;;
  esac
done

if [[ "$install_plink2" == 1 ]]; then
  mkdir -p "$TOOLS_DIR"
  workdir="$(mktemp -d)"
  trap 'rm -rf "$workdir"' EXIT
  echo "Downloading PLINK2 from $PLINK2_URL"
  curl -fsSL "$PLINK2_URL" -o "$workdir/plink2.zip"
  sha256sum "$workdir/plink2.zip"
  unzip -o -q "$workdir/plink2.zip" plink2 -d "$TOOLS_DIR"
  chmod +x "$TOOLS_DIR/plink2"
  "$TOOLS_DIR/plink2" --version
fi

if [[ "$install_vep" == 1 ]]; then
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "Pulling $VEP_IMAGE"
    docker pull "$VEP_IMAGE"
  else
    echo "Docker is not running; skipping $VEP_IMAGE (annotate_variants keeps the GFF location class)." >&2
  fi
fi

echo
echo "Heavy tools ready. Build the heavy tier so the bundle lists the VEP cache and LD panel:"
echo "  uv run agrihub-data fetch --species soybean --tier heavy"
echo "  uv run agrihub-data build --species soybean --tier heavy"
