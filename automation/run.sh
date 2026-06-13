#!/usr/bin/env bash
# cron 진입점: 글 생성 → git commit → push (push 하면 deploy.yml 이 자동 배포)
# 사용: run.sh digest | run.sh breaking
set -euo pipefail

MODE="${1:?usage: run.sh digest|breaking}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UV="$HOME/.local/bin/uv"
LOG_DIR="$REPO/automation/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/${MODE}.log"

cd "$REPO"
{
  echo "===== $(date '+%F %T %Z') :: $MODE ====="
  # 최신 상태로 (다른 경로의 커밋과 충돌 방지)
  git pull --rebase --autostash origin main || true

  "$UV" run automation/sns_blog.py "$MODE"

  if [[ -n "$(git status --porcelain content/posts)" ]]; then
    git add content/posts
    git commit -m "content($MODE): 트럼프 트루스 소셜 자동 생성 $(date '+%F %T')"
    git push origin main
    echo "pushed."
  else
    echo "변경 없음 — push 생략."
  fi
} >> "$LOG" 2>&1
