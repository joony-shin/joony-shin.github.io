# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.40", "python-dotenv>=1.0"]
# ///
"""Truth Social(트럼프) 게시물 → 한국어 블로그 포스트 자동 생성.

두 가지 모드:
  digest   : 특정 날짜(기본=어제 KST)의 게시물을 모아 1개 다이제스트 글 생성
  breaking : 최근 게시물 중 중요도 높은 건만 LLM 판정 후 단독 속보 글 생성

저작권/AdSense 안전: 원문 전재가 아니라 한국어 요약+자체 해설로 가공하고,
각 글 하단에 원문 출처 링크와 AI 생성 고지를 단다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AzureOpenAI

KST = ZoneInfo("Asia/Seoul")
ARCHIVE_URL = "https://ix.cnn.io/data/truth-social/truth_archive.json"

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
POSTS = REPO / "content" / "posts"
STATE_PATH = HERE / "state.json"

load_dotenv(HERE / ".env")

# 속보 판정: 후보 사전필터(LLM 비용 절약) + LLM 중요도 임계값
BREAKING_MIN_TEXT_LEN = 150       # 이보다 짧은 글(리포스트/단문)은 다이제스트로만
BREAKING_LOOKBACK_HOURS = 8       # 폴링 주기보다 넉넉히 (중복은 state 로 방지)
BREAKING_IMPORTANCE_THRESHOLD = 4 # LLM 1~5 점 중 이 이상만 단독 발행


def _client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT_DEFAULT", "gpt-5.4-mini")


def _strip_html(s: str | None) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


def fetch_posts() -> list[dict]:
    """아카이브 JSON 을 받아 정규화. 최신순으로 반환."""
    req = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "trump-blog/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    raw = data if isinstance(data, list) else data.get("posts", data)
    out: list[dict] = []
    for p in raw:
        ca = p.get("created_at")
        if not ca:
            continue
        when = dt.datetime.fromisoformat(ca.replace("Z", "+00:00"))
        out.append(
            {
                "id": str(p.get("id")),
                "when_utc": when,
                "when_kst": when.astimezone(KST),
                "text": _strip_html(p.get("content")),
                "url": p.get("url") or "",
                "media": p.get("media") or [],
                "favourites": int(p.get("favourites_count") or 0),
                "reblogs": int(p.get("reblogs_count") or 0),
                "replies": int(p.get("replies_count") or 0),
            }
        )
    out.sort(key=lambda x: x["when_utc"], reverse=True)
    return out


def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"published_breaking_ids": []}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _llm_json(system: str, user: str, *, max_tokens: int = 4000) -> dict:
    resp = _client().chat.completions.create(
        model=DEPLOYMENT,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.7,
        max_completion_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"
    return json.loads(content)


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _write_post(*, slug: str, title: str, date_kst: dt.datetime, description: str,
                tags: list[str], body: str, sources: list[dict]) -> Path | None:
    POSTS.mkdir(parents=True, exist_ok=True)
    path = POSTS / f"{slug}.md"
    if path.exists():
        print(f"  skip (이미 존재): {path.name}")
        return None
    src_lines = "\n".join(
        f"- [{_strip_html(s.get('text',''))[:80] or '원문'}]({s['url']})" for s in sources if s.get("url")
    )
    footer = (
        "\n\n---\n\n"
        "> 이 글은 도널드 트럼프 미국 대통령의 트루스 소셜 공개 게시물을 한국어로 "
        "요약·해설한 콘텐츠이며, 원문을 그대로 옮긴 것이 아닙니다. AI 가 자동 생성했습니다.\n\n"
        "**원문 출처**\n\n" + (src_lines or "- (출처 없음)")
    )
    seen: set[str] = set()
    uniq_tags = [t for t in tags if t and not (t in seen or seen.add(t))]
    tags_yaml = "[" + ", ".join(f'"{_yaml_escape(t)}"' for t in uniq_tags) + "]"
    front = (
        "---\n"
        f'title: "{_yaml_escape(title)}"\n'
        f"date: {date_kst.isoformat()}\n"
        "draft: false\n"
        f"tags: {tags_yaml}\n"
        f'description: "{_yaml_escape(description)}"\n'
        "---\n\n"
    )
    path.write_text(front + body.strip() + footer + "\n", encoding="utf-8")
    print(f"  생성: {path.name}")
    return path


# ─── digest ──────────────────────────────────────────────────────────────────
DIGEST_SYSTEM = (
    "당신은 미국 정치 뉴스를 한국 독자에게 전하는 블로그 에디터다. "
    "트럼프 대통령의 트루스 소셜 게시물 목록을 받아, 하루치를 정리한 한국어 다이제스트 글을 쓴다. "
    "원문을 그대로 번역해 나열하지 말고, 주제별로 묶어 핵심을 요약하고 맥락·배경을 곁들인 해설을 더한다. "
    "사실에 근거하고 과장하지 말 것. 마크다운 본문은 ## 소제목으로 주제를 나눈다. "
    "반드시 아래 JSON 스키마로만 답한다: "
    '{"title": str, "description": str(80자 이내), "tags": [str], "body_markdown": str}'
)


def run_digest(date_str: str | None) -> int:
    posts = fetch_posts()
    if date_str:
        target = dt.date.fromisoformat(date_str)
    else:
        target = (dt.datetime.now(KST) - dt.timedelta(days=1)).date()
    day_posts = [p for p in posts if p["when_kst"].date() == target]
    if not day_posts:
        print(f"[digest] {target}: 게시물 없음 — skip")
        return 0
    day_posts.sort(key=lambda x: x["when_utc"])
    print(f"[digest] {target}: {len(day_posts)}건")

    lines = []
    for p in day_posts:
        t = p["text"] or "(텍스트 없음 — 미디어/리포스트)"
        lines.append(f"- [{p['when_kst'].strftime('%H:%M')} KST] {t} (좋아요 {p['favourites']:,})")
    user = (
        f"날짜: {target} (KST)\n트럼프 트루스 소셜 게시물 {len(day_posts)}건:\n\n"
        + "\n".join(lines)
    )
    result = _llm_json(DIGEST_SYSTEM, user, max_tokens=6000)
    slug = f"{target.isoformat()}-trump-digest"
    noon = dt.datetime.combine(target, dt.time(12, 0), tzinfo=KST)
    _write_post(
        slug=slug,
        title=result.get("title") or f"트럼프 트루스 소셜 다이제스트 ({target})",
        date_kst=noon,
        description=result.get("description", ""),
        tags=(result.get("tags") or []) + ["트럼프", "다이제스트"],
        body=result.get("body_markdown", ""),
        sources=day_posts,
    )
    return 0


# ─── breaking ────────────────────────────────────────────────────────────────
BREAKING_SYSTEM = (
    "당신은 미국 정치 속보를 한국 독자에게 전하는 블로그 에디터다. "
    "트럼프 대통령의 트루스 소셜 게시물 1건을 받아, 한국어 속보 기사로 가공한다. "
    "먼저 이 게시물이 뉴스 가치가 있는지 1~5 점으로 평가한다(5=중대 정책/외교/시장 영향, 1=잡담). "
    "원문 전재 금지 — 핵심을 요약하고 배경·맥락을 해설한다. 사실에 근거하고 과장 금지. "
    "반드시 아래 JSON 스키마로만 답한다: "
    '{"importance": int(1~5), "title": str, "description": str(80자 이내), '
    '"tags": [str], "body_markdown": str}'
)


def run_breaking() -> int:
    posts = fetch_posts()
    state = _load_state()
    published: set[str] = set(state.get("published_breaking_ids", []))
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(hours=BREAKING_LOOKBACK_HOURS)

    candidates = [
        p for p in posts
        if p["when_utc"] >= cutoff
        and p["id"] not in published
        and len(p["text"]) >= BREAKING_MIN_TEXT_LEN
    ]
    print(f"[breaking] 후보 {len(candidates)}건 (최근 {BREAKING_LOOKBACK_HOURS}h, 미발행, 본문 충분)")
    made = 0
    for p in candidates:
        user = (
            f"게시 시각: {p['when_kst'].isoformat()} (KST)\n"
            f"좋아요 {p['favourites']:,} / 리포스트 {p['reblogs']:,} / 댓글 {p['replies']:,}\n\n"
            f"게시물 원문:\n{p['text']}"
        )
        result = _llm_json(BREAKING_SYSTEM, user, max_tokens=4000)
        importance = int(result.get("importance") or 0)
        published.add(p["id"])  # 판정만 했어도 재평가 방지 (중복 LLM 호출 절약)
        if importance < BREAKING_IMPORTANCE_THRESHOLD:
            print(f"  pass (중요도 {importance}): {p['text'][:50]}...")
            continue
        slug = f"breaking-{p['id']}"
        _write_post(
            slug=slug,
            title=result.get("title") or "트럼프 속보",
            date_kst=p["when_kst"],
            description=result.get("description", ""),
            tags=(result.get("tags") or []) + ["트럼프", "속보"],
            body=result.get("body_markdown", ""),
            sources=[p],
        )
        made += 1

    state["published_breaking_ids"] = sorted(published)[-2000:]  # 무한 증가 방지
    _save_state(state)
    print(f"[breaking] 발행 {made}건")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Truth Social → 한국어 블로그 자동 생성")
    sub = ap.add_subparsers(dest="mode", required=True)
    d = sub.add_parser("digest", help="하루치 다이제스트")
    d.add_argument("--date", help="YYYY-MM-DD (KST, 기본=어제)")
    sub.add_parser("breaking", help="속보 단독 발행")
    args = ap.parse_args()
    if args.mode == "digest":
        return run_digest(args.date)
    if args.mode == "breaking":
        return run_breaking()
    return 1


if __name__ == "__main__":
    sys.exit(main())
