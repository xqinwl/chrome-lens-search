#!/usr/bin/env bash
set -euo pipefail

# Build, tag, and push a versioned Docker image to Docker Hub (or any OCI registry).
#
# Examples:
#   DOCKER_IMAGE=myuser/chrome-lens-search ./scripts/docker-release.sh v1.0.0
#   ./scripts/docker-release.sh v1.0.0 --image myuser/chrome-lens-search
#   ./scripts/docker-release.sh v1.0.0 --platform linux/amd64,linux/arm64 --git-tag
#   ./scripts/docker-release.sh v1.0.1-rc1 --no-latest

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

usage() {
  cat <<'EOF'
Usage: ./scripts/docker-release.sh <version-tag> [options]

Arguments:
  <version-tag>            Release tag, e.g. v1.0.0 or 2026.11.23

Options:
  --image <repo>           Target Docker image repo (e.g. myuser/chrome-lens-search).
                           Falls back to $DOCKER_IMAGE or DOCKER_IMAGE in .env.
  --platform <platforms>   Target platform(s) (default: linux/amd64 for DigitalOcean).
                           Use linux/amd64,linux/arm64 for multi-arch.
  --no-latest              Do not also tag and push :latest
  --no-sha                 Do not also tag and push :sha-<git-short-sha>
  --git-tag                Also create and push matching git tag <version-tag>
  --allow-dirty            Allow releasing with uncommitted git changes
  --dry-run                Print the build/push commands without executing
  -h, --help               Show this help message
EOF
}

read_env_var() {
  local key="$1"
  local file="$2"
  [[ -f "$file" ]] || return 1
  local line
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n 1 || true)"
  [[ -n "$line" ]] || return 1
  local val="${line#*=}"
  val="${val%\"}"
  val="${val#\"}"
  val="${val%\'}"
  val="${val#\'}"
  printf '%s' "$val"
}

VERSION=""
IMAGE="${DOCKER_IMAGE:-}"
PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
PUSH_LATEST=1
PUSH_SHA=1
CREATE_GIT_TAG=0
ALLOW_DIRTY=0
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --image)
      IMAGE="${2:-}"
      shift 2
      ;;
    --platform)
      PLATFORM="${2:-}"
      shift 2
      ;;
    --no-latest)
      PUSH_LATEST=0
      shift
      ;;
    --no-sha)
      PUSH_SHA=0
      shift
      ;;
    --git-tag)
      CREATE_GIT_TAG=1
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -*)
      echo "Error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
    *)
      if [[ -z "$VERSION" ]]; then
        VERSION="$1"
        shift
      else
        echo "Error: unexpected argument: $1" >&2
        usage >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -z "$VERSION" ]]; then
  echo "Error: <version-tag> is required (e.g. v1.0.0)." >&2
  usage >&2
  exit 2
fi

if [[ ! "$VERSION" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]; then
  echo "Error: invalid Docker tag '$VERSION'." >&2
  exit 2
fi

if [[ -z "$IMAGE" ]]; then
  IMAGE="$(read_env_var "DOCKER_IMAGE" "${REPO_ROOT}/.env" || true)"
fi

if [[ -z "$IMAGE" ]]; then
  echo "Error: target image not specified." >&2
  echo "Pass --image <dockerhub-user/repo> or set DOCKER_IMAGE in .env / environment." >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker CLI not found." >&2
  exit 1
fi

if [[ "$ALLOW_DIRTY" -eq 0 ]] && [[ -n "$(git status --porcelain)" ]]; then
  echo "Error: git working tree has uncommitted changes." >&2
  echo "Commit your changes first or pass --allow-dirty." >&2
  git status -s >&2
  exit 1
fi

GIT_SHA="$(git rev-parse --short HEAD)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

TAGS=("${IMAGE}:${VERSION}")
if [[ "$PUSH_SHA" -eq 1 ]]; then
  TAGS+=("${IMAGE}:sha-${GIT_SHA}")
fi
if [[ "$PUSH_LATEST" -eq 1 ]]; then
  TAGS+=("${IMAGE}:latest")
fi

BUILD_CMD=(docker buildx build)

# Multi-platform builds require a docker-container buildx builder when the active driver is 'docker'.
if [[ "$PLATFORM" == *","* ]]; then
  BUILDER_NAME="glens-multiarch"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
      echo "Creating multi-arch buildx builder '${BUILDER_NAME}'..."
      docker buildx create --name "$BUILDER_NAME" --driver docker-container --bootstrap >/dev/null
    fi
  fi
  BUILD_CMD+=(--builder "$BUILDER_NAME")
fi

BUILD_CMD+=(
  --platform "$PLATFORM"
  --label "org.opencontainers.image.version=${VERSION}"
  --label "org.opencontainers.image.revision=${GIT_SHA}"
  --label "org.opencontainers.image.created=${BUILD_DATE}"
)

for tag in "${TAGS[@]}"; do
  BUILD_CMD+=(-t "$tag")
done

BUILD_CMD+=(--push .)

echo "==> Docker Release Summary"
echo "    Image:     ${IMAGE}"
echo "    Platforms: ${PLATFORM}"
echo "    Git SHA:   ${GIT_SHA}"
echo "    Tags:"
for tag in "${TAGS[@]}"; do
  echo "      - ${tag}"
done

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo ""
  echo "[dry-run] Command:"
  printf '  %q' "${BUILD_CMD[@]}"
  echo ""
  if [[ "$CREATE_GIT_TAG" -eq 1 ]]; then
    echo "[dry-run] Git tag:"
    echo "  git tag -a ${VERSION} -m \"Release ${VERSION}\" && git push origin ${VERSION}"
  fi
  exit 0
fi

echo ""
echo "==> Building and pushing image..."
"${BUILD_CMD[@]}"

if [[ "$CREATE_GIT_TAG" -eq 1 ]]; then
  if git rev-parse "$VERSION" >/dev/null 2>&1; then
    echo "Note: git tag '${VERSION}' already exists locally; skipping tag creation."
  else
    git tag -a "$VERSION" -m "Release ${VERSION}"
  fi
  git push origin "$VERSION"
fi

echo ""
echo "==> Release complete!"
echo "Deploy on DigitalOcean with:"
echo "  GLENS_IMAGE=${IMAGE}:${VERSION} docker compose pull && GLENS_IMAGE=${IMAGE}:${VERSION} docker compose up -d --no-build"
