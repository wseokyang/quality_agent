# 품질 업무 서비스 배포 및 복구

## 1. 배치와 운영 책임

기존 CDEP Open WebUI 서버의 Dockerfile·SSO·LLM·브랜딩·백업 경로를 유지한다. 추가 서버에는 이 폴더의 `compose.yaml` 또는 같은 구성을 옮긴 CDEP 서비스를 배포한다.

```text
사용자 ── 기존 Open WebUI 서버 (vLLM/API Tools 유지)
              │ 서버 간 JWT 인증
              ▼
          품질 API 서버 ── PostgreSQL
              │          품질 전용 DB 볼륨
              ├─ 품질 산출물 볼륨
              └─ 보고서 worker (같은 코드/이미지, 다른 명령)
사용자 브라우저 ── 품질 API의 HTTPS /ui/index.html (Action의 일회용 ticket)
```

품질 API는 vLLM을 새로 서빙하지 않으며 원천 시스템의 쓰기 API도 실행하지 않는다. 기존 품질 Tool의 읽기 실행은 Open WebUI의 명시적 래퍼에서 수행한다. 근거 기록 실패 시 원천 API를 임의 재호출하지 않는다.

별도 서비스는 반드시 별도 물리 서버일 필요는 없다. 여기서는 사용자가 허용한 별도 API 서버 구성을 기본으로 한다. 기존 Open WebUI를 이 Compose에 복제하지 않는다.

## 2. 사전 입력

운영자가 확인할 값은 다음이다. 이 저장소는 실제 내부 주소, 원천 API 스키마, SSO 자격증명을 만들어 넣지 않는다.

| 입력 | 설정 위치 |
|---|---|
| CDEP 승인 Python/PostgreSQL 이미지와 digest | `QUALITY_PYTHON_BASE_IMAGE`, `QUALITY_POSTGRES_IMAGE` |
| 이미지 빌드 태그 / 배포 digest | `QUALITY_IMAGE`; 빌드할 때는 고유 태그, 검증한 이미지를 운영에 올릴 때는 digest 사용 |
| 품질 API의 서버 간 HTTPS 주소 | 기존 Open WebUI `QUALITY_API_BASE_URL` |
| 사용자의 브라우저에서 접속할 HTTPS 주소 | 기존 Open WebUI `QUALITY_UI_BASE_URL` |
| Open WebUI 설치별 안정적인 ID | 기존 Open WebUI `QUALITY_INSTANCE_ID` |
| 사용자 ID와 팀 연결, 큐레이터 ID | API `QUALITY_USER_TEAMS_JSON`, `QUALITY_CURATOR_IDS_JSON` |
| M0에서 확인한 읽기 Tool ID·버전·입력 JSON Schema | `config/tool_registry.json` |
| 영속 저장소 및 백업 대상 | PostgreSQL·artifacts 볼륨, Open WebUI trace outbox 경로 |

`config/tool_registry.json`은 의도적으로 빈 배열이다. 임의 API를 허용하지 않는 초기값이며, 실제 Tool을 래핑하고 ID/version/input_schema를 정확히 등록해야 추적 실행이 가능하다. `shareable`은 실제 원천 공유 정책을 확인한 경우에만 true로 지정한다. 쓰기 Tool은 등록해 실행하는 기능을 제공하지 않는다.

팀 권한은 SSO의 부서 로그인 허용과 별개다. Open WebUI에서 인증된 사용자 ID를 기준으로 명시적으로 팀을 매핑해야 하며, 모든 로그인 사용자를 한 팀으로 자동 등록하지 않는다.

## 3. 최초 실행

아래 명령의 작업 경로는 `quality_agent`다. Docker Engine과 Compose v2가 필요하다. 현 개발 환경에는 Docker CLI가 없어 이미지 빌드·Compose 실제 기동·볼륨 복원을 실행 검증하지 못했다. 배포 전 사내 스테이징에서 확인한다.

```bash
python3 scripts/generate_env.py
# .env의 이미지 참조를 승인된 이미지로 수정하고 Tool/팀 설정을 작성한다.
docker compose build
docker compose up -d db
docker compose --profile ops run --rm migrate
docker compose up -d quality-api quality-worker
curl --fail http://127.0.0.1:18080/health/live
curl --fail http://127.0.0.1:18080/health/ready
```

위 명령은 소스에서 이미지를 빌드하는 초기 경로다. `QUALITY_IMAGE`에는 이때 `quality-agent:<고유버전>`처럼 **태그**를 넣는다. 레지스트리에 게시하고 검증이 끝난 이미지는 `QUALITY_IMAGE=사내레지스트리/품질이미지@sha256:...`로 고정하고 `docker compose --profile ops pull`로 받는다. digest를 빌드 결과의 태그로 사용할 수 없으므로 이 배포 경로에서는 `docker compose build`를 다시 실행하지 않는다.

생성 스크립트는 네 역할의 서명 키, DB 비밀번호, 복구 epoch를 서로 독립적으로 생성하며 파일 권한을 0600으로 지정한다. 기존 파일을 덮어쓰지 않고 비밀값을 출력하지 않는다. `.env`와 백업은 Git 및 Docker 빌드 컨텍스트에서 제외한다. CDEP에서는 이 값을 비밀 환경변수/secret manager로 주입하는 것이 우선이다.

초기 `migrate`는 명시적으로 실행한다. API 시작마다 자동으로 스키마를 바꾸지 않는다. PostgreSQL과 산출물 볼륨은 재배포해도 보존되어야 한다. `docker compose down -v`는 이 데이터를 삭제하므로 일반 재배포 절차에 포함하지 않는다.

Python 이미지는 Python 3.12, pip, groupadd/useradd를 지원해야 한다. 사내 이미지에서 이 조건이 다르면 계정 생성 부분을 해당 이미지에 맞춰 수정한다. 패키지 설치는 고정된 `api/requirements.txt`를 사용하며 사내 PyPI 미러/CA는 CDEP의 표준 빌드 설정을 적용한다. 인증서 검증을 끄는 우회 설정은 넣지 않는다.

`127.0.0.1:18080`이 기본 노출 주소다. 같은 호스트의 TLS reverse proxy가 여기에 연결하는 구성을 가정한다. CDEP가 컨테이너 포트를 직접 라우팅하면 `QUALITY_BIND_HOST`와 외부 포트·방화벽을 해당 환경에 맞게 지정한다. PostgreSQL 포트는 외부에 게시하지 않는다. API와 DB가 다른 서버라면 사내 PostgreSQL의 TLS/접근 정책과 URL을 적용하고 Compose db 의존성을 해당 배포 명세에서 제거한다.

Compose 대신 CDEP에서 서비스별로 배포할 때도 **새 API 이미지 하나**를 아래처럼 사용한다.

| CDEP 항목 | 이미지/명령 및 연결 |
|---|---|
| 품질 API | `api/Dockerfile`로 만든 이미지의 기본 CMD, 컨테이너 포트 8000, `/health/ready` 준비 확인 |
| 보고서 worker | 같은 이미지, 시작 명령 `python -m quality_api.cli worker` |
| 마이그레이션 작업 | 같은 이미지, 일회성 명령 `python -m quality_api.cli migrate`; API/worker 기동 전 실행 |
| PostgreSQL | CDEP 관리 DB 또는 별도 DB 서비스, `QUALITY_DATABASE_URL`로 연결 |
| 산출물 저장소 | API와 worker에 **동일한 영속 파일시스템**을 `/var/lib/quality/artifacts`로 마운트 |

서로 다른 컨테이너에 같은 경로 이름만 만든 것은 공유 저장소가 아니다. 현재 파일 저장 구현에서 worker가 생성한 보고서를 API가 읽으려면 실제 같은 볼륨이어야 한다. 서로 다른 호스트에 배치할 경우 CDEP가 지원하는 공유 파일시스템을 사용하거나 검증된 객체 저장소 어댑터를 추가한 뒤 배포한다. 초기 Compose는 같은 Docker 호스트의 named volume을 공유하므로 이 조건을 충족한다. PostgreSQL 데이터 디렉터리를 여러 DB 컨테이너가 동시에 직접 공유해서는 안 된다.

## 4. 기존 Open WebUI 연결

1. `openwebui/README.md`에 따라 bridge 패키지와 Tool/Action을 설치한다. 원본 Dockerfile은 변경하지 않았으며 별도 add-on 예제를 제공한다. 기존 백업 래퍼 CMD를 교체하지 않는다.
2. API와 동일한 HUMAN/MODEL/TRACE 키·issuer·audience·epoch를 Open WebUI **서버 환경변수**에 주입한다. WORKER 키는 Open WebUI에 전달하지 않는다. 프런트엔드, Tool 설명, 모델 프롬프트, Valves 사용자 입력에 키를 넣지 않는다.
3. `QUALITY_API_BASE_URL`, `QUALITY_UI_BASE_URL`, `QUALITY_INSTANCE_ID`를 설정한다. 기존 `NO_PROXY`와 `no_proxy`는 유지하면서 API 호스트를 추가한다. 두 서버의 시각 동기화도 확인한다.
4. `QUALITY_OUTBOX_PATH`를 재시작 후 유지되는 **설치별 독립 경로**에 설정한다. Open WebUI의 `/app/backend/data`는 현재 구성에서 컨테이너 로컬 경로일 수 있고, DB-only S3 백업이 outbox를 포함한다는 근거가 없다. `/config/work/sharedworkspace`를 사용할 수 있다면 운영/개발 및 인스턴스별 디렉터리를 분리하고 권한·동시 접근 조건을 검증한다.
5. M0에서 실제 `__user__`, chat ID, Action 버튼 동작과 래퍼 기록 수집을 확인한다. Filter의 Tool 결과 수집 가능성에 운영 기능을 의존시키지 않는다.

API와 UI는 같은 웹 앱에서 제공된다. 브라우저가 내부 Docker 이름 `quality-api`를 알아야 하는 구조가 아니다. 브라우저가 접속할 public origin은 사내 사용자 네트워크에서 접근 가능한 주소여야 한다. HTTPS를 사용하고, reverse proxy access log에는 ticket/Authorization/요청 본문을 기록하지 않는다. ticket은 URL fragment로 전달되고 짧은 수명의 일회용 교환 후 제거된다.

`/health/live`는 프로세스 확인, `/health/ready`는 DB·스키마·산출물 저장소 준비 확인에 사용한다. 건강 확인은 실제 품질 API나 DRM/EDM 파일을 호출하지 않는다. worker는 별도 HTTP 서버가 없으므로 컨테이너 재시작 횟수와 보고서 작업 실패/적체도 함께 모니터링한다.

## 5. 업데이트

스테이징에서 M0 수용 시험과 마이그레이션·복구를 완료한 동일 이미지를 승격한다. 먼저 아래 백업을 만들고 API/worker를 멈춘 뒤 마이그레이션을 실행한다. 새 API/worker는 같은 버전의 이미지를 사용한다. DB 스키마 변경 후 단순히 이전 이미지로 되돌릴 수 있다고 가정하지 말고, 해당 버전의 마이그레이션 정책 또는 검증된 백업 복원을 적용한다.

```bash
docker compose stop quality-api quality-worker
docker compose --profile ops run --rm migrate
docker compose up -d quality-api quality-worker
curl --fail http://127.0.0.1:18080/health/ready
```

원천 Tool 실행 중 종료가 발생한 경우 완료 응답이 없다는 이유로 다시 실행하지 않는다. 미확인 호출은 운영자가 원천 상태를 확인한 후 UI/API의 명시적 복구 경로에서 처리한다.

## 6. 일관된 오프라인 백업

이 배포의 최소 백업 단위는 **PostgreSQL dump + 산출물 tar + checksum manifest**다. 서비스 쓰기를 멈춘 상태에서 묶는다. DB만 백업하면 보고서 파일을 복원할 수 없다. 기존 Open WebUI S3 prefix를 재사용하지 않고 별도 저장·보관 정책을 설정한다.

```bash
mkdir -m 700 -p backups
python3 scripts/backup.py --output backups/quality-20261110-01
python3 scripts/verify_backup.py backups/quality-20261110-01
```

백업 스크립트는 API·worker를 중지하고 DB dump와 파일을 수집한 후 검증에 성공하면 원래 실행 중이던 서비스만 재시작한다. 실패 시 자동 재시작하지 않는다. 외부 클라이언트에는 점검 시간을 공지하며 백업 중 별도 migrate/recover 또는 DB 직접 쓰기를 실행하지 않는다. 스크립트의 파일 잠금은 이 스크립트끼리의 중복 실행만 막는다.

S3 전송·보관 자동화는 회사 표준 도구에 이 백업 디렉터리를 넘겨 연결한다. 자격증명을 이 프로젝트에 작성하지 않는다. 체크섬은 전송 오류 검출용이며 접근 통제·암호화·변조 방지 저장 정책을 대체하지 않는다. 백업에는 품질 자료가 있으므로 일반 공유 폴더에 저장하지 않는다.

## 7. 기존 볼륨을 보존하는 복구 연습

아래는 **새 Compose 프로젝트와 새 볼륨**에 복원하는 운영자 절차다. 실제 Docker 환경에서 검증 후 운영 절차로 확정한다. 원래 프로젝트를 삭제하거나 기존 DB에 dump를 덮어쓰지 않는다. 예시의 `quality-recovery`가 이미 있다면 새로운 이름을 선택한다.

1. 원래 API/worker를 중지하고 Open WebUI의 품질 연결을 일시 중지한다. `verify_backup.py`를 실행한다. 백업 당시 이미지/DB 버전과 호환되는 배포 코드를 준비한다.
2. `python3 scripts/generate_env.py --output .env.restore`로 **새 키와 새 epoch**를 생성하고 `QUALITY_RECOVERY_MODE=true`로 바꾼다. 백업의 키/epoch를 복원하지 않는다. 이미지 참조·팀 매핑·Tool registry를 검토해 복원한다. 이 파일의 host port는 사용하지 않는 포트로 정한다.
3. 새 프로젝트에 DB만 올린다. 이 시점에는 API·worker를 실행하지 않는다.

```bash
docker compose --project-name quality-recovery --env-file .env.restore up -d db
docker compose --project-name quality-recovery --env-file .env.restore exec -T db \
  pg_restore -U quality -d quality --no-owner --no-acl --exit-on-error < backups/quality-20261110-01/database.dump
```

4. 새 산출물 볼륨에 아래 명령으로 복원한다. 반드시 앞에서 검증한 동일 tar를 입력한다. Python 3.12의 extraction filter로 링크·경로 접근을 제한하며, 스크립트가 빈 볼륨인지 확인한다.

```bash
docker compose --project-name quality-recovery --env-file .env.restore run --rm --no-deps -T \
  --entrypoint python quality-api -c 'import pathlib,sys,tarfile; p=pathlib.Path("/var/lib/quality/artifacts"); assert not any(p.iterdir()), "Restore requires an empty artifact volume"; tarfile.open(fileobj=sys.stdin.buffer,mode="r|*").extractall("/var/lib/quality",filter="data")' \
  < backups/quality-20261110-01/artifacts.tar
docker compose --project-name quality-recovery --env-file .env.restore --profile ops run --rm migrate
docker compose --project-name quality-recovery --env-file .env.restore run --rm --no-deps \
  quality-api python -m quality_api.cli recover
```

5. `recover`가 중단된 실행/호출을 `unknown`, 보고서 작업을 `interrupted`로 전환하고 기존 ticket을 소진했는지 확인한다. 새 epoch가 복원된 호출 기록 중 하나와 같으면 CLI가 거부하므로 새 epoch를 생성한다. 이전 JWT, claim, Action ticket, Open WebUI outbox 이벤트는 새 epoch에 속하지 않으므로 수락하면 안 된다. 옛 outbox를 삭제하거나 자동 재전송하지 않고 별도로 보존·점검한다.
6. worker는 중지한 채 복구 모드의 API만 올린다. 검증용 Open WebUI bridge에 새 키/epoch와 새 API 주소를 반영하고, 업무 Tool 실행은 중지한 상태에서 Action으로 읽기·보고서 다운로드·권한·누락 파일을 확인한다. 복구 모드는 UI ticket 교환을 허용하지만 근거/호출 상태 변경은 거부한다. `/health/ready`는 의도적으로 503이므로 일반 ingress가 라우팅하지 않을 수 있다. 이때 공개 readiness 우회를 만들지 말고 운영자가 접근 가능한 격리된 경로에서 확인한다.
7. 대조가 끝나면 `.env.restore`를 `QUALITY_RECOVERY_MODE=false`, `QUALITY_DRAINING=true`로 바꾸고 API를 재생성한다. 새 실행/claim을 막은 채 원천 시스템 상태를 확인하고 미확인 호출을 수동 정리한다. 성공 여부 확인 없이 원천 Tool을 재실행하지 않는다. 이 단계에서도 worker는 시작하지 않는다.
8. 정리가 끝나면 `QUALITY_DRAINING=false`를 적용해 API를 재생성하고 ready를 확인한다. Open WebUI의 검증한 연결을 일반 업무에 열고 worker를 시작한다. 복구/드레인 모드에서 worker 기동 거부는 의도된 동작이다. 기존 볼륨은 검증과 보관 정책이 끝날 때까지 유지한다.

단계 6~8에서 환경변수 변경은 기존 컨테이너의 단순 restart로 반영되지 않는다. 새 환경으로 재생성한다.

```bash
# 각 단계의 .env.restore 값을 확인한 뒤 API만 재생성한다.
docker compose --project-name quality-recovery --env-file .env.restore up -d --no-deps --force-recreate quality-api
# 마지막 단계에서만 worker를 올린다.
docker compose --project-name quality-recovery --env-file .env.restore up -d --no-deps quality-worker
```

## 8. 운영 전 확인할 미검증 항목

- 사내 승인 이미지에서 빌드, UID 10001의 볼륨 쓰기, CDEP 종료 시 SIGTERM 전달.
- CDEP의 실제 PostgreSQL과 전체 백업/복구 훈련. 로컬 독립 PostgreSQL 16에서는 동시 요청·마이그레이션·보고서 worker를 검증했지만 실제 배포 연결과는 구분한다.
- 기존 Open WebUI 버전에서 bridge/Action 등록, chat/user context 전달, 비공개 ticket 교환.
- 서버 간 TLS/CA/방화벽/NO_PROXY, 브라우저에서 UI 접근, access log 비밀값 제외.
- 실제 품질 API·DRM/EDM skill의 정상/실패/시간초과·중복 호출과 권한 전달.
- 로컬 LLM의 Tool 호출 및 evidence 제안 스키마. 자동 근거 제안이 실패해도 사람이 검토할 수 있는 원천 기록 경로.
- 원본 Open WebUI의 업로드·vector DB·trace outbox 실제 백업 범위. 기존 DB-only 백업과 혼동하지 않는다.
