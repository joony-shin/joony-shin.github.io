# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""기존 발행 글 일괄 정규화 (one-off migration, 2026-07).

AdSense 'low value content' 대응:
  1) front matter 의 tags/sectors/asset 을 taxonomy.py 통제 어휘로 정규화
     → 글 1개짜리 taxonomy term 페이지 폭증 제거
  2) 본문이 기준 미달로 짧은 글(구제 불가 thin page)은 삭제

사용:
  uv run automation/normalize_posts.py --dry-run   # 변경 내용만 출력
  uv run automation/normalize_posts.py             # 실제 적용
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from taxonomy import TOPIC_TAGS, TYPE_TAGS, normalize_assets, normalize_sectors, normalize_tags  # noqa: E402

REPO = HERE.parent
POSTS = REPO / "content" / "posts"
SOURCES_PATH = HERE / "sources.json"

MIN_BODY_WORDS = 120  # 본문(footer 제외)이 이 미만이면 삭제 대상


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_list(items: list[str]) -> str:
    return "[" + ", ".join(f'"{_yaml_escape(x)}"' for x in items) + "]"


def _parse_list_line(fm: str, key: str) -> list[str] | None:
    m = re.search(rf'^{key}:\s*\[(.*)\]\s*$', fm, re.MULTILINE)
    if not m:
        return None
    return re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))


def _replace_list_line(fm: str, key: str, items: list[str] | None) -> str:
    """key 라인을 교체. items 가 빈 리스트면 라인 자체를 제거."""
    if items:
        return re.sub(rf'^{key}:\s*\[.*\]\s*$', f"{key}: {_yaml_list(items)}", fm, count=1, flags=re.MULTILINE)
    return re.sub(rf'^{key}:\s*\[.*\]\s*\n', "", fm, count=1, flags=re.MULTILINE)


def _body_word_count(after_fm: str) -> int:
    """front matter 이후 본문에서 footer(마지막 '---' 구분선 이후) 제외 단어 수."""
    body = after_fm.split("\n---\n")[0]
    return len(body.split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fig_tags: set[str] = set()
    for fig in json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["figures"]:
        fig_tags.update(fig["tags"])
    allowed_extra = fig_tags  # 인물 태그는 전부 허용

    changed = 0
    deleted: list[str] = []
    tag_drop_total = 0
    for md in sorted(POSTS.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        _, fm, after = parts

        # 1) 구제 불가 thin post 삭제
        words = _body_word_count(after)
        if words < MIN_BODY_WORDS:
            deleted.append(f"{md.name} ({words}단어)")
            if not args.dry_run:
                md.unlink()
            continue

        new_fm = fm

        tags = _parse_list_line(fm, "tags")
        if tags is not None:
            new_tags = normalize_tags(tags, allowed_extra=allowed_extra)
            if not new_tags:  # 안전망: 태그가 전멸하면 유형 태그라도 유지
                new_tags = [t for t in tags if t in TYPE_TAGS] or ["다이제스트"]
            tag_drop_total += len(tags) - len(new_tags)
            if new_tags != tags:
                new_fm = _replace_list_line(new_fm, "tags", new_tags)

        sectors = _parse_list_line(fm, "sectors")
        if sectors is not None:
            new_sectors = normalize_sectors(sectors)
            if new_sectors != sectors:
                new_fm = _replace_list_line(new_fm, "sectors", new_sectors)

        assets = _parse_list_line(fm, "asset")
        if assets is not None:
            new_assets = normalize_assets(assets)
            if new_assets != assets:
                new_fm = _replace_list_line(new_fm, "asset", new_assets)

        if new_fm != fm:
            changed += 1
            if not args.dry_run:
                md.write_text("---" + new_fm + "---" + after, encoding="utf-8")

    mode = "DRY-RUN" if args.dry_run else "적용"
    print(f"[{mode}] front matter 정규화: {changed}건 / 태그 {tag_drop_total}개 정리")
    print(f"[{mode}] thin post 삭제: {len(deleted)}건")
    for d in deleted:
        print(f"  - {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
