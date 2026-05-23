#!/usr/bin/env bash
# Antigravity & Developer Quality Verification Harness
set -euo pipefail

echo "=== Running Python formatting check (ruff) ==="
if [ -d "backend" ]; then
  # 自動修正とフォーマットの実行
  .venv/bin/ruff format backend/
  .venv/bin/ruff check --fix backend/
  
  # 最終的なリントチェックの検証
  if ! .venv/bin/ruff check backend/; then
    echo "❌ Ruff lint check failed!"
    exit 1
  fi
else
  echo "⚠️ 'backend' directory not found, skipping Python linting."
fi

echo "=== Running Python tests (pytest) ==="
if [ -d "backend" ]; then
  if ! PYTHONPATH=. .venv/bin/pytest backend/; then
    echo "❌ Pytest tests failed!"
    exit 1
  fi
else
  echo "⚠️ 'backend' directory not found, skipping Python tests."
fi

echo "=== Running Swift lint check (swiftlint) ==="
# Find all swift files to check if there are any
swift_files=$(find . -name "*.swift" -not -path "*/.*" -print -quit)
if [ -n "$swift_files" ]; then
  if which swiftlint >/dev/null 2>&1; then
    if ! swiftlint lint; then
      echo "❌ SwiftLint check failed with errors!"
      exit 1
    fi
  else
    echo "⚠️ swiftlint command not found. Skipping Swift lint check."
  fi
else
  echo "ℹ️ No Swift files found, skipping Swift lint check."
fi

echo "✅ All verification checks passed successfully!"
exit 0
