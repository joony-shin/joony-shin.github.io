# joony-shin.github.io

Hugo + GitHub Pages 로 만든 자동 블로그.

- **테마**: [PaperMod](https://github.com/adityatelange/hugo-PaperMod) (git submodule)
- **배포**: `main` 브랜치 push → GitHub Actions(`.github/workflows/deploy.yml`) 자동 빌드·배포
- **URL**: https://joony-shin.github.io

## 로컬 미리보기
```bash
hugo server -D            # http://localhost:1313, 드래프트 포함
```

## 새 글 작성
```bash
hugo new content posts/내-글-제목.md
# draft: false 로 바꾸면 다음 배포에 포함됨
```

## AdSense
승인 후 `hugo.toml` 의 `params.googleAdsenseClient` 에 `ca-pub-...` 를 넣으면
`<head>` 에 광고 스크립트가 자동 주입됩니다. 또한 AdSense 가 요구하는
`ads.txt` 는 `static/ads.txt` 로 두면 사이트 루트로 서빙됩니다.

## 자동 글 생성 (SNS → 한국어 블로그)
`automation/sns_blog.py` 가 인사들의 SNS 게시물을 한국어로 요약·해설해 포스트를 만든다.
인물 목록은 `automation/sources.json`, cron 진입점은 `automation/run.sh`.

각 글에는 발언 요약·해설에 더해 **투자 관점**(`## 투자 관점`) 섹션이 붙는다 —
영향받는 자산·섹터·방향·시간축·신뢰도를 *정보로만* 제시하며(매수·매도 권유 금지),
모든 글 하단에 투자 유의 고지가 자동으로 들어간다. 추출된 섹터/자산은 front matter
의 `sectors` / `asset` taxonomy 로 저장되어 `/sectors/`, `/asset/` 에서 탐색된다.

### 품질 가드 (AdSense 저품질 대응)
- **통제 어휘**: 태그·섹터·자산은 `automation/taxonomy.py` 의 canonical 로만 매핑
  (LLM 자유 태그 금지 → 글 1개짜리 taxonomy 페이지 방지). 글 2개 미만 term
  페이지는 `noindex`.
- **발행 기준**: 속보는 중요도 5만·인물당 하루 2건, 본문 700자 미만 발행 안 함.
  다이제스트는 게시물 2건 이상·본문 1,000자 이상일 때만 발행.
- **중복 방지**: 속보로 단독 발행한 게시물은 다이제스트에서 제외.
- 기존 글 일괄 정규화는 `automation/normalize_posts.py` (one-off, --dry-run 지원).

```bash
# 수동 실행
./automation/run.sh digest      # 전 인물, 어제치 다이제스트 → commit → push
./automation/run.sh breaking    # 전 인물, 중요도 높은 속보만
# 특정 인물·날짜
uv run automation/sns_blog.py digest --date 2026-06-12 --only reich,fsc_kr
```

cron(GitHub Actions `publish.yml`): 다이제스트 매일 09:00 KST, 속보 매시, 적중 리뷰 매주 일요일 18:00 KST.

## 투자 관점 사후검증 (track record)
`automation/track_record.py` 가 지난 글의 `## 투자 관점` 예측을 발행 7일 뒤
실제 시세(야후 파이낸스 무인증 JSON, API 키 불필요)와 대조해 적중 여부를 채점하고,
한 편의 "적중 리뷰" 글로 묶어 발행한다. LLM 미사용(결정론적 채점). 중복 채점은
`automation/track_state.json` 으로 방지.

```bash
./automation/run.sh track                              # 성숙한 예측 채점 → 리뷰 글 발행
uv run automation/track_record.py --dry-run            # 글 안 쓰고 채점 결과만 확인
uv run automation/track_record.py --date 2026-06-19 --min 1
```

자산명→시세 심볼 매핑은 `track_record.py` 의 `ASSET_SYMBOLS` 에서 관리한다
(원유·금·달러·코스피·국채금리·반도체지수 등). 매핑 안 되는 자산은 채점에서 제외된다.

### 소스 어댑터
| source | 접근 | 비고 |
|---|---|---|
| `truthsocial` | 공개 JSON 아카이브 | 트럼프 전용 |
| `bluesky` | 공개 API(무인증) | 임의 핸들. 가장 쉬움 |
| `rss` | RSS/Atom XML | `handle` 에 피드 URL. 기관 보도자료·네이버블로그 등 |
| `facebook` | Graph API | **`FACEBOOK_GRAPH_TOKEN` 필요** (아래) |

### 인물 추가
`sources.json` 의 `figures` 에 한 줄 추가하면 끝:
```json
{ "key": "고유키", "name_ko": "표시이름", "title": "직함", "source": "bluesky",
  "handle": "핸들/URL/페이지ID", "category": "commentary", "tags": ["태그"], "no_breaking": true }
```
`no_breaking: true` 면 다이제스트만 (기관 보도자료 권장).
`category` 는 분류용 메타(`politics`/`centralbank`/`regulator`/`commentary`/`business`).

### Facebook 사용 시 (국내 정치인 등)
Graph API 로 **임의 공개 페이지**를 읽으려면 Meta 의 `Page Public Content Access`
권한이 필요하고, 이는 **앱 심사 + 비즈니스 인증**을 통과해야 한다. 승인 전에는
공개 페이지도 빈 응답이 정상이다. 승인된 앱 토큰을 `automation/.env` 에
`FACEBOOK_GRAPH_TOKEN=...` 로 넣으면 `facebook` 소스가 활성화된다(없으면 자동 skip).
