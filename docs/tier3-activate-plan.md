# Tier 3 구현 계획: 독립 ADE 활성화 (`ade-dedrm activate`)

> **Handoff 문서**: 이 계획은 별도 세션에서 작업 시작용으로 작성됐다.
> 실행 전에 아래 "작업 재개 체크리스트"부터 확인할 것.

---

## 작업 재개 체크리스트

이 계획을 다음 세션에서 실행할 때 먼저 확인할 것들:

- [ ] `git log` 으로 현재 브랜치가 이 문서 작성 시점(`6780e8f Initial release`) 이후 어디까지 와있는지 확인
- [ ] `uv sync && uv run pytest tests/ -q` 로 기존 26개 테스트가 여전히 통과하는지 확인
- [ ] `/Users/sangho/Downloads/DeACSM_0.0.16/` 가 여전히 존재하는지 확인 (포팅 참조 소스). 없으면 https://github.com/Leseratte10/acsm-calibre-plugin 에서 동일 버전 재확보
- [ ] 본인의 Adobe 계정이 device slot에 여유가 있는지 확인 (Tier 3 엔드투엔드 테스트에서 1개 슬롯 소비됨, 특히 Adobe ID 경로)
- [ ] 이 문서의 "사용자 결정 사항" 섹션이 여전히 유효한지 사용자에게 재확인 (시간 경과로 마음이 바뀔 수 있음)

---

## Context

`ade-dedrm`은 현재 fulfill/decrypt 코어는 완전히 크로스플랫폼이지만, **초기 상태 파일**(`devicesalt`, `device.xml`, `activation.xml`)을 얻는 방법이 macOS ADE 설치에 의존한다 (`src/ade_dedrm/adobe_import.py`). 그래서 다른 OS 사용자는 도구를 쓸 수가 없다.

이 단계의 목표는 ADE 설치 없이 Adobe 서버에 직접 디바이스를 등록해 상태 파일을 생성하는 `activate` 서브커맨드를 추가하는 것이다. 완성되면:

- **Linux/Windows/macOS 어디서든** `uv tool install ade-dedrm` 후 `ade-dedrm activate --anonymous`만으로 쓸 수 있다
- ADE 기존 사용자는 여전히 `init`로 기존 활성화를 재사용할 수 있다 (두 경로 공존)
- 이후 Tier 2(Windows 네이티브 import), Tier 1(export/import state) 확장 시 이 모듈이 공통 기반이 된다

참고 구현: DeACSM `/Users/sangho/Downloads/DeACSM_0.0.16/libadobeAccount.py` (927 LOC) + `libadobe.py` 일부. 이를 정제·포팅한다.

---

## 사용자 결정 사항 (계획 수립 시점)

1. **Scope**: anonymous + Adobe ID **둘 다 한 번에** 구현
2. **비밀번호 입력**: `getpass` 대화형 프롬프트만 지원 (TTY 필수). `--password-stdin`, 환경변수 경로는 도입하지 않음 (단순화 + 보안)
3. **원자성**: `.tmp` → `os.replace` 방식으로 **처음부터 원자적 쓰기** (중간 실패 시 기존 상태 보존)

---

## File Layout

### 신규 파일

| 경로 | 추정 LOC | 역할 |
|---|---|---|
| `src/ade_dedrm/adobe_versions.py` | ~60 | `AdeVersion` dataclass + 버전 테이블 + `ADE_2_0_1`, `ADE_3_0_1`, `ADE_4_0_3` 상수. `adobe_fulfill.py`와 공유 가능하게 분리 |
| `src/ade_dedrm/adobe_activate.py` | ~380 | 활성화 파이프라인 (Phase A/B/C/D), 공개 API `activate_anonymous` / `activate_adobe_id` |
| `tests/test_adobe_activate.py` | ~260 | Offline 유닛 테스트 12개 + 픽스처 |

### 수정할 파일

| 경로 | 변경 내용 |
|---|---|
| `src/ade_dedrm/adobe_state.py` | `encrypt_with_devicesalt()` 추가 (기존 `decrypt_with_device_key()`의 짝), `load_pkcs12_from_bytes(pkcs12, devicesalt)` 내부 헬퍼 추가 — 디스크에 쓰기 전 pkcs12 언랩 가능하게 |
| `src/ade_dedrm/cli.py` | `activate` 서브커맨드 + `_cmd_activate` 핸들러 추가, `EXIT_ACTIVATE_FAIL = 5` 추가 |
| `README.md` | `activate` 사용법 섹션, 수동 스모크 테스트 절차, 익명 활성화의 device slot 소비 고지 |

### 건드리지 않을 파일

- `adobe_sign.py` — `sign_node` 그대로 재사용 (`/Activate` 요청 서명)
- `adobe_http.py` — `post_adept`, `get_adept` 그대로 재사용
- `adobe_fulfill.py` — `_add_nonce_xml`이 여기 있음. 이번 단계에서는 건드리지 말고 `adobe_activate.py`에서 import해서 씀 (나중에 `adobe_xml.py`로 빼는 리팩터는 별도 단계)

---

## Public API

`src/ade_dedrm/adobe_activate.py`에서 두 개만 export:

```python
class ActivationError(Exception): ...

def activate_anonymous(
    state: DeviceState,
    *,
    version: AdeVersion = ADE_2_0_1,
    force: bool = False,
) -> None: ...

def activate_adobe_id(
    state: DeviceState,
    username: str,
    password: str,
    *,
    version: AdeVersion = ADE_2_0_1,
    force: bool = False,
) -> None: ...
```

둘 다 성공 시 void, 실패 시 `ActivationError`. 완료되면 `state.exists()` → `True`이고 기존 `fulfill`/`decrypt`/`process`가 변경 없이 동작한다.

내부적으로는 둘 다 같은 `_run_activation(state, version, credentials, force)` 오케스트레이터를 호출하며, `credentials`가 `None`이면 anonymous, `(user, pass)` 튜플이면 Adobe ID.

---

## CLI Interface

```
ade-dedrm activate --anonymous [--ade-version VER] [--force]
ade-dedrm activate --adobe-id EMAIL [--ade-version VER] [--force]
```

- `--anonymous` 와 `--adobe-id EMAIL` 는 mutually exclusive, 하나 필수 (`required=True` mutex group)
- `--adobe-id` 지정 시 TTY에서 `getpass.getpass("Adobe ID password: ")` 로 비밀번호 입력. TTY 아니면 `error: password requires an interactive terminal`로 거부
- `--ade-version` 는 `{2.0.1, 3.0.1, 4.0.3}` 선택지, 기본 `2.0.1` (ADE 2.0.1 build 78765, DeACSM 권장 기본값)
- `--force` 는 기존 state dir 존재 시 덮어쓸지 여부 (기존 `init`와 동일)

종료 코드:
- `0` 성공
- `3` IO 문제 (state already exists without --force, TTY 없음 등)
- `5` (신규) `ActivationError`: 네트워크 실패, 서버 에러, 자격증명 실패 등

---

## Implementation Plan (단계별)

### Phase 0 — `adobe_versions.py` 작성 (~60 LOC)

```python
@dataclass(frozen=True)
class AdeVersion:
    name: str            # "ADE 2.0.1"
    build_id: int        # 78765
    hobbes: str          # "9.3.58046"
    client_version: str  # "2.0.1.78765"
    client_os: str       # "Windows Vista"
    use_https: bool      # False for <ADE 4.0.3, True otherwise

ADE_2_0_1 = AdeVersion(..., build_id=78765, ..., use_https=False)
ADE_3_0_1 = AdeVersion(..., build_id=91394, ..., use_https=False)
ADE_4_0_3 = AdeVersion(..., build_id=123281, ..., use_https=True)

ACS_SERVER_HTTP  = "http://adeactivate.adobe.com/adept"
ACS_SERVER_HTTPS = "https://adeactivate.adobe.com/adept"

def acs_server(version: AdeVersion) -> str: ...
```

상수 값 출처: DeACSM `libadobe.py:63-105`.

### Phase 1 — `adobe_state.py` 확장 (~25 LOC)

추가할 함수:

```python
def encrypt_with_devicesalt(devicesalt: bytes, data: bytes) -> bytes:
    """PKCS#7 pad → random IV → AES-CBC → IV + ciphertext 반환.
    DeACSM libadobe.encrypt_with_device_key의 포팅."""

def load_pkcs12_from_bytes(pkcs12_bytes: bytes, devicesalt: bytes) -> bytes:
    """pkcs12 바이트를 언랩해서 private key의 PKCS#8 DER 반환.
    기존 load_pkcs12_private_key_der()의 내부 헬퍼로 리팩터
    (활성화 Phase D에서 디스크를 거치지 않고 쓰기 위해)."""
```

기존 `load_pkcs12_private_key_der(state)`는 파일에서 읽은 뒤 `load_pkcs12_from_bytes` 호출하는 얇은 래퍼로 전환. 공개 API 변경 없음.

유닛 테스트: `encrypt_with_devicesalt ↔ decrypt_with_device_key` round-trip for lengths {0, 1, 15, 16, 17, 100}.

### Phase 2 — `adobe_activate.py` Phase A: 로컬 상태 생성 (~70 LOC)

네트워크 없음.

```python
def _create_devicesalt() -> bytes:
    """secrets.token_bytes(16) 반환."""

def _make_random_serial() -> str:
    """SHA-1(20 random bytes) → 40-char lowercase hex.
    DeACSM libadobe.makeSerial(random=True) 포팅.
    NOTE: deterministic 경로(MAC/UID 기반)는 의도적으로 포팅 안 함 —
    플랫폼 차이와 보안 이슈 회피."""

def _make_fingerprint(serial: str, devicesalt: bytes) -> str:
    """base64(sha1(serial_bytes + devicesalt)).
    latin-1 인코딩 사용 (DeACSM 매칭)."""

def _build_device_xml(version: AdeVersion, devicesalt: bytes) -> tuple[str, str]:
    """device.xml 문자열과 serial을 함께 반환.
    serial은 activate request에서 바로 필요하므로 재파싱 회피.
    locale은 하드코딩 'en' (DeACSM 기본값 동등)."""
```

### Phase 3 — `adobe_activate.py` Phase B: 서비스 디스커버리 (~80 LOC)

Unsigned GET 두 번.

```python
def _parse_service_info(xml_bytes: bytes) -> dict[str, str]:
    """<*ServiceInfo>의 직계 자식 adept:* 요소들을 dict로."""

def _discover_services(version: AdeVersion) -> tuple[dict, str]:
    """
    1. GET {acs_server(version)}/ActivationServiceInfo
       → authURL, userInfoURL, activationURL, certificate(b64)
    2. GET {authURL}/AuthenticationServiceInfo
       → authentication certificate(b64)
    returns: (service_info_dict, auth_cert_b64)
    """

def _build_initial_activation_xml(service_info: dict, auth_cert_b64: str) -> etree._Element:
    """<activationInfo xmlns="http://ns.adobe.com/adept"> 루트에
    <adept:activationServiceInfo> 블록 추가한 lxml tree 반환.
    아직 디스크에 쓰지 않음 — 모든 Phase 끝난 뒤 원자적으로 write."""
```

### Phase 4 — `adobe_activate.py` Phase C: SignIn (~120 LOC)

anonymous와 Adobe ID 모두 경유. 차이는 `<adept:signInData>` 내용물뿐.

```python
def _generate_rsa_keypair() -> tuple[bytes, bytes]:
    """pycryptodome으로 1024-bit RSA 생성.
    (public_key_der, private_key_pkcs8_der) 반환."""

def _encrypt_login_credentials(
    devicesalt: bytes,
    username: str,
    password: str,
    auth_cert_b64: str,
) -> bytes:
    """
    Build: [devicesalt(16)] + [len_u(1)] + u_latin1 + [len_p(1)] + p_latin1

    1. base64-decode auth_cert_b64 → DER X.509
    2. cryptography.x509.load_der_x509_certificate → .public_key()
    3. SubjectPublicKeyInfo DER로 export
    4. Crypto.PublicKey.RSA.importKey → PKCS1_v1_5.new(key).encrypt(payload)

    Anonymous 경로: username="", password="" (empty strings)
    → payload는 [devicesalt][0][0] 형태

    CRITICAL: 비밀번호/유저명에 latin-1 외 문자 있으면 ActivationError 발생 —
    Adobe 서버가 2009년 기준 single-byte 인코딩 가정.
    """

def _build_signin_request(
    method: str,                     # "anonymous" or "AdobeID"
    encrypted_creds: bytes,          # _encrypt_login_credentials 결과
    public_auth_key_der: bytes,
    encrypted_private_auth_key: bytes,
    public_license_key_der: bytes,
    encrypted_private_license_key: bytes,
) -> str:
    """Unsigned <adept:signIn method="..."> XML 반환.
    문자열 템플릿 방식 (등록된 DeACSM 요소 순서 그대로 매칭).
    anonymous/AdobeID 모두 <signInData>는 존재 — DeACSM이 anonymous에서도
    empty creds encryption으로 필드를 채움."""

def _run_signin(
    service_info: dict,
    auth_cert_b64: str,
    devicesalt: bytes,
    credentials: tuple[str, str] | None,
) -> SignInReply:
    """
    1. RSA auth/license 키 쌍 생성
    2. 각 private key → encrypt_with_devicesalt
    3. _encrypt_login_credentials(...) — anonymous든 아니든 항상 호출
    4. _build_signin_request
    5. post_adept(authURL + "/SignInDirect", xml_str)
    6. <error> 응답 시 Adobe 에러 코드 매핑 후 ActivationError
    7. <credentials> 응답 파싱 → SignInReply dataclass 반환

    반환 필드: user_uuid, pkcs12_b64, license_cert_b64,
               private_license_key_b64, auth_cert_b64, username(optional), method(optional)
    """

def _map_adobe_error(reply_xml: str) -> str:
    """E_AUTH_FAILED CUS05051 → 'Invalid username or password'
       E_AUTH_FAILED LOGIN_FAILED → '2FA detected; disable 2FA on your Adobe account and retry'
       기타 → 'Adobe error: {data attribute}'"""
```

### Phase 5 — `adobe_activate.py` Phase D: 디바이스 활성화 (~90 LOC)

서명된 POST. 이제 sign_node가 pkcs12에서 언랩한 private key로 서명할 수 있음.

```python
def _build_activate_request(
    state_in_memory: dict,  # device 정보 dict
    signin_reply: SignInReply,
    version: AdeVersion,
) -> str:
    """<?xml?><adept:activate requestType="initial">... XML 반환.
    DeACSM libadobeAccount:674-745 포팅.
    CRITICAL: productName에 'ADOBE Digitial Editions' (Adobe의 오타 그대로).
    요소 순서 DeACSM과 정확히 일치시킬 것 — tree hash가 순서 민감함."""

def _run_activate(
    signin_reply: SignInReply,
    device_info: dict,
    version: AdeVersion,
    activation_url: str,
) -> etree._Element:
    """
    1. pkcs12 b64 decode → load_pkcs12_from_bytes(pkcs12_bytes, devicesalt) → private key DER
    2. _build_activate_request → 문자열
    3. etree.fromstring → sign_node(root, private_key_der) → <adept:signature> 추가
    4. post_adept(activation_url + "/Activate")
    5. <error> 체크 후 ActivationError
    6. <adept:activationToken> 요소 반환 (나중에 activation.xml에 append)
    """
```

### Phase 6 — `adobe_activate.py` 오케스트레이터 + 원자적 쓰기 (~40 LOC)

```python
def _run_activation(
    state: DeviceState,
    version: AdeVersion,
    credentials: tuple[str, str] | None,
    force: bool,
) -> None:
    # 0. state 검증
    if state.exists() and not force:
        raise ActivationError(f"state already exists at {state.root} (use --force)")
    state.ensure_dir()

    # Phase A — in memory
    devicesalt = _create_devicesalt()
    device_xml, serial = _build_device_xml(version, devicesalt)

    # Phase B — network, returns in-memory activation.xml tree
    service_info, auth_cert = _discover_services(version)
    activation_tree = _build_initial_activation_xml(service_info, auth_cert)

    # Phase C — network, appends <credentials> to tree
    signin_reply = _run_signin(service_info, auth_cert, devicesalt, credentials)
    _append_credentials_to_tree(activation_tree, signin_reply, credentials)

    # Phase D — network, appends <activationToken> to tree
    device_info = _collect_device_info(device_xml, signin_reply, devicesalt)
    activation_token = _run_activate(
        signin_reply, device_info, version, service_info["activationURL"]
    )
    activation_tree.getroot().append(activation_token)

    # 원자적 쓰기: .tmp → os.replace
    _atomic_write_state(state, devicesalt, device_xml, activation_tree)


def _atomic_write_state(
    state: DeviceState,
    devicesalt: bytes,
    device_xml: str,
    activation_tree: etree._ElementTree,
) -> None:
    tmp_devicesalt = state.devicesalt.with_suffix(".tmp")
    tmp_device = state.device_xml.with_suffix(".tmp")
    tmp_activation = state.activation_xml.with_suffix(".tmp")
    try:
        tmp_devicesalt.write_bytes(devicesalt)
        tmp_device.write_text(device_xml, encoding="utf-8")
        tmp_activation.write_bytes(
            b'<?xml version="1.0"?>\n' + etree.tostring(activation_tree, pretty_print=True)
        )
        # 모두 성공했으면 atomic replace
        os.replace(tmp_devicesalt, state.devicesalt)
        os.replace(tmp_device, state.device_xml)
        os.replace(tmp_activation, state.activation_xml)
    except Exception:
        for tmp in (tmp_devicesalt, tmp_device, tmp_activation):
            if tmp.exists():
                tmp.unlink()
        raise
```

### Phase 7 — `cli.py` 통합 (~60 LOC)

```python
# _build_parser() 안에 추가
act = sub.add_parser("activate", help="Register a fresh ADE device with Adobe servers.")
grp = act.add_mutually_exclusive_group(required=True)
grp.add_argument("--anonymous", action="store_true",
                 help="Anonymous activation — no Adobe ID required.")
grp.add_argument("--adobe-id", metavar="EMAIL",
                 help="Activate with an Adobe ID. Password will be prompted.")
act.add_argument("--ade-version", choices=["2.0.1", "3.0.1", "4.0.3"],
                 default="2.0.1",
                 help="Which ADE version to emulate (default: 2.0.1).")
act.add_argument("-f", "--force", action="store_true",
                 help="Overwrite existing ade-dedrm state.")


def _cmd_activate(args) -> int:
    from ade_dedrm.adobe_activate import (
        ActivationError, activate_anonymous, activate_adobe_id,
    )
    from ade_dedrm.adobe_versions import ADE_2_0_1, ADE_3_0_1, ADE_4_0_3
    from ade_dedrm.adobe_state import DeviceState, state_dir

    version_map = {"2.0.1": ADE_2_0_1, "3.0.1": ADE_3_0_1, "4.0.3": ADE_4_0_3}
    version = version_map[args.ade_version]
    state = DeviceState(root=state_dir())

    try:
        if args.anonymous:
            activate_anonymous(state, version=version, force=args.force)
            print(f"Anonymous activation saved to {state.root}")
        else:
            if not sys.stdin.isatty():
                print("error: password requires an interactive terminal", file=sys.stderr)
                return EXIT_IO
            import getpass
            password = getpass.getpass("Adobe ID password: ")
            activate_adobe_id(state, args.adobe_id, password, version=version, force=args.force)
            print(f"Adobe ID activation saved to {state.root}")
    except ActivationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ACTIVATE_FAIL

    return EXIT_OK
```

---

## Testing Strategy

### Offline 유닛 테스트 (`tests/test_adobe_activate.py`)

네트워크 없이 검증 가능한 것만:

1. **`_create_devicesalt`** — 16 bytes, 서로 다른 호출은 다른 값 반환
2. **`_make_random_serial`** — 40자 lowercase hex, 2회 호출 시 다름
3. **`_make_fingerprint`** — 고정된 `(serial, devicesalt)` → `hashlib.sha1` 직접 계산 결과와 일치
4. **`_build_device_xml`** — lxml로 파싱, `deviceType=standalone`, `deviceClass=Desktop`, fingerprint 길이, version 요소 개수 등 구조 검증
5. **`encrypt_with_devicesalt` ↔ `decrypt_with_device_key`** — round-trip, 길이 {0, 1, 15, 16, 17, 100}
6. **`_encrypt_login_credentials`** — 테스트용 1024-bit RSA 키 쌍 생성, `cryptography.x509.CertificateBuilder`로 self-signed cert 만든 뒤 공개키를 Adobe 인증서 자리로 사용. encrypt 후 private key로 decrypt → 페이로드 레이아웃 검증: `[16 salt][1 len][user][1 len][pass]`
7. **`_build_signin_request`** — `method="anonymous"` 와 `method="AdobeID"` 둘 다 파싱 후 `<adept:publicAuthKey>`, `<adept:encryptedPrivateAuthKey>`, `<adept:publicLicenseKey>`, `<adept:encryptedPrivateLicenseKey>` 존재 확인
8. **`_build_activate_request`** — 가짜 device/signin reply로 빌드, 파싱, 핵심 요소 검증 (`productName` 오타 포함)
9. **`_parse_service_info`** — 실제 Adobe 응답 샘플을 fixture로 저장 후 dict 키 검증
10. **`_run_signin` `<error>` 매핑** — `E_AUTH_FAILED CUS05051` 등 케이스마다 `ActivationError` 메시지 확인
11. **CLI argparse** — `activate --anonymous` 네임스페이스 필드, mutex 위반 시 SystemExit
12. **원자적 쓰기** — `_atomic_write_state` 중간에 예외 발생 시 기존 파일 보존 확인 (monkeypatch로 중간 실패 시뮬레이션)

### Manual smoke test (서버 필요)

README에 문서화:

```bash
# 임시 state dir에서 익명 활성화
export ADE_DEDRM_HOME=$(mktemp -d)
uv run ade-dedrm activate --anonymous
ls -la $ADE_DEDRM_HOME   # devicesalt, device.xml, activation.xml 세 파일

# 활성화 직후 실제 ACSM fulfill 확인
uv run ade-dedrm process ~/Downloads/sample.acsm -k <key.der>
```

Adobe ID 경로는 별도로 수동 테스트 (CI 불가).

### 회귀 테스트

기존 26개 pytest 케이스가 그대로 통과해야 함. 특히:
- `test_adobe_state.test_pkcs12_roundtrip_through_state` — `load_pkcs12_private_key_der` 리팩터 후에도 통과
- `test_adobe_sign.*` — 영향 없음 (재사용만 함)
- `test_smoke.*` — CLI help 파싱

---

## Risks / Gotchas

### 1. `E_AUTH_USER_ALREADY_REGISTERED` (Adobe 서버 상태)
Adobe의 `/Activate`는 **idempotent가 아님**. 응답을 받지 못한 상태에서 재시도하면 서버 쪽에 고아 등록이 남고, 사용자 계정의 device slot을 소비함. 로컬 코드로는 해결 불가 — README에 "activation이 실패하면 adobe.com에서 직접 orphan device를 제거하라"는 안내 필수.

### 2. 요소 순서 민감성
Adobe tree hash는 요소 순서에 민감. `_build_activate_request` / `_build_signin_request` 는 **문자열 템플릿**으로 작성 (etree.SubElement 순서 흔들림 방지). `adobe_fulfill.py:_build_fulfill_request`와 동일한 패턴.

### 3. latin-1 인코딩 강제
DeACSM은 사용자명/비밀번호를 latin-1로 인코딩. 한국어 비밀번호 등 non-latin-1 문자가 있으면 Adobe 서버가 거부함. `_encrypt_login_credentials`는 시작 시점에 validate 후 명확한 에러 메시지 발생.

### 4. locale 처리
Python 3.12에서 `locale.getdefaultlocale()` deprecated. **하드코딩 `"en"` 사용** — Adobe는 실제 locale 값을 신경쓰지 않고, 이 방식이 DeACSM의 fallback 동작과 동등.

### 5. HTTP vs HTTPS
ADE 4.0.3+ (build 123281+)는 HTTPS. 기본값 ADE 2.0.1은 HTTP. `adobe_http.py`의 lenient TLS (`CERT_NONE`) 설정이 양쪽 모두 처리. `_discover_services`, `_run_signin`, `_run_activate`가 같은 `version.use_https` 플래그를 일관되게 사용해야 함 — 안 그러면 activation.xml에 저장된 URL과 fulfill 시점 URL이 불일치.

### 6. `_add_nonce_xml` 공유
현재 `adobe_fulfill.py`에 있음. 활성화에서도 필요 (activate request에 nonce 포함). 이번 단계에서는 **`from ade_dedrm.adobe_fulfill import _add_nonce_xml`** 로 import만. 나중에 `adobe_xml.py` 등으로 분리하는 리팩터는 별도 커밋.

### 7. `save_activation`의 `etree._ElementTree` vs `_Element` 혼동
기존 `save_activation(state, tree)`는 `_ElementTree`를 받음. 활성화 오케스트레이터는 `_Element`로 작업하는 게 더 자연스러움 (루트에 계속 append). `_atomic_write_state` 안에서 `etree.tostring(root)`로 직접 직렬화하고 `save_activation`을 거치지 않도록 구성.

---

## 파일별 최종 작업 목록

| 파일 | 작업 | LOC |
|---|---|---|
| `src/ade_dedrm/adobe_versions.py` | 신규 | ~60 |
| `src/ade_dedrm/adobe_activate.py` | 신규 | ~380 |
| `src/ade_dedrm/adobe_state.py` | `encrypt_with_devicesalt` + `load_pkcs12_from_bytes` 추가 | +25 |
| `src/ade_dedrm/cli.py` | `activate` 서브커맨드 + `_cmd_activate` + `EXIT_ACTIVATE_FAIL` | +60 |
| `tests/test_adobe_activate.py` | 신규 | ~260 |
| `tests/test_adobe_state.py` | `encrypt_with_devicesalt` round-trip 테스트 추가 | +20 |
| `README.md` | `activate` 섹션, 스모크 테스트, orphan device 고지 | +30 |
| **합계** | | **~835** |

---

## 재사용 포인트

- `src/ade_dedrm/adobe_sign.py:sign_node` — `/Activate` 요청 서명
- `src/ade_dedrm/adobe_state.py:DeviceState` — 상태 디렉터리 관리
- `src/ade_dedrm/adobe_state.py:decrypt_with_device_key` — 활성화 response의 pkcs12 언랩에서 사용
- `src/ade_dedrm/adobe_http.py:post_adept, get_adept` — HTTP 레이어
- `src/ade_dedrm/adobe_fulfill.py:_add_nonce_xml` — activate request의 nonce (이번엔 import만)

---

## 원본 소스 참고

구현 중 line-by-line 대조할 참조 파일:

- `/Users/sangho/Downloads/DeACSM_0.0.16/libadobe.py`
  - 63-105: `VAR_VER_*` 상수
  - 156-253: `createDeviceKeyFile`, `makeSerial`, `makeFingerprint`, `get_mac_address`
  - 414-459: `encrypt_with_device_key`, `decrypt_with_device_key`

- `/Users/sangho/Downloads/DeACSM_0.0.16/libadobeAccount.py`
  - 46-111: `createDeviceFile`
  - 113-158: `getAuthMethodsAndCert`
  - 160-231: `createUser`
  - 233-267: `encryptLoginCredentials`
  - 270-334: `buildSignInRequest`, `buildSignInRequestForAnonAuthConvert`
  - 424-501: `signIn`
  - 674-745: `buildActivateReq`
  - 793-887: `activateDevice`

- `/Users/sangho/Downloads/DeACSM_0.0.16/register_ADE_account.py` — standalone entry point; DeACSM이 이 함수들을 어떤 순서로 호출하는지 확인용.

---

## Verification

### 단계별 체크

1. `uv run pytest tests/test_adobe_state.py -q` — 기존 통과 + 새 `encrypt_with_devicesalt` round-trip
2. `uv run pytest tests/test_adobe_activate.py -q` — 신규 12개 유닛 테스트 전부 통과
3. `uv run pytest tests/ -q` — 전체 38개 테스트 통과 (기존 26 + 신규 12)
4. `uv run ade-dedrm activate --help` — argparse 구조 정상
5. `uv run ade-dedrm activate --anonymous --ade-version 2.0.1` 와 `--adobe-id` 의 mutex 강제 여부 (둘 다 주면 SystemExit)

### End-to-end 수동 스모크 (서버 필요)

```bash
# clean slate
export ADE_DEDRM_HOME=$(mktemp -d)

# anonymous activation
uv run ade-dedrm activate --anonymous
ls -la $ADE_DEDRM_HOME
#   devicesalt       (16 B)
#   device.xml       (~500 B)
#   activation.xml   (~10 KB, credentials + activationToken 포함)

# 방금 활성화된 상태로 fulfill이 돌아가는지 — 이것이 진짜 검증
uv run ade-dedrm fulfill ~/Downloads/sample.acsm -o /tmp/test.drm.epub
#   성공하면 activate 경로가 완벽하게 동작

# Adobe ID 경로 (별도 계정으로 한 번만)
export ADE_DEDRM_HOME=$(mktemp -d)
uv run ade-dedrm activate --adobe-id test@example.com
#   getpass 프롬프트에 비밀번호 입력
#   성공 시 activation.xml에 <adept:username method="AdobeID">가 들어있어야 함
```

### 성공 기준

- 3개 상태 파일이 완전한 형태로 생성
- `fulfill` 서브커맨드가 해당 state로 실제 ACSM을 처리해서 암호화된 EPUB 다운로드 성공
- `decrypt`까지 통과해서 DRM-free 파일 얻음
- anonymous / Adobe ID 양쪽 경로 모두 독립적으로 검증
- 기존 macOS `init` 경로는 영향 없이 그대로 동작

### 실패 시 체크리스트

1. `_discover_services`에서 HTTP 404 — Adobe 서버 URL 변경 확인 (`VAR_ACS_SERVER_*` 값)
2. `/SignInDirect`가 `<error data="E_AUTH_*">` — 자격증명 오타 또는 2FA 활성화 계정
3. `/Activate`가 `<error data="E_ACT_DEVICE_LIMIT_REACHED">` — 계정의 6개 디바이스 슬롯 소진
4. fulfill 시점에서 `E_ADEPT_AUTHENTICATION_FAILED` — activate 요청의 tree hash 서명이 틀렸음 (요소 순서, namespace, productName 오타 체크)
5. `<adept:credentials>`의 pkcs12가 언랩 안 됨 — devicesalt를 SignIn 전에 누설했거나 잘못된 key를 사용

---

## 구현 순서 요약 (TL;DR)

```
1. adobe_versions.py                      (Phase 0, 독립 커밋 가능)
2. adobe_state.py 확장 + round-trip 테스트   (Phase 1, 회귀 안전)
3. adobe_activate.py Phase A (로컬)        (Phase 2, 오프라인 검증)
4. adobe_activate.py Phase B (service discovery)  (Phase 3, 서버 필요)
5. adobe_activate.py Phase C (SignIn)      (Phase 4, anonymous 먼저)
6. adobe_activate.py Phase D (Activate)    (Phase 5, 서명 + POST)
7. 오케스트레이터 + 원자적 쓰기              (Phase 6)
8. cli.py 통합                             (Phase 7, end-to-end 가능)
9. fulfill 회귀 확인 — "진짜 검증"          (Verification)
10. Adobe ID 경로 별도 수동 테스트
```

각 Phase 완료 시점마다 유닛 테스트 통과 + 필요 시 수동 스모크. Phase 9에서 기존 `fulfill`이 새 state로 성공하면 activation 로직 전체가 검증된다.
