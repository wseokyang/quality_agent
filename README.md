# 기존 Open WebUI에 붙이는 품질 업무 서비스

기존 **CDEP Open WebUI + 사내 vLLM/LLM 프록시 + 품질 API Tools**를 유지하고, T-RAX에서 참고한 케이스·근거 검토·결과 확정·보고서·절차·지식 공유 기능을 별도 API 서비스로 추가하는 코드다. 반도체 Test 전용 판정이나 opencode 실행을 전제로 하지 않는다.

현재 Open WebUI 참조 Dockerfile은 `openwebui/docker/dockerfile`에 보존했다. 신규 API의 Dockerfile은 `api/Dockerfile`이다. 두 파일의 역할과 이미지를 구분한다. 기존 SSO `mail` 클레임, `deptname` 부서 제한, LLM 연결, 백업 래퍼, 브랜딩은 신규 서비스에서 변경하지 않는다.

## 바로 확인할 파일

| 파일 | 용도 |
|---|---|
| [prd_frd_quality.md](prd_frd_quality.md) | 제품 목표와 요구사항 |
| [docs/deployment.md](docs/deployment.md) | 실제 신규 서비스 실행·연결·백업·복구 절차 |
| [docs/reference_review.md](docs/reference_review.md) | 전달받은 과거 기록과 현재 Dockerfile의 차이 |
| [docs/implementation_status.md](docs/implementation_status.md) | 구현·검증 범위와 실제 사내 연결에 필요한 입력 |
| [docs/openapi.json](docs/openapi.json) | 현재 코드에서 생성한 업무 API 요청 명세 |
| [openwebui/README.md](openwebui/README.md) | 기존 Open WebUI에 bridge·Action·Tools를 설치하는 방법 |
| [compose.yaml](compose.yaml) | 새 API·보고서 worker·PostgreSQL 서비스 구성 |
| [config/tool_registry.json](config/tool_registry.json) | 추적 가능한 실제 읽기 Tool 목록. 초기값은 빈 배열 |
| [docs/quality_agent_implementation_guide.md](docs/quality_agent_implementation_guide.md) | 설계 배경과 단계별 구현 가이드 |

## 제공하는 기능과 경계

- 케이스 생성·조회·종료·재개·보관, 소유자 중심 케이스 권한과 팀 자산 공유, 변경 이력.
- 실행·Tool 호출 claim/outcome과 출처 기록, 중복 실행 차단, 미확인 호출의 사람 확인.
- 수동 자료 등록, 근거 제안·버전·채택/제외, 채택 근거로 확정 결과 생성.
- 확정 결과를 기준으로 보고서 작업·파일 저장·권한 확인 후 다운로드.
- 지식/절차 초안·승인·팀 공유 제안·큐레이션·버전·만료·내보내기.
- Open WebUI의 Action으로 케이스/검토 화면을 열고, 모델 Tools는 읽기·초안 제안만 수행.
- 저장된 출처만 사용하는 근거 초안 검증과 형식 수정 1회, 실패 시 수동 검토 전환용 helper.
- PostgreSQL 마이그레이션, 역할별 JWT, 일회용 UI ticket, 별도 worker, 복구 epoch, 오프라인 백업 스크립트.

다음 연결은 사내 환경에서 설정·검증해야 한다.

| 항목 | 현재 상태 |
|---|---|
| 이미 사용 중인 사내 품질 API·DRM/EDM skill | 실제 코드가 제공되지 않아 그대로 호출하는 래퍼 연결 지점과 예제를 제공한다. 가상의 사내 API를 구현하지 않았다. |
| 기존 Tool의 실행 기록 자동 수집 | 임의 Filter가 모든 결과를 받는다고 가정하지 않는다. 검증된 dispatcher의 호출 ID와 명시적 래퍼를 사용하며 실제 Open WebUI 버전에서 M0 검증이 필요하다. |
| 자료 읽기 실패·추적 결과 전송 실패 | 원천 호출 자동 재실행을 허용하지 않는다. outbox/미확인 상태로 보존하고 확인한다. |
| 자동 실행 가능한 절차 | 현재는 사람이 따르는 guided 절차다. 구문 검사를 통과했다고 실행 검증 완료로 표시하지 않는다. |
| 원천 시스템 변경·쓰기 Tool | 실행 기능을 제공하지 않는다. 기존 쓰기 Tool을 이 관리 흐름에 넣으려면 서버 강제 승인과 실제 승인 UI 검증이 먼저 필요하다. |
| 사내 LLM 품질 | 모델을 호출하지 않는 테스트로는 평가할 수 없다. 구조화 출력·Tool 선택은 실제 모델로 M0에서 확인한다. |
| CDEP 운영 배포 | 저장소 코드와 로컬 테스트 대상으로 구현했다. 운영 서버 접근·실제 배포·이미지 빌드·백업 복원은 수행하지 않았다. |

수동 등록 자료는 원천 Tool 실행으로 위장하지 않는다. 모델은 출처/권한/승인 상태를 생성하지 않으며, 제안과 확정은 서로 다른 API 권한으로 분리한다.

## 신규 서버 실행

Docker Engine과 Compose v2가 있는 서버에서 이 디렉터리를 작업 경로로 사용한다.

```bash
python3 scripts/generate_env.py
# .env에서 CDEP 승인 이미지와 실제 팀 설정을 적용한다.
docker compose build
docker compose up -d db
docker compose --profile ops run --rm migrate
docker compose up -d quality-api quality-worker
curl --fail http://127.0.0.1:18080/health/ready
```

`.env`는 비밀값을 생성하는 로컬 파일이며 Git/이미지/채팅에 첨부하지 않는다. 기본 API bind는 localhost다. CDEP ingress/TLS 설정, Open WebUI 서버의 실제 연결 주소, 브라우저 접근 주소를 [배포 문서](docs/deployment.md)에 따라 지정한다. 기존 Open WebUI와 API 서버에 필요한 서명 키와 epoch를 함께 주입해야 한다.

검토 화면은 Open WebUI Action이 발급하는 짧은 수명의 일회용 링크로 접속한다. 운영 환경에 공개 테스트 로그인이나 고정 관리자 토큰은 없다.

## 폴더 구조

```text
quality_agent/
├── api/                       # FastAPI·DB 모델·마이그레이션·정적 검토 화면
│   ├── Dockerfile
│   ├── quality_api/
│   ├── migrations/
│   └── requirements.txt
├── openwebui/                 # 기존 서버용 bridge·Tools·Action·add-on 예제
│   └── docker/dockerfile      # 사용자가 제공한 참조, 변경하지 않음
├── config/tool_registry.json  # 실제 등록 Tool 목록
├── scripts/                   # 비밀 env 생성·오프라인 백업·검증
├── tests/                     # 애플리케이션/연동 검증
├── compose.yaml
└── docs/                      # 설계와 운영 설명
```

## 검증과 도입 순서

애플리케이션 테스트는 이 디렉터리에 개발 의존성을 설치한 뒤 `pytest`로 실행한다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
# 별도 *_test PostgreSQL DB에서 실제 잠금/동시성도 검증한다. 이 DB의 테스트 테이블은 초기화된다.
QUALITY_TEST_DATABASE_URL='postgresql+psycopg://테스트사용자:비밀번호@테스트호스트/quality_agent_test' \
  .venv/bin/python -m pytest -q
```

테스트 URL에 운영 DB를 넣지 않는다. PostgreSQL 동시성 검증은 실제 PostgreSQL URL이 없으면 skip으로 표시한다. 브라우저 자동 검증은 [tests/browser/README.md](tests/browser/README.md)를 따른다. Node/Playwright는 테스트용이며 운영 UI는 빌드 도구나 CDN 없이 제공된다.

운영 스크립트만 확인하려면 외부 패키지 없이 `python3 -m unittest discover -s scripts/tests -v`를 실행한다.

먼저 스테이징에서 실제 읽기 Tool 한 종류를 등록해 **케이스 생성 → 실행 기록 → 근거 검토 → 결과 확정 → 보고서 다운로드**를 완료한다. 이후 인증/응답 방식이 다른 Tool을 유형별로 검증하고 팀 공유 기능을 연다. 배포 문서의 미검증 항목이 통과하기 전에는 운영 연결이 검증되었다고 판단하지 않는다.
