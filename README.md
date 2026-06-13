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

## 자동 글 생성 (다음 단계)
`scripts/` 에 글 생성 스크립트를 추가하고, 별도 GitHub Actions(cron)에서
`hugo new` → commit → push 하면 `deploy.yml` 이 이어받아 배포합니다.
