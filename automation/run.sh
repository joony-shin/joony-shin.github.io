#!/usr/bin/env bash
# cron 진입점: 글 생성 → git commit → push (push 하면 deploy.yml 이 자동 배포)
# 사용: run.sh digest | run.sh breaking | run.sh track
#   digest/breaking : SNS → 한국어 요약·해설 글 (automation/sns_blog.py)
#   track           : 투자 관점 예측 사후검증 리뷰 글 (automation/track_record.py)
set -euo pipefail

MODE="${1:?usage: run.sh digest|breaking|track}"
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

  case "$MODE" in
    track)   "$UV" run automation/track_record.py ;;
    *)       "$UV" run automation/sns_blog.py "$MODE" ;;
  esac

  if [[ -n "$(git status --porcelain content/posts automation/state.json automation/track_state.json)" ]]; then
    git add content/posts
    for f in automation/state.json automation/track_state.json; do
      [[ -f "$f" ]] && git add "$f"
    done
    git commit -m "content($MODE): 자동 생성 $(date '+%F %T')"
    git push origin main
    echo "pushed."
  else
    echo "변경 없음 — push 생략."
  fi
} >> "$LOG" 2>&1
