# ade-dedrm

> Adobe Digital Editions(ADE)의 Adept DRM이 걸린 EPUB·PDF를 CLI 한 줄로 처리하는 도구.
> `.acsm` fulfillment 티켓부터 DRM-free 파일까지 전 단계가 하나의 파이프라인에 들어있다.

**English**: [README.md](./README.md)

- [noDRM/DeDRM_tools](https://github.com/noDRM/DeDRM_tools)의 `ineptepub` /
  `ineptpdf` 를 정제해 DRM 해제 코어를 이식
- [acsm-calibre-plugin (DeACSM)](https://github.com/Leseratte10/acsm-calibre-plugin)
  의 libgourou 포팅을 정제해 ACSM fulfillment 경로를 이식
- Calibre 플러그인 불필요, 터미널·스크립트·배치 환경에서 바로 실행

## 주요 기능

| 작업 | 서브커맨드 | 결과 |
|---|---|---|
| 로컬 macOS ADE 활성화 + 사용자 키 가져오기 | `init` | `~/.config/ade-dedrm/` 에 상태 파일 + `adobekey.der` |
| ACSM fulfillment + Adept DRM 해제 (ACSM/EPUB/PDF 자동 판별) | `decrypt` | DRM-free EPUB/PDF |
| 변환된 EPUB/PDF 를 [Calibre Web](https://github.com/janeczku/calibre-web) 에 업로드 | `upload` | Calibre Web 서재에 새 책 |
| Calibre Web 접속 정보 저장 | `config setup` / `config set-calibre` / `config show` | `~/.config/ade-dedrm/.env` (권한 0600) |

## 설치

```bash
git clone https://github.com/yun-sangho/ade-dedrm.git
cd ade-dedrm
uv sync
```

Python 3.12가 필요하다. `uv`가 자동으로 설치·고정한다.

## 빠른 시작 (macOS)

이미 Adobe Digital Editions가 설치돼 활성화돼 있다면:

```bash
# 1. ADE 활성화 상태 + 사용자 키를 한 번에 가져옴
uv run ade-dedrm init

# 2. 구매한 .acsm 파일을 원샷으로 DRM-free 파일로 변환
uv run ade-dedrm decrypt ~/Downloads/book.acsm
# → ~/Downloads/book.epub (또는 book.pdf)
```

## 서브커맨드 상세

### `init` — ADE 활성화 상태 + 사용자 키 가져오기 (macOS 전용)

`~/Library/Application Support/Adobe/Digital Editions/activation.dat`와
macOS 키체인의 `DeviceKey` / `DeviceFingerprint`를 조합해
`~/.config/ade-dedrm/{devicesalt, device.xml, activation.xml, adobekey.der}`
를 한 번에 만든다.

```bash
uv run ade-dedrm init [--force] [-o PATH]
```

- **선행 조건**: macOS에 ADE가 설치돼 있고 Help → Authorize Computer 로 본인 계정 인증 완료
- 키체인 조회 시 macOS가 권한 프롬프트를 띄울 수 있음
- `--force`: 기존 `~/.config/ade-dedrm/` 및 `-o` 대상 파일을 덮어씀
- `-o / --key-output PATH`: `adobekey.der` 의 추가 사본을 해당 경로에
  복사한다. 원본은 항상 상태 디렉터리 안에 들어가므로 `decrypt` 는
  `-k` 없이도 키를 찾을 수 있다.
- **상태 디렉터리 위치**: `$ADE_DEDRM_HOME` 환경변수로 오버라이드 가능
  (기본값: `$XDG_CONFIG_HOME/ade-dedrm` 또는 `~/.config/ade-dedrm`)

### `decrypt` — `.acsm` 또는 Adept DRM EPUB/PDF → DRM-free 파일

```bash
uv run ade-dedrm decrypt INPUT [-k KEY.der] [-o OUTPUT] [--force]
```

입력 파일 종류(확장자 / 매직 바이트)를 자동 판별해 알맞은 경로로 실행한다:

- **`.acsm` fulfillment 티켓**: `.acsm` 을 파싱해 `operatorURL`을 추출하고,
  Adobe의 tree-hash + textbook RSA 서명으로 요청을 만들어 ACS4 서버에 POST.
  응답에서 암호화된 책을 받아 곧바로 메모리에서 복호해 DRM-free 파일만
  남긴다. 중간 DRM 파일은 디스크에 남지 않는다. 기본 출력은 서버가
  돌려준 형식에 따라 `<input_stem>.<epub|pdf>`.
  **선행 조건**: `init` 로 상태 디렉터리가 구성돼 있어야 함.
- **암호화된 EPUB (`PK...`)**: ZIP 엔트리별 AES-CBC 복호 + PKCS#7 패딩
  스트립 + zlib inflate → `encryption.xml` 의 Adept 조각 없이 ZIP 재구성.
  기본 출력: `<input>.nodrm.epub`.
- **암호화된 PDF (`%PDF-`)**: `/ADEPT_LICENSE` → RSA 언랩 → 객체별 AES
  복호 → `/Encrypt` 제거하고 재직렬화. 기본 출력: `<input>.nodrm.pdf`.

`-k / --key` 는 선택 사항이다. 우선순위: 명시적 `-k` → `<state_dir>/adobekey.der`
(`init` 이 심어둠) → 로컬 ADE 설치에서 즉석 추출.

```bash
# .acsm → DRM-free, 키는 상태 디렉터리에서 자동 조회
uv run ade-dedrm decrypt ~/Downloads/book.acsm

# 이미 받아둔 DRM EPUB, 명시적 키
uv run ade-dedrm decrypt -k ~/adobekey.der 암호화된책.epub

# 복호화 직후 Calibre Web 에 바로 업로드
uv run ade-dedrm decrypt ~/Downloads/book.acsm --upload
```

### `upload` — 변환된 파일을 Calibre Web 에 업로드

```bash
uv run ade-dedrm upload FILE
  [--calibre-url URL] [--calibre-username USER] [--calibre-password PASS]
  [--calibre-no-verify-tls] [--env-file PATH] [--delete-after-upload]
```

`--delete-after-upload` 는 **업로드 성공 후에만** 로컬 파일을 삭제한다.
업로드가 실패하면 파일은 그대로 남는다.

[Calibre Web](https://github.com/janeczku/calibre-web) 인스턴스에 로그인해
(서버가 요구하는 세션 바인딩 CSRF 토큰을 파싱한 뒤) `FILE` 을
`multipart/form-data` 로 `/upload` 에 POST 한다. 성공 시
`Uploaded <name> -> <base-url>/book/<id>` 를 출력한다.

`decrypt --upload` 플래그는 복호화 성공 직후 동일한 업로드 단계를 자동
실행한다. 업로드가 실패해도 이미 만들어진 복호화 파일은 그대로 남으므로
`upload` 로 재시도할 수 있다.

### `config` — Calibre Web 접속 정보 저장

```bash
uv run ade-dedrm config setup                         # 대화형 프롬프트
uv run ade-dedrm config set-calibre --url ... --username ...   # 스크립트용
uv run ade-dedrm config show                          # 현재 소스 전체 확인
```

세 커맨드 모두 **단일 파일** 하나만 읽고 쓴다 — `~/.config/ade-dedrm/.env`
(권한 `0o600`). 별도의 JSON 설정 파일은 없다.

- `config setup` 은 URL · username · password 를 하나씩 묻는다.
  기존 값이 있으면 `[default]` 로 표시되고, 엔터만 누르면 유지된다.
  password 는 `getpass` 로 받아 터미널에 찍히지 않는다.
- `config set-calibre` 는 같은 파일에 비-대화형으로 저장한다 (스크립트용).
  전달된 필드만 덮어쓰고, 파일의 다른 변수 / 주석은 그대로 유지된다.
- `config show` 는 현재 로더가 보고 있는 모든 소스
  (영구 `.env`, 프로세스 환경변수, 최종 해석된 값) 를 한꺼번에 보여준다.
  password 는 `***` 로 마스킹된다.

## Calibre Web 크리덴셜 해석 순서

`upload` / `decrypt --upload` 는 각 필드에 대해 아래 목록을 **위에서 아래로**
돌면서 처음 발견된 값을 사용한다:

1. CLI 플래그 (`--calibre-url`, `--calibre-username`, `--calibre-password`,
   `--calibre-verify-tls` / `--calibre-no-verify-tls`)
2. 프로세스 환경변수:
   - `ADE_DEDRM_CALIBRE_URL`
   - `ADE_DEDRM_CALIBRE_USERNAME`
   - `ADE_DEDRM_CALIBRE_PASSWORD`
   - `ADE_DEDRM_CALIBRE_VERIFY_TLS` (`false`/`0`/`no`/`off` → 검증 끔)
3. `.env` 파일. 아래 중 **처음으로 존재하는 파일 하나만** 로드한다:
   - `--env-file PATH` 로 명시된 파일
   - 현재 디렉터리의 `./.env`
   - `~/.config/ade-dedrm/.env` (영구 저장 위치, `config` 커맨드가 쓰는 곳)

위 단계를 모두 돌고도 미결인 필드가 있으면 어떤 필드가 비어있는지 명시한
에러로 종료된다. 변수명 예시는 [`.env.example`](./.env.example) 을 참고.

## 사용 예시

### 기본 워크플로우

```bash
# 초기 설정 (한 번만)
uv run ade-dedrm init
uv run ade-dedrm config setup        # 선택: Calibre Web 접속 정보 저장

# 이후 구매한 책은 전부 이 한 줄로
uv run ade-dedrm decrypt ~/Downloads/새로운책.acsm -o ~/Books/새로운책.epub
```

### 복호화 → Calibre Web 업로드 원샷

```bash
# 복호화 후 Calibre Web 에 업로드하고 로컬 파일은 삭제
uv run ade-dedrm decrypt ~/Downloads/새로운책.acsm --upload --delete-after-upload

# 이미 변환해 둔 파일 업로드
uv run ade-dedrm upload ~/Books/예전책.nodrm.epub
```

### 이미 다운로드된 DRM 파일 해제

`.acsm` 없이 암호화된 EPUB·PDF 파일만 가지고 있다면:

```bash
# 명시적 키
uv run ade-dedrm decrypt -k ~/adobekey.der 암호화된책.epub

# `init` 로 만들어둔 상태 디렉터리의 키를 자동 사용
uv run ade-dedrm decrypt 암호화된책.pdf
```

### 상태 디렉터리 위치 변경

테스트용 임시 환경 등:

```bash
export ADE_DEDRM_HOME=$(mktemp -d)
uv run ade-dedrm init
uv run ade-dedrm decrypt book.acsm
```

### 현재 크리덴셜 소스 확인

```bash
uv run ade-dedrm config show
# → 로더가 보고 있는 .env 파일, 프로세스 환경변수, 그리고 최종 해석된
#   effective 설정을 한꺼번에 출력 (password 는 *** 로 마스킹).
```

## 크로스플랫폼 현황

현재 코드 약 95%는 플랫폼 독립적이고, Linux/Windows에서도 동작한다. 단,
**초기 상태 파일을 얻는 방법**만 macOS에 한정돼 있다:

| 영역 | macOS | Linux | Windows |
|---|---|---|---|
| `decrypt` (DRM 해제 + ACSM fulfillment) | ✅ | ✅ | ✅ |
| 상태 bootstrap (`init`) | ✅ | ❌ | ❌ |
| 상태 bootstrap (`activate`) | 🗓 예정 | 🗓 예정 | 🗓 예정 |

즉 macOS 사용자가 한 번 `init` 로 만든 `~/.config/ade-dedrm/` 를
다른 OS 기기에 복사하면 거기서도 `decrypt` 가 그대로 동작한다.

**완전한 크로스플랫폼 지원 계획**: Tier 3 (`ade-dedrm activate --anonymous` /
`--adobe-id`) 로 ADE 설치 없이 Adobe 서버에 직접 디바이스를 등록하는 경로를
추가한다. 상세 계획: [`docs/tier3-activate-plan.md`](./docs/tier3-activate-plan.md)

## 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 입력 파일이 Adobe Adept DRM으로 보호되어 있지 않음 |
| 2 | 키 불일치 / 복호화 실패 |
| 3 | 입력·출력 파일 문제 (존재 안 함, 덮어쓰기 금지 등) |
| 4 | ACSM fulfillment 실패 (네트워크·서버 에러) |
| 5 | Calibre Web 업로드 실패 (네트워크·인증·CSRF·권한 등) |

## Troubleshooting

### `E_GOOGLE_DEVICE_LIMIT_REACHED` (Google Play Books)

Google 계정에 묶인 Play Books 디바이스 슬롯이 가득 찬 상태. 우리 CLI 문제가 아니라
Google 서버의 거부다. `play.google.com/books` → 설정 → 기기 관리 에서 사용하지 않는
기기를 deauthorize 하거나 Google 고객센터에 "Play Books device limit reset" 요청.

### `E_ADEPT_DISTRIBUTOR_AUTH`

우리가 이미 재시도 로직을 넣어두었지만 실패가 반복되면 `init` 를 다시 실행해
상태를 새로 만들거나, Adobe 계정 device limit 을 확인.

### `wrong key` / `decryption failed`

상태 디렉터리의 `adobekey.der` 가 현재 ADE 계정과 다른 활성화에서 나온 것.
같은 계정의 `activation.dat` 로 `init --force` 를 다시 실행해 상태를 새로 만든다.

### `%PDF-` 인데 `EBX_HANDLER` / `ADEPT_LICENSE` 단어가 안 보임

해당 PDF는 Adept DRM이 아니라 다른 보호 방식(Apple FairPlay, Amazon 등)이다.
이 도구 범위 밖.

## 테스트

```bash
uv run pytest tests/ -q
```

26개 케이스가 포함돼 있다:
- Adobe 트리 해시 + 서명 (DeACSM 원본과 바이트 단위 일치)
- pkcs12 언랩, state dir 경로 해석
- PDF patch 헬퍼 (backward reader, trailer parsing 등)
- PDF 파서 기본 단위, 합성 PDF의 "not DRM" 분기
- 합성 EPUB 라운드트립 (RSA 언랩 → AES-CBC → zlib inflate → ZIP 재구성)

실제 ACSM fulfillment과 실제 DRM 파일 복호화는 Adobe/Google 서버 접근이
필요해 CI 에서 자동 테스트할 수 없다 — 수동 스모크 테스트로 검증한다.

## 라이선스

**GPL v3**. 이 프로젝트는 DeDRM_tools 와 DeACSM 에서 코드를 포팅했으므로
카피레프트가 상속된다. 상세 저작권은 [`NOTICE`](./NOTICE) 참조.

## 법적 고지

이 도구는 **본인이 합법적으로 구매한** EPUB·PDF 의 개인 백업 또는 접근성 확보
목적으로만 사용해야 한다. 구매하지 않은 도서, 타인의 도서, 도서관 대출본 등에
대해 사용하는 것은 저작권법 및 기술적 보호조치 무력화 금지 조항(한국 저작권법
제104조의2 등) 위반이 될 수 있으며, 이로 인해 발생하는 모든 책임은 이용자에게 있다.

개발자는 본 소프트웨어의 사용으로 발생하는 어떤 결과에 대해서도 책임지지 않는다.
