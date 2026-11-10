# 사내 실행·개발 에이전트 가이드

이 파일은 이 저장소에서 작업하는 에이전트의 시작점이다. 사용자의 현재 요청과 실제 사내 배포 설정을 우선하고, 아래 절차를 작업 범위에 맞게 수행한다. **모든 셸 예시는 Linux/Bash 기준이며 `compose.yaml`이 있는 저장소 루트에서 시작한다.** clone한 폴더 안에 `quality_agent/`를 다시 만들지 않는다.

## 1. 목적과 현재 구현

- 기존 **Open WebUI + 사내 vLLM/LLM 프록시 + 품질 API Tools + DRM/EDM 읽기 skill**에 케이스·근거 검토·결과·보고서·절차·지식 관리를 추가한다. 반도체 Test 전용 제품으로 바꾸지 않는다.
- 기존 Open WebUI는 유지하고, 별도 **품질 API + 보고서 worker + PostgreSQL + 산출물 저장소**를 배포한다. API와 UI는 같은 서비스다. API 서버가 vLLM이나 기존 원천 Tools를 대신 실행하지 않는다.
- 관리 대상 원천 호출은 현재 **read만 지원**한다. 절차는 사람이 따르는 `guided` 방식이다. 자동 절차 실행, 원천 쓰기, 정량 판정, 미제공 사내 API 연결을 구현 완료로 취급하지 않는다.
- Action/Tools 등록만으로 기존 모든 Tool이 추적되지 않는다. 기존 Tool의 실제 호출 위치와 신뢰된 dispatcher를 연결해야 한다.

먼저 읽을 문서:

1. [README.md](README.md): 프로젝트와 시작 명령.
2. [구현 상태](docs/implementation_status.md): 제공 기능·미구현 경계·과거 검증 범위.
3. [배포와 복구](docs/deployment.md): 실제 실행 절차와 CDEP 대응.
4. [Open WebUI 연결](openwebui/README.md): bridge·Action·Tools·실행 기록 연결.
5. 업무 범위 변경 시 [최종 PRD/FRD](prd_frd_quality.md), [계약](docs/quality_agent_contracts.md), [검증 요구사항](docs/quality_agent_validation.md).

실제 요청 DTO·경로는 `api/quality_api/routes/`, `main.py` 및 [OpenAPI 명세](docs/openapi.json)를 확인한다. 설계 문서에는 후속 요구사항도 있다. 코드와 문서가 다르면 차이를 기록하고, 접근권한·출처·사람의 승인 규칙을 느슨하게 만들어 맞추지 않는다.

## 2. 작업 시작과 환경 확인

```bash
pwd
git status --short --branch
python3 --version
docker version
docker compose version
```

Docker가 없는 개발 환경에서도 문서 검토·Python 테스트·연결 코드 수정은 진행할 수 있다. Docker 없는 환경의 테스트를 이미지 빌드나 CDEP 검증으로 보고하지 않는다. 기존 수정 사항과 현재 배포 리소스를 먼저 확인한다.

사용자 요청에서 **개발 검증 / 신규 스테이징 / 기존 서비스 업데이트 / 복구** 중 무엇인지 파악한다. 이미 제공된 값과 승인 범위는 다시 묻지 않는다. 아래 정보는 파일·런타임 설정에서 먼저 확인하고, 없는 필수 입력만 묶어서 질문한다. 답변이 필요한 연결 작업을 기다리는 동안 독립적인 코드·테스트 작업을 진행한다.

| 확인할 정보 | 판단에 사용하는 위치 |
|---|---|
| 배포 방식·대상 서비스·기존 데이터 유무 | Docker Compose 호스트 또는 CDEP 배포 명세. 개발/운영 DB·볼륨·프로젝트 이름 구분 |
| 승인된 이미지·패키지 미러·CA | Python 3.12 기반 이미지, PostgreSQL 16 기준, 사내 PyPI/npm/브라우저 공급 경로 |
| 현재 Open WebUI 코드·버전 | 실제 Dockerfile, 이미지 digest, 시작·백업 스크립트, integration/branding, 설치된 플러그인 |
| 서버 간 주소와 브라우저 주소 | `QUALITY_API_BASE_URL`, `QUALITY_UI_BASE_URL`, ingress/TLS/프록시 |
| DB와 영속 저장 | `QUALITY_DATABASE_URL`, API/worker 공용 artifacts 볼륨, Open WebUI 전용 outbox 위치 |
| 사용자와 팀 | Open WebUI 실제 user ID, `QUALITY_INSTANCE_ID`, 팀·큐레이터 매핑 |
| 첫 연결 Tool | 실제 함수·인증·입출력·read 효과·버전, dispatcher의 호출 식별자와 수명 |

주소·Tool 함수·사용자 ID·이미지 digest를 만들어 넣지 않는다. 비밀값은 배포 secret/환경변수에서 사용하며 채팅으로 요청하지 않는다. 필요한 변수의 **설정 여부**를 확인하고 값은 출력하지 않는다. `env`, `printenv`, `cat .env`, 전체 `docker inspect`, 값이 확장된 `docker compose config` 출력은 진단 보고에 사용하지 않는다. Compose 검증에는 `docker compose config --quiet`를 사용한다.

## 3. 파일별 수정 위치

| 대상 | 위치 / 주의점 |
|---|---|
| API 설정·인증·저장 모델 | `api/quality_api/{config,auth,db,models,common,main}.py` |
| 케이스·실행·근거·결과 | `api/quality_api/routes/core.py` |
| 보고서와 worker 처리 | `api/quality_api/routes/reports.py`, `cli.py` |
| 절차·지식·팀 공유 | `api/quality_api/routes/assets.py` |
| 한국어 검토 UI | `api/quality_api/static/`; 운영 Node 빌드·CDN 불필요 |
| DB migration | `api/migrations/versions/`; 기배포 migration을 고치는 대신 새 revision 추가. `main.py`의 `SCHEMA_VERSION`도 새 Alembic head와 맞추기: readiness가 정확히 비교함 |
| 기존 Open WebUI용 패키지·플러그인 | `openwebui/quality_bridge/`, `openwebui/plugins/` |
| 실제 Tool 연결 예제 | `openwebui/examples/`; 예제를 연결 완료 코드로 오인하지 않기 |
| 신규 서비스 이미지 | `api/Dockerfile`, **빌드 context는 `api/`** |
| Open WebUI add-on 이미지 | `openwebui/docker/Dockerfile.addon`, **빌드 context는 저장소 루트** |
| 배포·운영 | `compose.yaml`, `.env.example`, `scripts/`, `config/tool_registry.json` |

`openwebui/docker/dockerfile`은 사용자 제공 **참조 파일**이며 `FROM`과 일부 실제 파일이 빠져 있다. 이것을 그대로 신규 API 이미지로 빌드하지 않는다. 실제 사내 파일과 대조한 차이는 [참조 검토](docs/reference_review.md)를 갱신한다.

기존 Open WebUI의 SSO `mail`, 실제 부서 클레임 `deptname`, 부서 목록, LLM 주소, `NO_PROXY/no_proxy`, 시작 CMD, 백업, integration/branding을 보존한다. 실제 배포 값이 다르면 그 차이를 먼저 파악한다. 과거 설명만으로 현재 값을 덮어쓰지 않는다. 새 API 호스트의 proxy 예외는 기존 목록에 필요한 항목만 추가한다.

## 4. 개발 검증

Python 3.12 환경을 사용한다. Windows에서는 아래 Bash 절차를 실행할 Linux/WSL 환경을 사용한다. 운영 변수·비밀값을 주입한 셸과 테스트 셸을 구분한다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
env -u QUALITY_TEST_DATABASE_URL .venv/bin/python -m pytest -q
.venv/bin/ruff check api openwebui tests scripts
```

기존 `.venv`가 올바르면 재생성하지 않는다. 의존성 설치 실패 시 승인된 미러·CA를 확인하며 TLS 검증을 끄거나 고정 버전을 임의 변경하지 않는다. 기본 pytest는 임시 SQLite를 사용하고 PostgreSQL 동시성 검사는 skip한다.

**PostgreSQL 테스트는 지정한 DB의 애플리케이션 테이블과 Alembic 테이블을 삭제·재생성한다.** `QUALITY_TEST_DATABASE_URL`에는 따로 만든 폐기 가능한 `*_test` DB만 주입한다. 운영 DB URL을 복사하거나 이름만 바꿔 사용하지 않는다. 이름 접미사 검사는 실수 방지를 위한 최소 조건이다. 같은 테스트 DB에 여러 테스트 실행을 동시에 연결하지 않는다. 대상 DB를 확인한 테스트 프로세스에서만 다음을 실행한다.

```bash
# QUALITY_TEST_DATABASE_URL을 테스트 프로세스에 별도 주입한 경우에만 실행
.venv/bin/python -m pytest -q
```

브라우저 검증은 [tests/browser/README.md](tests/browser/README.md)를 따른다. 임시 API/DB와 합성 자료를 사용하며 사내 SSO나 실제 Tool 연결 시험은 아니다. Node 20+와 승인된 Chromium이 필요하고 운영 배포에는 Node가 필요 없다. 코드 수정 범위에 맞는 테스트를 선택하고, 실행 결과·skip·미검증을 구분해 기록한다. 과거의 102개 통과 기록을 현재 환경에서 직접 실행한 결과로 쓰지 않는다.

## 5. 새 업무 서비스 실행 — Docker Compose

다음은 **대상 DB·볼륨이 확인된 신규 설치** 절차다. 기존 설치 업데이트는 [배포 문서 §5](docs/deployment.md#5-업데이트)의 백업·중지·migration 순서를 사용한다.

1. 호스트에 기존 `quality-agent` 프로젝트가 있는지 확인한다. `compose.yaml` 기본 이름이 같으면 같은 서비스를 대상으로 한다. 스테이징/운영이 같은 호스트면 서로 다른 프로젝트 이름·포트·env 파일을 정하고 모든 명령에 일관되게 적용한다.
2. `.env`가 없고 CDEP secret 주입도 준비되지 않은 새 설치에서만 `python3 scripts/generate_env.py`로 생성한다. 기존 파일을 덮어쓰거나 키·epoch를 단순 재기동 때문에 바꾸지 않는다.
3. 실제 이미지·팀·registry·DB·볼륨 설정을 적용한 뒤 아래 순서로 실행한다.

```bash
docker compose config --quiet
docker compose build
docker compose up -d db
docker compose --profile ops run --rm migrate
docker compose up -d quality-api quality-worker
docker compose ps
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/health/ready
```

명령 하나가 실패하면 원인을 확인한 뒤 다음 단계로 진행한다. 포트를 바꿨으면 health URL도 바꾼다. `QUALITY_IMAGE`는 빌드할 때 태그를 사용하고, 검증된 배포 이미지는 digest로 고정한다. 이미 만들어진 digest 이미지를 사용하는 경우 build 대신 배포 문서의 pull 절차를 따른다.

운영 API는 `postgresql+psycopg://` URL과 서로 다른 32바이트 이상 JWT 키 4개, 비어 있지 않은 epoch를 요구한다. API/worker는 같은 이미지와 DB, **실제로 같은 artifacts 볼륨**을 사용하며 UID/GID 10001에 쓰기 권한이 있어야 한다. 경로명만 같은 별개 볼륨으로 구성하지 않는다. PostgreSQL 데이터 볼륨은 API의 파일 볼륨과 별개다.

Compose는 `.env` 값을 컨테이너에 전달하지만, **호스트에서 직접 실행하는 `Settings()`는 `.env`를 자동으로 읽지 않는다.** 호스트 실행이 필요하면 적절한 환경변수를 프로세스에 주입하고 `PYTHONPATH=api` 또는 모듈 작업 경로를 지정한다. 생성된 DB 호스트 이름 `db`는 Compose 네트워크용이며 호스트에서 그대로 접속할 수 있다고 가정하지 않는다. Uvicorn 진입점은 `quality_api.main:create_app --factory`이고 `quality_api.main:app`이 아니다. `.env` 생성만으로 호스트 `uvicorn` 설정이 끝났다고 가정하지 않는다.

## 6. CDEP에서 서비스별로 실행하는 경우

Docker Compose를 사용할 수 없으면 같은 구성을 CDEP 명세로 옮긴다. CDEP의 실제 서비스 생성·secret·volume 기능을 확인하고 다음을 연결한다.

| 구성 | 이미지 / 실행 |
|---|---|
| 품질 API | `api/Dockerfile` 이미지 기본 CMD. 컨테이너 8000, ready `/health/ready` |
| 보고서 worker | 같은 이미지, `python -m quality_api.cli worker` |
| 초기/업데이트 migration | 같은 이미지, `python -m quality_api.cli migrate`를 API/worker보다 먼저 실행 |
| PostgreSQL | 관리 DB 또는 별도 서비스, 실제 URL 주입 |
| artifacts | API와 worker가 공유하는 영속 파일시스템, `/var/lib/quality/artifacts` |
| 환경변수와 secret | `compose.yaml`의 `x-api-environment`에 대응하는 실제 값들을 API/worker/migration에 주입 |
| Tool registry | `config/tool_registry.json`을 읽기 전용 설정/볼륨으로 `/app/config/tool_registry.json`에 제공하고 `QUALITY_TOOL_REGISTRY_PATH`에 그 경로 지정 |

Dockerfile만으로 DB·볼륨·ingress·secret이 만들어지지는 않는다. Tool registry도 API 이미지에 COPY되지 않으며 Compose의 `./config:/app/config:ro` 마운트가 제공하던 부분을 CDEP에서 연결해야 한다. 환경변수 `QUALITY_TOOL_REGISTRY_JSON`으로 직접 주입하는 방식을 선택했다면 파일 경로 변수는 제거하고 두 방식을 혼동하지 않는다. 현재 Compose는 `db` 서비스에 의존하므로 외부 관리 DB를 사용하면 해당 의존성과 배포 명세를 함께 조정한다. `scripts/backup.py`는 Compose `db`에 접속하는 구현이므로 관리 DB용 백업으로 그대로 사용하지 않는다.

## 7. 기존 Open WebUI 연결과 첫 Tool

1. 실제 운영 버전·이미지 digest·기존 파일을 확인한다. 현재 정상 동작하는 최종 QREW 이미지/빌드에 bridge 패키지만 추가하는 [설치 절차](openwebui/README.md#2-기존-docker-배포에-python-패키지-추가)를 적용한다. 기존 CMD와 SSO·백업·브랜딩을 유지한다.
2. API와 Open WebUI에 동일한 HUMAN/MODEL/TRACE 키·issuer·audience·epoch를 주입한다. **WORKER 키는 Open WebUI에 전달하지 않는다.** API와 브라우저가 접속할 주소를 구분한다. `QUALITY_API_BASE_URL`은 API origin/프록시 접두사까지, `QUALITY_UI_BASE_URL`은 브라우저에서 도달 가능한 HTTPS origin/접두사까지 설정한다.
3. Functions에 `plugins/quality_action.py`, Tools에 `plugins/quality_tools.py`를 등록·활성화하고 품질 모델에 연결한다. 위 경로는 `openwebui/` 기준이다. 파일을 이미지에 COPY하는 것만으로 플러그인이 등록되지는 않는다.
4. 사람의 Action으로 케이스를 만들고 검토 화면을 연다. 일회용 ticket으로 로그인하며 공개 관리자 토큰을 만들지 않는다. 만료 시 Action을 다시 실행한다. 운영 환경의 인증 문제를 해결하려고 development 모드나 demo-ticket을 켜지 않는다.
5. 먼저 수동 자료 → 근거 채택 → 결과 확정 → 보고서 다운로드를 확인한다. 이 단계는 실제 Tool 추적 검증과 별도로 보고한다.
6. 실제 읽기 Tool 하나의 ID/version/input_schema를 `config/tool_registry.json`에 등록한다. 초기 `[]`는 정상이며, wildcard 허용으로 우회하지 않는다. 원천 공유 정책이 확인되지 않았으면 `shareable=false`를 유지한다.
7. `examples/existing_tool_adapter.py`와 `dispatcher_lifecycle.py`를 실제 호출 위치에 연결한다. 신뢰된 `__user__`, 채팅·케이스, 저장된 request/invocation ID를 사용한다. **재전송은 같은 ID, 새 논리 호출은 새 ID**다. 모델 출력·현재 시각·인자 hash를 신뢰된 invocation ID로 대체하지 않는다. 병렬 호출의 context를 분리한다.
8. `begin_run → execute_read(claim → 원래 함수 → outcome) → close_run`을 확인한다. 관리 호출의 context나 claim이 없으면 원천 호출 전에 거절한다. `result_mapper`는 인증값을 제외한 유한 JSON을 반환하며 현재 bridge 한도는 500KB다. 큰 파일·stream은 실제 어댑터가 필요하다.
9. 정상/빈 결과/원천 오류/전송 실패/중복 요청을 확인한다. 같은 invocation의 재전송 시 **원천 실행 횟수가 늘지 않아야 한다**. outbox는 저장된 outcome만 전송한다. 결과 불명 상태를 성공으로 바꾸거나 원천을 자동 재실행하지 않는다.
10. 관리 모델에는 검증된 읽기 래퍼와 제안 Tools만 노출한다. 모델 제안은 사람이 채택하며, 기존 쓰기 Tool의 별도 노출로 관리 흐름을 우회하지 않게 확인한다.

신뢰된 dispatch ID 연결 위치를 찾지 못하면 해당 Tool 자동 추적을 미연결로 남기고 수동 경로 검증을 계속한다. 임의 Filter가 모든 실제 결과를 수집한다고 가정하지 않는다. DRM/EDM은 이미 있는 skill을 호출하며 접근 코드를 추측해 새로 만들지 않는다.

모델 출력 보완이 필요하면 `quality_bridge/drafting.py`에 **저장된 출처와 Tools가 비활성화된 모델 전용 callback**을 연결한다. 형식 수정은 같은 자료로 최대 1회이며 실패하면 수동 검토한다. 이것이 native 모델 루프를 자동 대체하거나 원천을 다시 읽게 만들지 않는다.

## 8. 오류 확인과 복구

| 증상 | 먼저 확인할 항목 |
|---|---|
| ready 503 | migration 버전·DB 연결·artifacts 권한·복구 모드. live 200만으로 준비 완료 처리하지 않기 |
| 보고서가 queued | worker 기동·동일 DB/이미지·recovery/draining 설정 |
| 보고서 파일 누락 | API/worker 실제 공유 볼륨·파일 해시·사용자 권한 |
| Action 401/403 | 역할별 키·issuer/audience·epoch·시각 동기화·주입 사용자·케이스/팀 권한. 키 값 출력 금지 |
| UI 링크 접속 실패 | 서버 내부 주소와 Windows 브라우저 주소 구분, HTTPS/프록시 접두사, ticket 만료 |
| Tool 실행 거절 | registry ID/version/입력·read 효과·실제 dispatch context·claim·활성 케이스 상태 |
| source import NOT_CONFIGURED | 미제공 파일/과거 대화 어댑터 경계. 실제 연결 또는 user_text/existing_ref 사용 |
| unknown run/call | 원천 실행 상태와 outbox를 대조한 뒤 명시적 정리. 자동 재실행 금지 |

업무 서비스 백업은 PostgreSQL dump와 artifacts가 한 세트다. 기존 Open WebUI S3 DB 백업은 새 업무 DB·파일·outbox를 자동 포함하지 않는다. 공유 workspace의 SQLite WAL 사용은 실제 파일시스템 지원 여부를 확인한다.

복구는 [배포 문서 §7](docs/deployment.md#7-기존-볼륨을-보존하는-복구-연습)을 따른다. `recover`는 일반 시작 명령이 아니며 새 epoch·복구 모드에서 수행하는 상태 변경이다. 기존 볼륨에 임의 덮어쓰기, `docker compose down -v`, 테스트로 운영 DB 초기화를 하지 않는다. 이미 지정된 작업 범위 내 수정·검증은 계속 수행하고, 대상이 불명확한 파괴적 작업은 필요한 정보를 먼저 확인한다.

## 9. 완료 보고와 저장소 관리

실제 수행한 단계에 따라 보고한다.

- **개발 검증:** 실행한 테스트·통과/skip·수정 파일. Docker/CDEP 미실행 여부.
- **서비스 기동:** 적용 이미지/버전, migration, API ready, worker, 영속 볼륨 확인 결과.
- **사용자 흐름:** Action 접속, 근거 채택, 결과 확정, 보고서 다운로드. 기존 SSO/모델/백업 확인 결과.
- **Tool 연결:** 실제 연결한 Tool과 정상·실패·중복 검증 결과. 미연결 항목과 필요한 다음 입력.

비밀값·원문 자료·JWT/ticket은 보고서와 커밋에 넣지 않는다. `.env`, DB, backups, runtime, `.venv`, `node_modules` 제외를 유지한다. 사내에서 생긴 설정·자료를 원격 저장소에 게시할 때는 해당 작업에 대한 사용자의 지시 범위를 따른다.

PRD/FRD는 `prd_frd_quality.md` 하나를 유지하고, 구현·배포가 달라지면 관련 README와 `docs/implementation_status.md`를 함께 갱신한다. 소스 변경 시 필요한 테스트를 실행한다. 문서만 바꾼 경우는 링크·경로·명령과 실제 코드의 일치를 확인하며 이전 테스트를 재실행했다고 쓰지 않는다.
