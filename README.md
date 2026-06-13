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

```bash
# 수동 실행
./automation/run.sh digest      # 전 인물, 어제치 다이제스트 → commit → push
./automation/run.sh breaking    # 전 인물, 중요도 높은 속보만
# 특정 인물·날짜
uv run automation/sns_blog.py digest --date 2026-06-12 --only reich,fsc_kr
```

cron: 다이제스트 매일 09:00 KST, 속보 4시간마다 (`crontab -l` 로 확인).

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
  "handle": "핸들/URL/페이지ID", "tags": ["태그"], "no_breaking": true }
```
`no_breaking: true` 면 다이제스트만 (기관 보도자료 권장).

### Facebook 사용 시 (국내 정치인 등)
Graph API 로 **임의 공개 페이지**를 읽으려면 Meta 의 `Page Public Content Access`
권한이 필요하고, 이는 **앱 심사 + 비즈니스 인증**을 통과해야 한다. 승인 전에는
공개 페이지도 빈 응답이 정상이다. 승인된 앱 토큰을 `automation/.env` 에
`FACEBOOK_GRAPH_TOKEN=...` 로 넣으면 `facebook` 소스가 활성화된다(없으면 자동 skip).
