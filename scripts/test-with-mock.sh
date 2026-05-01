#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "==> Checking prerequisites..."
if ! command -v docker &> /dev/null; then
    echo "ERROR: Docker is required"
    exit 1
fi

# Use docker compose (v2) or docker-compose (v1)
if command -v docker compose &> /dev/null; then
    DOCKER_COMPOSE="docker compose"
elif command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    echo "ERROR: docker compose (v2) or docker-compose (v1) is required"
    exit 1
fi

echo "==> Cleaning up any existing containers..."
$DOCKER_COMPOSE down --remove-orphans 2>/dev/null || true

echo "==> Starting Proxmox Mock API containers..."
$DOCKER_COMPOSE up -d --build

echo "==> Waiting for health checks..."
PUBLISHED_HEALTHY=""
LOCAL_HEALTHY=""
for i in {1..30}; do
    if [ -z "$PUBLISHED_HEALTHY" ]; then
        if docker inspect --format='{{.State.Health.Status}}' proxmox-mock-published 2>/dev/null | grep -q "healthy"; then
            echo "✓ proxmox-mock-published is healthy"
            PUBLISHED_HEALTHY=1
        fi
    fi

    if [ -z "$LOCAL_HEALTHY" ]; then
        if docker inspect --format='{{.State.Health.Status}}' proxmox-mock-local 2>/dev/null | grep -q "healthy"; then
            echo "✓ proxmox-mock-local is healthy"
            LOCAL_HEALTHY=1
        fi
    fi

    if [ -n "$PUBLISHED_HEALTHY" ] && [ -n "$LOCAL_HEALTHY" ]; then
        echo "✓ Both mock containers are healthy"
        break
    fi

    if [ "$i" -eq 30 ]; then
        echo "✗ Timeout waiting for containers"
        echo "Container statuses:"
        $DOCKER_COMPOSE ps
        echo "Container logs:"
        $DOCKER_COMPOSE logs
        exit 1
    fi
    sleep 2
done

export PROXMOX_API_MODE=mock
export PROXMOX_MOCK_PUBLISHED_URL=http://localhost:8006
export PROXMOX_MOCK_LOCAL_URL=http://localhost:8007

echo ""
echo "=========================================="
echo "Running Unit/Integration Tests (MockBackend)"
echo "=========================================="
uv run pytest tests --ignore=tests/e2e -m "mock_backend and not mock_http" -v --tb=short || TEST_RESULT=1

echo ""
echo "=========================================="
echo "Running Unit/Integration Tests (HTTP Published)"
echo "=========================================="
uv run pytest tests --ignore=tests/e2e -m "mock_http" -v --tb=short \
    --proxbox-mock-url="$PROXMOX_MOCK_PUBLISHED_URL" || TEST_RESULT=1

echo ""
echo "=========================================="
echo "Running Unit/Integration Tests (HTTP Local)"
echo "=========================================="
uv run pytest tests --ignore=tests/e2e -m "mock_http" -v --tb=short \
    --proxbox-mock-url="$PROXMOX_MOCK_LOCAL_URL" || TEST_RESULT=1

echo ""
echo "=========================================="
echo "Running E2E Tests (all modes)"
echo "=========================================="
uv run pytest tests/e2e -v --tb=short \
    --proxbox-mock-url-published="$PROXMOX_MOCK_PUBLISHED_URL" \
    --proxbox-mock-url-local="$PROXMOX_MOCK_LOCAL_URL" || TEST_RESULT=1

echo ""
echo "==> Cleaning up containers..."
$DOCKER_COMPOSE down --remove-orphans

if [ -n "${TEST_RESULT:-}" ]; then
    echo ""
    echo "✗ Some tests failed"
    exit 1
fi

echo ""
echo "✓ All tests passed successfully!"
