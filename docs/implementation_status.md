# 구현 상태 — 품질팀 에이전트 확장

기준일: 2026-11-10. 이 문서는 현재 코드의 실제 제공 범위를 설명한다. `prd_frd_quality.md`의 전체 요구사항과 사내 실환경 연결이 모두 완료되었다는 의미는 아니다.

## 제공하는 코드

| 영역 | 구현 | 실제 연결 경계 |
|---|---|---|
| 업무 API | FastAPI, PostgreSQL, 초기 Alembic migration, role-scoped JWT, recovery epoch, live/ready | 사내 DB·secret·주소는 배포 설정 필요 |
| 케이스 | 현재 채팅과 연결, 생성·검색·상세·완료·재개·보관·휴지통/복원·감사 | 현재 UI는 소유자 중심. 팀 자산 공유와 케이스 공동 편집은 구분 |
| 실제 실행 기록 | run/claim/outcome, 등록된 read Tool만 허용, 원자적 실행권, 중복·불명 상태·reconciliation | 기존 사내 Tool과 dispatcher의 정확한 연결 위치·호출 ID는 실제 코드로 확인 필요 |
| 근거 | 텍스트 자료 등록·허용된 출처 가져오기, 제안·채택·제외·새 버전, 출처 유형 보존 | 원천 업로드/과거 메시지의 사내 수집 API는 미설정 시 NOT_CONFIGURED |
| 결과·보고서 | 현재 채택 버전의 불변 스냅샷, DB job 기반 Markdown/DOCX 생성, 해시·ACL 검사 다운로드 | 정식 업무 판정은 기본 프로필에서 사용 안 함 |
| 절차·지식 | 기록에서 지침형 절차 초안, 모델/사람 초안, 개인 승인, 팀 신청·게시·반려·병합·피드백·만료·ZIP export | validated 자동 실행은 실제 runner/fixture 검증 없이 허용하지 않음 |
| 검토 UI | Open WebUI Action의 일회용 ticket으로 연결되는 한국어 화면, 근거·결과·자산 관리 | 실제 Windows 브라우저에서 접근 가능한 HTTPS 라우트 필요 |
| Open WebUI 연결 | 설치 가능한 Action/Tools, 기존 함수용 tracing helper, 결과 전송 outbox, 서버 context 검증 | 모든 Tool을 자동 수집하는 Filter나 원천 API 대체 코드는 제공하지 않음 |
| 모델 출력 보완 | 저장된 출처를 고정해 근거 JSON 검증, 형식 수정 최대 1회, 정책 위반·모델 장애 시 수동 검토 | 기존 모델 호출부에 model-only callback 연결 필요. 실제 vLLM 품질은 별도 평가 |
| 운영 | 별도 API/worker Dockerfile, Compose, 비밀 env 생성, 오프라인 DB+파일 백업/검증, 격리 복원 runbook | 실제 Docker 빌드·CDEP 배포·원천 데이터·S3/DRM/ADFS 연결은 미검증 |

## API 명세와 설계 문서의 관계

현재 실행 코드의 요청 DTO와 경로는 생성된 [openapi.json](./openapi.json) 및 `api/quality_api/routes/`를 기준으로 확인한다. 설계 계약과 JSON Schema는 제품 원칙과 후속 기능의 요구사항으로 유지한다.

현재 구현에서 명시적으로 구체화한 부분:

- 신뢰된 recorder의 outcome은 제한된 `result` JSON을 inline 전송하고 서버가 source refs를 생성한다. 서버 최대 1MiB, bridge mapper 최대 500KB다. 임의 URL·파일 경로를 열지 않는다.
- `artifact` 및 과거 채팅 메시지의 사내 원천 수집 어댑터는 실제 API가 없으므로 실패를 숨기지 않고 NOT_CONFIGURED로 반환한다. `user_text`와 권한 있는 `existing_ref`는 동작한다.
- 기본 프로필은 `quality-general` v1, 판단 없음·보고서 요청 시·원천 읽기 전용이다. 특정 정량 판단 체계나 원천 쓰기 승인 실행은 이 버전의 활성 기능이 아니다.
- 지침형 절차는 실행 순서·입력·관련 Tools를 검토해 재사용할 수 있게 내보낸다. 구조 검사를 실행 정확성 검증으로 표시하지 않는다.
- source 데이터는 immutable refs에 저장하고 보고서는 고정된 스냅샷을 사용한다. 자연어 해석의 정확성은 사람의 검토와 실제 모델 평가가 필요하다.
- `quality_bridge/drafting.py`는 이미 권한 확인 후 조회한 출처만 입력받는 선택 연결부다. 형식 수정 시에도 원천 Tool을 다시 실행하지 않으며, 유효한 초안도 사람이 채택해야 한다. 기존 Open WebUI 모델 루프를 자동 교체하지 않는다.
- 원천 자료의 공유 허용은 기본 false다. 팀 매핑과 source shareability를 운영자가 명시적으로 확인해야 팀 게시를 허용한다.

## 검증 범위

- 단위·API·bridge·운영 스크립트를 포함한 PostgreSQL 기반 전체 테스트 **102개 통과**, 하위 검사 3개 통과. Ruff 정적 검사와 Python 구문 검사도 통과했다. 테스트 도구의 httpx 지원 중단 예정 경고 1건이 있으며 실행 실패는 없었다.
- 별도 로컬 PostgreSQL 16.15에서 초기 migration과 전체 애플리케이션 테스트를 실행했다. 동일 키 경쟁·claim 응답 유실·병렬 읽기·단일 UI ticket 교환을 별도로 검증했다.
- 실제 API와 Chromium에서 케이스 → 자료 → 근거 채택 → 결과 → Markdown/DOCX 생성·다운로드 → 지식 저장·export → 재검토 흐름을 확인했다. 테스트는 합성 자료·임시 DB·무작위 테스트 키를 사용한다.
- 복구 모드/새 epoch 검증, 진행 중 작업 격리, 이전 token·ticket 차단, 완료된 케이스·스냅샷·보고서 보존, 반복 복구와 worker 기동 제한을 검증했다.
- 테스트 코드와 실행 방법은 `tests/`, `tests/browser/README.md`, `scripts/tests/`에 있다. fixture 통과와 실제 CDEP/원천 연결 통과는 구분한다.
- Docker CLI가 없는 작성 환경이므로 실제 이미지 빌드·Compose 기동·운영 백업 복원은 실행하지 않았다. Docker build context의 COPY 대상과 Compose YAML은 검사했다.

## 사내 도입 전 필요한 입력

1. 실제 CDEP 이미지 digest와 새 업무 API의 내부/브라우저 주소, TLS/CA/프록시 설정.
2. 현재 사용 중인 품질 API Tool 본문·입출력·인증·효과(read/write), 안정적인 dispatcher context 위치.
3. DRM/EDM skill의 실제 진입점·반환 데이터·공유 제한. 기존 읽기 기능을 새로 만들지 않는다.
4. Open WebUI 실제 사용자 ID와 업무 API의 팀·큐레이터 매핑.
5. 업무 DB·artifact·Open WebUI outbox의 영속 저장 및 복구 계획.

실제 설치 절차는 [deployment.md](./deployment.md), 기존 Open WebUI 변경 범위는 [연결 README](../openwebui/README.md)를 따른다. 기존 참고 Dockerfile의 기동·SSO·모델·백업 값은 변경하지 않았다.
