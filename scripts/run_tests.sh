#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

docker build --tag thirdhand:test "$project_dir"
docker run --rm \
  --env TELEGRAM_API_ID=1 \
  --env TELEGRAM_API_HASH=test-only-value \
  --tmpfs /app/volume/runtime:rw,noexec,nosuid,mode=0700,uid=10001,gid=10001,size=64m \
  thirdhand:test \
  python -m unittest discover -s tests -p 'test_*.py'
