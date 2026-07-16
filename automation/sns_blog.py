# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.40", "python-dotenv>=1.0"]
# ///
"""국내외 정치·경제계 인사 SNS → 한국어 블로그 포스트 자동 생성 (다중 소스).

소스 어댑터:
  truthsocial : 트럼프 공개 게시물 아카이브(CNN/stiles) — 현재 트럼프 전용
  bluesky     : AT Protocol 공개 API(public.api.bsky.app, 무인증) — 임의 핸들

인물 목록은 sources.json 에서 읽는다. 각 인물에 대해 두 모드:
  digest   : 특정 날짜(기본=어제 KST)의 게시물을 모아 1개 다이제스트 글
  breaking : 최근 게시물 중 LLM 중요도 판정으로 단독 속보 글

저작권/AdSense 안전: 원문 전재가 아니라 한국어 요약+해설로 가공하고,
각 글 하단에 출처 링크와 인용(비평·보도) 고지를 단다.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from openai import AzureOpenAI

sys.path.insert(0, str(Path(__file__).resolve().parent))
from taxonomy import normalize_assets, normalize_sectors, normalize_tags  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
UTC = dt.timezone.utc
TRUTHSOCIAL_ARCHIVE = "https://ix.cnn.io/data/truth-social/truth_archive.json"
BSKY_PUBLIC = "https://public.api.bsky.app/xrpc"
FACEBOOK_GRAPH = "https://graph.facebook.com/v21.0"
ATOM = "{http://www.w3.org/2005/Atom}"

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
POSTS = REPO / "content" / "posts"
STATE_PATH = HERE / "state.json"
SOURCES_PATH = HERE / "sources.json"

load_dotenv(HERE / ".env")

# 속보 판정 파라미터
# (AdSense 저품질 대응: 대량 발행을 멈추고 소수 정예로 — 중요도 5만, 인물당 하루 2건)
BREAKING_MIN_TEXT_LEN = 150        # 짧은 글/리포스트는 다이제스트로만
BREAKING_LOOKBACK_HOURS = 8        # 폴링 주기보다 넉넉히 (중복은 state 로 방지)
BREAKING_IMPORTANCE_THRESHOLD = 5  # LLM 1~5 점 중 이 이상만 단독 발행
BREAKING_MAX_EVAL_PER_RUN = 8      # 인물당 1회 실행 LLM 평가 상한(비용 가드)
BREAKING_DAILY_CAP = 2             # 인물당 하루(KST) 최대 속보 발행 수

# 발행 품질 가드 (thin content 방지)
MIN_BODY_CHARS_BREAKING = 700      # 속보 본문 최소 글자 수 (미달 시 발행 안 함)
MIN_BODY_CHARS_DIGEST = 1000       # 다이제스트 본문 최소 글자 수
DIGEST_MIN_SOURCE_POSTS = 2        # 이 미만 게시물로는 다이제스트를 만들지 않음

# Bluesky 수집 가드
BSKY_PAGE_LIMIT = 100
BSKY_MAX_PAGES = 6

# ─── 투자 관점 공통 지침 ──────────────────────────────────────────────────────
# 정치·경제 발언을 "시장/자산 관점"으로 한 겹 더 해석하되, 매수·매도 권유는 금지한다.
# (한국 자본시장법상 유사투자자문 경계를 넘지 않기 위한 핵심 가드)
INVESTMENT_GUIDE = (
    "또한 이 발언이 자산·시장에 갖는 함의를 투자자 관점에서 한 겹 더 해석한다. "
    "단, 특정 종목·자산의 매수·매도를 권유하거나 목표가·수익률을 단정하지 말 것 — "
    "방향성과 그 근거, 영향받는 자산·섹터, 시간축, 신뢰도를 '정보'로만 제시한다. "
    "정치적 수사(과장·선거용 메시지)와 실제 정책·시장 변수를 반드시 구분하고, "
    "근거가 약하거나 추측이면 is_speculation=true, confidence='하'로 표시한다. "
    "시장 함의가 거의 없는 잡담·개인사·스포츠 글이면 market_impact 를 null 로 둔다. "
    "투자·시장 함의는 오직 market_impact 필드로만 제공하고, "
    "본문(body_markdown)에는 '투자 관점/투자자 관점/시장 전망' 같은 별도 소제목을 만들지 말 것 "
    "(본문은 발언 요약·해설에 집중한다 — 투자 관점 섹션은 시스템이 따로 붙인다)."
)

# market_impact JSON 스키마 조각 (digest/breaking 공용)
MARKET_IMPACT_SCHEMA = (
    '"market_impact": {"summary": str(한 문단, 투자자 관점 핵심), '
    '"assets": [{"name": str(예: WTI 원유, 미국 국채금리, 달러, KOSPI), '
    '"direction": "상승"|"하락"|"변동성 확대"|"중립", "rationale": str}], '
    '"sectors": [str(예: 에너지, 방산, 반도체)], '
    '"time_horizon": "단기"|"중기"|"장기", "confidence": "상"|"중"|"하", '
    '"is_speculation": bool} | null'
)


def _client() -> AzureOpenAI:
    return AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    )


DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT_DEFAULT", "gpt-5.4-mini")


def _strip_html(s: str | None) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


def _get_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "sns-blog/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


# ─── 소스 어댑터 ──────────────────────────────────────────────────────────────
def fetch_truthsocial(_handle: str, *, since_utc: dt.datetime | None = None) -> list[dict]:
    """트럼프 아카이브 전량 반환 (최신순). since_utc 는 무시(전량 보유)."""
    raw = _get_json(TRUTHSOCIAL_ARCHIVE)
    raw = raw if isinstance(raw, list) else raw.get("posts", raw)
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


def fetch_bluesky(handle: str, *, since_utc: dt.datetime | None = None) -> list[dict]:
    """getAuthorFeed (공개 API). since_utc 이전까지만 페이지네이션. 리포스트 제외."""
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(BSKY_MAX_PAGES):
        params = {"actor": handle, "limit": str(BSKY_PAGE_LIMIT), "filter": "posts_no_replies"}
        if cursor:
            params["cursor"] = cursor
        data = _get_json(f"{BSKY_PUBLIC}/app.bsky.feed.getAuthorFeed?{urllib.parse.urlencode(params)}")
        feed = data.get("feed", []) if isinstance(data, dict) else []
        if not feed:
            break
        oldest: dt.datetime | None = None
        for it in feed:
            if it.get("reason"):  # 리포스트(본인 발언 아님) 제외
                continue
            post = it.get("post") or {}
            rec = post.get("record") or {}
            ca = rec.get("createdAt")
            if not ca:
                continue
            try:
                when = dt.datetime.fromisoformat(ca.replace("Z", "+00:00"))
            except ValueError:
                continue
            oldest = when if oldest is None else min(oldest, when)
            uri = post.get("uri") or ""
            rkey = uri.rsplit("/", 1)[-1] if uri else ""
            out.append(
                {
                    "id": rkey or uri,
                    "when_utc": when,
                    "when_kst": when.astimezone(KST),
                    "text": (rec.get("text") or "").strip(),
                    "url": f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else "",
                    "media": [],
                    "favourites": int(post.get("likeCount") or 0),
                    "reblogs": int(post.get("repostCount") or 0),
                    "replies": int(post.get("replyCount") or 0),
                }
            )
        cursor = data.get("cursor") if isinstance(data, dict) else None
        if not cursor:
            break
        if since_utc and oldest and oldest < since_utc:
            break  # 필요한 기간을 모두 덮었다
        time.sleep(0.3)  # 공개 API 예의
    out.sort(key=lambda x: x["when_utc"], reverse=True)
    return out


def _get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (sns-blog/1.0)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def _parse_date(s: str) -> dt.datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    try:  # RFC822 (RSS pubDate)
        d = email.utils.parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        pass
    try:  # ISO8601 (Atom)
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def fetch_rss(url: str, *, since_utc: dt.datetime | None = None) -> list[dict]:
    """RSS 2.0 / Atom 모두 처리. 본문 = 제목 + 요약(HTML 제거). engagement 없음(0)."""
    root = ET.fromstring(_get_bytes(url))
    out: list[dict] = []
    items = root.findall(".//item")
    is_atom = False
    if not items:
        items = root.findall(f".//{ATOM}entry")
        is_atom = True
    for it in items:
        if is_atom:
            title = (it.findtext(f"{ATOM}title") or "").strip()
            summary = it.findtext(f"{ATOM}summary") or it.findtext(f"{ATOM}content") or ""
            when = _parse_date(it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated") or "")
            link_el = it.find(f"{ATOM}link")
            link = (link_el.get("href") if link_el is not None else "") or ""
            uid = it.findtext(f"{ATOM}id") or link
        else:
            title = (it.findtext("title") or "").strip()
            summary = it.findtext("description") or ""
            when = _parse_date(it.findtext("pubDate") or "")
            link = (it.findtext("link") or "").strip()
            uid = it.findtext("guid") or link or title
        if when is None:
            continue
        body = (title + "\n" + _strip_html(summary)).strip()
        out.append(
            {
                "id": hashlib.md5(uid.encode("utf-8")).hexdigest()[:16],
                "when_utc": when.astimezone(UTC),
                "when_kst": when.astimezone(KST),
                "text": body,
                "url": link,
                "media": [],
                "favourites": 0,
                "reblogs": 0,
                "replies": 0,
            }
        )
    out.sort(key=lambda x: x["when_utc"], reverse=True)
    return out


def fetch_facebook(page: str, *, since_utc: dt.datetime | None = None) -> list[dict]:
    """Graph API page feed. 토큰 필요(Meta 앱심사+Page Public Content Access).

    FACEBOOK_GRAPH_TOKEN 미설정 시 빈 목록 반환(비활성). 임의 공개 페이지를 읽으려면
    승인된 앱 토큰이 있어야 한다 — 토큰 없이는 공개 페이지도 빈 응답이 정상이다.
    """
    token = os.environ.get("FACEBOOK_GRAPH_TOKEN", "").strip()
    if not token:
        print(f"    [facebook:{page}] FACEBOOK_GRAPH_TOKEN 미설정 — skip")
        return []
    params = {
        "fields": "id,message,created_time,permalink_url",
        "limit": "50",
        "access_token": token,
    }
    data = _get_json(f"{FACEBOOK_GRAPH}/{page}/posts?{urllib.parse.urlencode(params)}")
    rows = data.get("data", []) if isinstance(data, dict) else []
    out: list[dict] = []
    for p in rows:
        msg = (p.get("message") or "").strip()
        when = _parse_date(p.get("created_time") or "")
        if when is None:
            continue
        out.append(
            {
                "id": str(p.get("id")),
                "when_utc": when.astimezone(UTC),
                "when_kst": when.astimezone(KST),
                "text": msg,
                "url": p.get("permalink_url") or "",
                "media": [],
                "favourites": 0,
                "reblogs": 0,
                "replies": 0,
            }
        )
    out.sort(key=lambda x: x["when_utc"], reverse=True)
    return out


SOURCES = {
    "truthsocial": fetch_truthsocial,
    "bluesky": fetch_bluesky,
    "rss": fetch_rss,
    "facebook": fetch_facebook,
}


def fetch_posts(fig: dict, *, since_utc: dt.datetime | None = None) -> list[dict]:
    fn = SOURCES.get(fig["source"])
    if not fn:
        raise SystemExit(f"알 수 없는 source: {fig['source']}")
    return fn(fig["handle"], since_utc=since_utc)


def platform_label(fig: dict) -> str:
    return {
        "truthsocial": "트루스 소셜",
        "bluesky": "블루스카이(Bluesky)",
        "rss": "공식 보도자료",
        "facebook": "페이스북",
    }.get(fig["source"], fig["source"])


# ─── 상태/파일 ────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"figures": {}}


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
    return json.loads(resp.choices[0].message.content or "{}")


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


_DIR_EMOJI = {"상승": "🔺", "하락": "🔻", "변동성 확대": "⚡", "중립": "➖"}


def _clean_mi(mi: dict | None) -> dict | None:
    """LLM market_impact 정규화. 빈 값/노이즈면 None."""
    if not isinstance(mi, dict):
        return None
    assets = [a for a in (mi.get("assets") or []) if isinstance(a, dict) and a.get("name")]
    sectors = [str(s).strip() for s in (mi.get("sectors") or []) if str(s).strip()]
    summary = (mi.get("summary") or "").strip()
    if not summary and not assets and not sectors:
        return None
    return {
        "summary": summary,
        "assets": assets,
        "sectors": sectors,
        "time_horizon": (mi.get("time_horizon") or "").strip(),
        "confidence": (mi.get("confidence") or "").strip(),
        "is_speculation": bool(mi.get("is_speculation")),
    }


# '투자…' 또는 '시장 + (신호/영향/전망/관점/메시지/함의/반응)' 조합 소제목을 잡는다.
# '주택시장 동향' 같은 실제 뉴스 헤딩(시장+동향)은 보존되도록 어휘를 한정.
_INVEST_HEADING = re.compile(r"투자|시장.{0,10}(신호|영향|전망|관점|메시지|함의|반응)")


def _strip_investment_sections(body: str) -> str:
    """본문에서 '## 투자…/시장 전망' 류 소제목 섹션을 제거.

    market_impact 로 구조화된 '## 투자 관점' 을 따로 붙이므로, LLM 이 본문에
    중복으로 만든 투자 전망 섹션을 결정론적으로 걷어낸다(다음 ## 또는 끝까지).
    """
    lines = body.splitlines(keepends=True)
    out: list[str] = []
    skip = False
    for ln in lines:
        m = re.match(r"^##\s+(.*)$", ln)
        if m:
            skip = bool(_INVEST_HEADING.search(m.group(1)))
            if skip:
                continue
        if not skip:
            out.append(ln)
    return "".join(out).rstrip()


def _render_market_impact(mi: dict | None) -> str:
    """투자 관점 섹션 마크다운. mi 없으면 빈 문자열."""
    if not mi:
        return ""
    out = ["\n\n## 투자 관점\n"]
    if mi["summary"]:
        out.append(mi["summary"] + "\n")
    if mi["assets"]:
        out.append("\n| 자산 | 예상 방향 | 근거 |")
        out.append("\n|---|---|---|")
        for a in mi["assets"]:
            d = (a.get("direction") or "").strip()
            d_disp = f"{_DIR_EMOJI.get(d, '')} {d}".strip()
            rat = (a.get("rationale") or "").replace("|", "·").replace("\n", " ").strip()
            name = str(a.get("name")).replace("|", "·").strip()
            out.append(f"\n| {name} | {d_disp} | {rat} |")
        out.append("\n")
    meta = []
    if mi["sectors"]:
        meta.append("**관련 섹터**: " + ", ".join(mi["sectors"]))
    if mi["time_horizon"]:
        meta.append("**시간축**: " + mi["time_horizon"])
    if mi["confidence"]:
        meta.append("**해석 신뢰도**: " + mi["confidence"])
    if meta:
        out.append("\n" + " · ".join(meta) + "\n")
    if mi["is_speculation"]:
        out.append(
            "\n> ⚠️ 이 해석은 발언의 정치적 수사 가능성을 포함하며, 실제 정책·시장 변수로 "
            "확정된 내용이 아닙니다. 추측성 정보로만 참고하세요.\n"
        )
    return "".join(out)


def _write_post(*, slug: str, title: str, date_kst: dt.datetime, description: str,
                tags: list[str], body: str, fig: dict, sources: list[dict],
                market_impact: dict | None = None) -> Path | None:
    POSTS.mkdir(parents=True, exist_ok=True)
    path = POSTS / f"{slug}.md"
    if path.exists():
        print(f"    skip (이미 존재): {path.name}")
        return None
    mi = _clean_mi(market_impact)
    src_lines = "\n".join(
        f"- [{(s.get('text') or '원문')[:80]}]({s['url']})" for s in sources if s.get("url")
    )
    footer = (
        "\n\n---\n\n"
        f"> 이 글은 {fig['name_ko']}({fig['title']})의 {platform_label(fig)} 공개 게시물을 "
        "한국어로 요약·해설한 콘텐츠입니다. 인용은 비평·보도 목적이며, 원문은 아래 출처에서 확인할 수 있습니다.\n\n"
        "> ⚠️ **투자 유의 고지**: 본 콘텐츠는 공개된 발언을 정리·해설한 정보 제공용이며, "
        "특정 종목·자산의 매수·매도를 권유하는 투자자문이 아닙니다. "
        "투자 판단과 그 결과의 책임은 전적으로 투자자 본인에게 있습니다.\n\n"
        "**원문 출처**\n\n" + (src_lines or "- (출처 없음)")
    )

    def _yaml_list(items: list[str]) -> str:
        seen2: set[str] = set()
        uniq = [x for x in items if x and not (x in seen2 or seen2.add(x))]
        return "[" + ", ".join(f'"{_yaml_escape(x)}"' for x in uniq) + "]"

    seen: set[str] = set()
    uniq_tags = [t for t in tags if t and not (t in seen or seen.add(t))]
    tags_yaml = "[" + ", ".join(f'"{_yaml_escape(t)}"' for t in uniq_tags) + "]"
    front_lines = [
        "---",
        f'title: "{_yaml_escape(title)}"',
        f"date: {date_kst.isoformat()}",
        "draft: false",
        f"tags: {tags_yaml}",
    ]
    if mi:  # 자산/섹터 taxonomy — 통제 어휘로 정규화 (thin page 방지)
        sectors = normalize_sectors(mi["sectors"])
        assets = normalize_assets([a["name"] for a in mi["assets"] if a.get("name")])
        if sectors:
            front_lines.append(f"sectors: {_yaml_list(sectors)}")
        if assets:  # 복수형 taxonomy 키가 'asset' → front matter 키도 'asset'
            front_lines.append(f"asset: {_yaml_list(assets)}")
    front_lines.append(f'description: "{_yaml_escape(description)}"')
    front_lines.append("---\n\n")
    front = "\n".join(front_lines)
    body_out = _strip_investment_sections(body) if mi else body.strip()
    path.write_text(front + body_out + _render_market_impact(mi) + footer + "\n", encoding="utf-8")
    print(f"    생성: {path.name}")
    return path


# ─── digest ──────────────────────────────────────────────────────────────────
def _digest_system(fig: dict) -> str:
    return (
        "당신은 국내외 정치·경제 뉴스를 한국 독자에게 전하는 블로그 에디터다. "
        f"{fig['name_ko']}({fig['title']})가 {platform_label(fig)}에 올린 게시물 목록을 받아, "
        "하루치를 정리한 한국어 다이제스트 글을 쓴다. "
        "원문을 그대로 번역해 나열하지 말고, 주제별로 묶어 핵심을 요약하고 맥락·배경을 곁들인 해설을 더한다. "
        "각 주제마다 발언의 배경(왜 지금 이 발언이 나왔는지), 과거 발언·정책과의 연결, "
        "반대 진영이나 시장의 관점 같은 독자적 맥락을 반드시 더해 깊이를 만든다 — "
        "본문은 공백 포함 1,500자 이상으로 충실하게 쓴다. "
        "사실에 근거하고 과장하지 말 것. 마크다운 본문은 ## 소제목으로 주제를 나눈다. "
        + INVESTMENT_GUIDE + " "
        "반드시 아래 JSON 스키마로만 답한다: "
        '{"title": str, "description": str(80자 이내), "tags": [str], "body_markdown": str, '
        + MARKET_IMPACT_SCHEMA + "}"
    )


def run_digest_for(fig: dict, target: dt.date, state: dict | None = None) -> bool:
    day_start_utc = dt.datetime.combine(target, dt.time(0, 0), tzinfo=KST).astimezone(UTC)
    posts = fetch_posts(fig, since_utc=day_start_utc)
    day_posts = [p for p in posts if p["when_kst"].date() == target]

    # 이미 속보로 단독 발행한 게시물은 제외 — 같은 원문을 두 페이지에 싣지 않는다(중복 콘텐츠 방지)
    if state:
        already = set(state.get("figures", {}).get(fig["key"], {}).get("published_breaking_ids", []))
        before = len(day_posts)
        day_posts = [p for p in day_posts if p["id"] not in already]
        if before != len(day_posts):
            print(f"  [{fig['key']}] {target}: 속보 기발행 {before - len(day_posts)}건 제외")

    if len(day_posts) < DIGEST_MIN_SOURCE_POSTS:
        print(f"  [{fig['key']}] {target}: 게시물 {len(day_posts)}건 < {DIGEST_MIN_SOURCE_POSTS}건 — skip (thin 방지)")
        return False
    day_posts.sort(key=lambda x: x["when_utc"])
    print(f"  [{fig['key']}] {target}: {len(day_posts)}건")

    lines = []
    for p in day_posts:
        t = p["text"] or "(텍스트 없음 — 미디어/링크)"
        lines.append(f"- [{p['when_kst'].strftime('%H:%M')} KST] {t} (좋아요 {p['favourites']:,})")
    user = (
        f"인물: {fig['name_ko']} ({fig['title']})\n날짜: {target} (KST)\n"
        f"게시물 {len(day_posts)}건:\n\n" + "\n".join(lines)
    )
    result = _llm_json(_digest_system(fig), user, max_tokens=6000)
    body_md = result.get("body_markdown", "")
    if len(body_md) < MIN_BODY_CHARS_DIGEST:
        print(f"  [{fig['key']}] {target}: 본문 {len(body_md)}자 < {MIN_BODY_CHARS_DIGEST}자 — 발행 안 함 (thin 방지)")
        return False
    noon = dt.datetime.combine(target, dt.time(12, 0), tzinfo=KST)
    _write_post(
        slug=f"{target.isoformat()}-{fig['key']}-digest",
        title=result.get("title") or f"{fig['name_ko']} 다이제스트 ({target})",
        date_kst=noon,
        description=result.get("description", ""),
        tags=fig["tags"] + normalize_tags(result.get("tags") or []) + ["다이제스트"],
        body=body_md,
        fig=fig,
        sources=day_posts,
        market_impact=result.get("market_impact"),
    )
    return True


# ─── breaking ────────────────────────────────────────────────────────────────
def _breaking_system(fig: dict) -> str:
    return (
        "당신은 국내외 정치·경제 속보를 한국 독자에게 전하는 블로그 에디터다. "
        f"{fig['name_ko']}({fig['title']})가 {platform_label(fig)}에 올린 게시물 1건을 받아, "
        "한국어 속보 기사로 가공한다. "
        "먼저 이 게시물이 뉴스 가치가 있는지 1~5 점으로 평가한다"
        "(5=시장·정책을 실제로 움직일 수 있는 중대 발표, 4=주목할 만한 정책·외교 발언, 3 이하=논평·공방·잡담). "
        "5점은 드물어야 정상이다 — 단순 비판·반복 주장·지지 선언은 3점 이하로 매긴다. "
        "원문 전재 금지 — 핵심을 요약하고, 발언의 배경(왜 지금인지), 과거 발언·정책과의 연결, "
        "예상되는 반응까지 해설을 더해 본문을 공백 포함 1,000자 이상으로 쓴다. "
        "사실에 근거하고 과장 금지. "
        + INVESTMENT_GUIDE + " "
        "반드시 아래 JSON 스키마로만 답한다: "
        '{"importance": int(1~5), "title": str, "description": str(80자 이내), '
        '"tags": [str], "body_markdown": str, '
        + MARKET_IMPACT_SCHEMA + "}"
    )


def run_breaking_for(fig: dict, state: dict) -> int:
    fstate = state["figures"].setdefault(fig["key"], {"published_breaking_ids": []})
    published: set[str] = set(fstate.get("published_breaking_ids", []))
    now = dt.datetime.now(UTC)
    cutoff = now - dt.timedelta(hours=BREAKING_LOOKBACK_HOURS)

    # 인물당 일일(KST) 발행 상한 — 대량 발행으로 인한 저품질 판정 방지
    today_kst = dt.datetime.now(KST).date().isoformat()
    if fstate.get("day") != today_kst:
        fstate["day"] = today_kst
        fstate["day_count"] = 0
    if fstate.get("day_count", 0) >= BREAKING_DAILY_CAP:
        print(f"  [{fig['key']}] 오늘 발행 {fstate['day_count']}건 — 일일 상한({BREAKING_DAILY_CAP}) 도달, skip")
        return 0

    posts = fetch_posts(fig, since_utc=cutoff)
    candidates = [
        p for p in posts
        if p["when_utc"] >= cutoff
        and p["id"] not in published
        and len(p["text"]) >= BREAKING_MIN_TEXT_LEN
    ][:BREAKING_MAX_EVAL_PER_RUN]
    if candidates:
        print(f"  [{fig['key']}] 속보 후보 {len(candidates)}건")
    made = 0
    for p in candidates:
        user = (
            f"인물: {fig['name_ko']} ({fig['title']})\n게시 시각: {p['when_kst'].isoformat()} (KST)\n"
            f"좋아요 {p['favourites']:,} / 리포스트 {p['reblogs']:,} / 댓글 {p['replies']:,}\n\n"
            f"게시물 원문:\n{p['text']}"
        )
        result = _llm_json(_breaking_system(fig), user, max_tokens=4000)
        published.add(p["id"])  # 판정했으면 재평가 방지
        importance = int(result.get("importance") or 0)
        if importance < BREAKING_IMPORTANCE_THRESHOLD:
            continue
        body_md = result.get("body_markdown", "")
        if len(body_md) < MIN_BODY_CHARS_BREAKING:
            print(f"    본문 {len(body_md)}자 < {MIN_BODY_CHARS_BREAKING}자 — 발행 안 함 (thin 방지)")
            continue
        if fstate.get("day_count", 0) >= BREAKING_DAILY_CAP:
            print(f"    일일 상한({BREAKING_DAILY_CAP}) 도달 — 이후 후보 발행 중단")
            break
        _write_post(
            slug=f"breaking-{fig['key']}-{p['id']}",
            title=result.get("title") or f"{fig['name_ko']} 속보",
            date_kst=p["when_kst"],
            description=result.get("description", ""),
            tags=fig["tags"] + normalize_tags(result.get("tags") or []) + ["속보"],
            body=body_md,
            fig=fig,
            sources=[p],
            market_impact=result.get("market_impact"),
        )
        made += 1
        fstate["day_count"] = fstate.get("day_count", 0) + 1

    fstate["published_breaking_ids"] = sorted(published)[-2000:]
    return made


# ─── 엔트리 ───────────────────────────────────────────────────────────────────
def load_figures(only: list[str] | None) -> list[dict]:
    figs = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["figures"]
    if only:
        figs = [f for f in figs if f["key"] in only]
    return figs


def main() -> int:
    ap = argparse.ArgumentParser(description="정치·경제계 인사 SNS → 한국어 블로그")
    sub = ap.add_subparsers(dest="mode", required=True)
    d = sub.add_parser("digest", help="하루치 다이제스트 (전 인물)")
    d.add_argument("--date", help="YYYY-MM-DD (KST, 기본=어제)")
    d.add_argument("--only", help="쉼표구분 key 만 처리")
    b = sub.add_parser("breaking", help="속보 단독 발행 (전 인물)")
    b.add_argument("--only", help="쉼표구분 key 만 처리")
    args = ap.parse_args()
    only = [s for s in (args.only or "").split(",") if s] or None
    figures = load_figures(only)

    if args.mode == "digest":
        target = dt.date.fromisoformat(args.date) if args.date else (dt.datetime.now(KST) - dt.timedelta(days=1)).date()
        state = _load_state()  # 읽기 전용 — 속보 기발행 게시물 중복 게재 방지용
        print(f"[digest] {target} — 인물 {len(figures)}명")
        for fig in figures:
            try:
                run_digest_for(fig, target, state=state)
            except Exception as e:  # 한 인물 실패가 전체를 막지 않도록
                print(f"  [{fig['key']}] 오류: {e}")
        return 0

    if args.mode == "breaking":
        state = _load_state()
        state.setdefault("figures", {})
        print(f"[breaking] 인물 {len(figures)}명 (최근 {BREAKING_LOOKBACK_HOURS}h)")
        total = 0
        for fig in figures:
            if fig.get("no_breaking"):  # 기관 보도자료 등은 다이제스트만 (속보 스팸 방지)
                continue
            try:
                total += run_breaking_for(fig, state)
            except Exception as e:
                print(f"  [{fig['key']}] 오류: {e}")
        _save_state(state)
        print(f"[breaking] 발행 {total}건")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
