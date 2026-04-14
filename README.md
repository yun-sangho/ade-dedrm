# ade-dedrm

Adobe Digital Editions(ADE)로 구매한 ePub의 Adept DRM을 해제하는 macOS용 CLI.
[noDRM/DeDRM_tools](https://github.com/noDRM/DeDRM_tools)의 Adobe Adept 관련 로직만
발췌·정제해서 Calibre 플러그인 없이 터미널에서 바로 쓸 수 있게 만든 도구다.

## 설치

```bash
uv sync
```

Python 3.12가 필요하다. `uv`가 자동으로 설치/고정한다.

## 사용법

### 1. Adobe 사용자 키 추출 (최초 1회)

macOS에 Adobe Digital Editions가 설치되어 있고, 해당 계정으로 활성화되어 있어야 한다.

```bash
uv run ade-dedrm extract-key -o ~/adobekey.der
```

`~/Library/Application Support/Adobe/Digital Editions/activation.dat`에서
RSA 개인키를 꺼내 `.der` 파일로 저장한다.

### 2. ePub 복호화

```bash
uv run ade-dedrm decrypt -k ~/adobekey.der ~/Downloads/book.epub
# -> ~/Downloads/book.nodrm.epub
```

출력 경로를 지정하려면 `-o`를 사용한다.

```bash
uv run ade-dedrm decrypt -k ~/adobekey.der ~/Downloads/book.epub -o ~/clean.epub
```

### 종료 코드

| 코드 | 의미 |
|---|---|
| 0 | 성공 |
| 1 | 입력 파일이 Adobe Adept로 보호되어 있지 않음 |
| 2 | 키 불일치 / 복호화 실패 |
| 3 | 입력·출력 파일 문제 (존재 안함, 이미 있음 등) |

## 라이선스

GPL v3. 이 프로젝트는 DeDRM_tools에서 코드를 포팅했으며,
상세 저작권 표기는 [`NOTICE`](./NOTICE)를 참고.

## 법적 고지

이 도구는 **본인이 합법적으로 구매한** ePub의 개인 백업·접근성 확보 목적으로만
사용해야 한다. 구매하지 않은 도서 또는 타인의 도서에 대해 사용하는 것은 저작권법
위반이 될 수 있으며, 이로 인해 발생하는 모든 책임은 이용자에게 있다.
