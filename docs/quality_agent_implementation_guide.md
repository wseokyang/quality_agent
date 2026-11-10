# 사내 코딩 LLM용 구현 가이드 — 품질팀 에이전트 확장

> **현재 구현 확인**: 이 문서는 설계 기준이다. 현재 코드의 동작·제한은 [구현 상태](./implementation_status.md), 실행 방법은 [운영 배포 문서](./deployment.md), 실제 요청 DTO는 [OpenAPI](./openapi.json)를 우선 대조한다.

> 버전 1.1 / 2026-11-10. [품질 PRD/FRD v1.2](../prd_frd_quality.md)의 구현 지시서다.
> **대상 독자**: 코드를 작성하는 개발용 LLM. 실제 업무 중 호출되는 vLLM 모델은 아래에서 **운영 모델**이라 부른다. 두 역할을 혼동하지 않는다.
> **현재 산출물**: 문서·계약·검증 기준이다. 프로그램 구현, 사내 연결, 실제 모델 평가가 완료된 상태가 아니다.
> **배포 결정**: Open WebUI는 기존 Dockerfile로 유지하고 품질 업무 API를 별도 서비스로 배포한다. [배포 가이드](./quality_agent_deployment.md)를 S0와 S5에서 함께 읽는다.

## 0. 시작할 때 전달할 프롬프트

아래 블록과 이 파일을 먼저 전달한다. 전체 PRD와 모든 세부 명세를 한 번에 구현하라고 요청하지 않는다. 작업 단계에 필요한 계약·검증 절만 함께 제공한다.

```text
너는 기존 품질팀 에이전트를 확장하는 개발자다.
현재 구성은 Open WebUI + vLLM 로컬 모델 + 기존 품질 API Tools다.
DRM·EDM 읽기 skill도 이미 있다. 이를 다시 만들지 않는다.
목표는 T-RAX에서 참고한 케이스·근거 검토·절차화·지식 공유 기능 추가다.

이번 요청에서는 quality_agent_implementation_guide.md의 지정 단계 하나만 구현한다.
제품 범위: prd_frd_quality.md
필드·상태·권한·API 계약: quality_agent_contracts.md
합격 조건: quality_agent_validation.md
배포 조건: quality_agent_deployment.md
Open WebUI는 기존 Dockerfile로 배포 중이며 추가 서비스 배포가 가능하다.
품질 업무 API는 별도 이미지/서비스로 만들고 기존 Open WebUI 기동 명령에 합치지 않는다.
확인하지 못한 사내 API·Open WebUI 함수·Tool 이름·토큰 형식을 발명하지 않는다.
기존 모델/Tools 호출을 별도 opencode 또는 Pipe 에이전트로 교체하지 않는다.
M1 관리 케이스의 원천 Tool은 읽기 전용이다.
원천 결과·실행 ID·권한·승인 상태는 서버가 관리하며 운영 모델이 만들지 않는다.
모르는 연결은 테스트용 fake로 분리하고 실제 연결 완료라고 보고하지 않는다.
환경 정보가 부족해도 순수 로직·스키마·fake 기반 테스트는 진행한다.
실제 호출에 필요한 정보가 없으면 해당 연결만 NOT_CONFIGURED로 막는다.

먼저 이번 단계, 읽은 실제 파일, 확인된 환경, 누락 정보, 변경 예정 파일을 적어라.
그다음 코드를 구현하고 지정 테스트를 실행하라.
응답 끝에는 변경 파일, 테스트 명령·실제 결과, mock/실연동 구분,
남은 제한, 다음 단계 진입 가능 여부를 보고하라.
테스트를 실행하지 못했으면 통과했다고 쓰지 마라.
한 단계의 필수 테스트가 실패한 상태에서 다음 단계 구현으로 넘어가지 마라.
```

## 1. 문서 읽기 순서와 결정 우선순위

| 순서 | 읽을 자료 | 용도 |
|---|---|---|
| 1 | 이 가이드 §0–4 | 범위, 기존 자산, M0 정보, 구현 순서 |
| 2 | [PRD/FRD](../prd_frd_quality.md)에서 해당 QFR | 사용자에게 필요한 기능 |
| 3 | [계약 명세](./quality_agent_contracts.md)의 해당 C 절 | 입력·출력·권한·DB 제약·실패 처리 |
| 4 | [검증 가이드](./quality_agent_validation.md)의 해당 테스트 | 코드 완료 조건 |
| 5 | [배포 가이드](./quality_agent_deployment.md)의 해당 D 절 | 별도 이미지·연결·저장·기동·복구 |

[요청 JSON Schema](./quality_agent_schemas.json)의 `$defs`에는 M1 핵심 DTO와 선택 확장 DTO를 제공한다. endpoint별 해당 정의로 서버 입력을 검증하고, schema 통과와 권한·상태·출처 검사를 혼동하지 않는다. 기본 루트는 EvidenceProposal이다. 이 파일은 서버 검증용이며 실제 vLLM이 모든 JSON Schema 키워드를 지원한다는 뜻은 아니다.

제품 범위는 최종 PRD, 구체 계약은 계약 명세, 현재 환경 사실은 실제 소스·설정·환경표가 기준이다. 테스트 때문에 권한·출처 규칙을 느슨하게 바꾸지 않는다. 과거 설계 및 T-RAX의 기술 선택은 새 기능의 강제 조건이 아니다.

계약과 실제 설치가 다르면 **어댑터에서 차이를 처리**한다. Open WebUI 코어를 바로 수정하거나 사내 API를 임의 변경하지 않는다. 해결이 불가능한 경우 영향받는 기능을 표시하고 그 기능의 실연동을 완료 처리하지 않는다.

## 2. 바꾸지 않을 실행 경로

```text
일반 대화: 기존 Open WebUI → 기존 vLLM / 기존 Tools → 기존 응답
관리 업무: 같은 경로 + 검증된 run/call 연결 + 업무 저장 + 검토 Action
추가 서비스: 케이스·근거·결과·자산 상태 관리
선택 작업: 보고서 생성·절차 검증·배포 동기화
```

### 2.1 구현 금지 사항

- 새 자율 에이전트 루프, 필수 opencode, 전체 Next.js 채팅 UI를 만들지 않는다.
- 실제 Tool 목록을 보지 않고 `quality_search`, `edm_read` 같은 이름을 실제 사내 Tool이라고 사용하지 않는다. 예시 이름은 fake fixture에만 쓴다.
- 모델 답변을 실행 로그로 파싱해서 실제 ToolExecution이라고 저장하지 않는다.
- 모델 Tool과 사람 승인 Action에 같은 관리자 권한을 주지 않는다.
- 프롬프트에 “승인받아라”라고 쓰는 것으로 실행 전 승인 검사를 대체하지 않는다.
- 원천 호출 실패와 기록 저장 실패를 하나의 재시도 블록으로 감싸지 않는다.
- SQLite 테스트 성공으로 다중 사용자 잠금·동시성 검증을 완료했다고 보고하지 않는다.
- 승인·검토·출처 검증을 통과시키기 위해 입력 검증을 끄거나 출처 값을 추측하지 않는다.
- 새 코드에 실제 인증값·문서 원문·사내 주소를 예시로 하드코딩하지 않는다.

### 2.2 권장 기본 구현안

기존 저장소의 승인된 스택·인증·배포 방식이 있으면 그것을 사용한다. **사용 가능한 구현 기반이 전혀 없을 때의 참조안**은 Python + FastAPI/Pydantic, SQLAlchemy/Alembic, PostgreSQL, HTTP 클라이언트, pytest다. 실제 설치와 호환되는 버전을 선택해 고정하고 선택 이유를 기록한다. 최신 버전이나 의존성 버전을 기억으로 적지 않는다.

공통 로직은 Open WebUI 플러그인 바깥의 테스트 가능한 모듈로 둔다. 플러그인은 입력 맥락 전달·표시·기존 Tool 호출 연결만 담당한다. 기능별 파일을 분리하되 M1에 불필요한 큐·벡터 DB·대규모 프레임워크를 추가하지 않는다.

이번 배포는 별도 quality-api 이미지/서비스를 사용한다. 기존 Open WebUI의 Dockerfile·인증·vLLM·백업 시작 래퍼를 업무 API 실행을 위해 덮어쓰지 않는다. 별도 Dockerfile과 사내 플랫폼 실행 명세, 업무 저장소를 추가한다. 코드 COPY와 Open WebUI Functions/Tools 등록은 별도 절차다. 아직 없는 실제 배포 주소·이미지 tag·secret 형식을 추측하지 않는다.

아래는 **앞으로 구현할 파일 구조의 예시**다. 현재 존재한다고 가정하지 않는다.

```text
quality_extension/
├── app/domain/          # 상태 전이·권한·스키마·스냅샷
├── app/services/        # cases, trace, evidence, results, reports, assets
├── app/adapters/        # 사내 인증, 기존 Tool, DRM/EDM, 저장소
├── app/api/             # 새 업무 서비스 API
├── openwebui/           # 검증된 버전에 맞춘 Tool wrapper / Action
├── migrations/          # 업무 DB만 변경
├── deploy/              # 별도 API Dockerfile·플랫폼 명세·migration/health 설정
├── tests/unit/
├── tests/integration/   # 실제 운영용 DB와 같은 엔진의 동시성 검증
├── tests/fixtures/      # 합성·비식별 자료와 fake 원천
├── docs/environment.json
├── docs/m0_report.md
└── docs/implementation_status.md
```

## 3. M0 — 코딩 전에 확인할 정보

### 3.1 환경표

다음 템플릿을 환경표로 작성한다. `null`은 미확인이다. 비밀값을 채우는 파일이 아니다. 실제 실행 설정의 비밀값은 기존 비밀 저장소/환경변수로 관리한다.

```json
{
  "schema_version": 1,
  "integration_mode": "mock",
  "deployment": {
    "topology": "separate_quality_api",
    "separate_service_allowed": true,
    "quality_api_base_url": null,
    "quality_api_image_digest": null,
    "database_reference": null,
    "artifact_store_reference": null,
    "outbox_persistence_verified": false,
    "service_auth_verified": false,
    "backup_restore_verified": false
  },
  "openwebui": {
    "version": null,
    "image_digest": null,
    "tool_context_mapping_verified": false,
    "request_retry_identity_verified": false,
    "human_action_auth_verified": false
  },
  "model": {
    "model_id": null,
    "vllm_version": null,
    "tool_parser": null,
    "structured_output_path_verified": false,
    "quality_mode": "manual"
  },
  "auth": {
    "adapter_reference": null,
    "human_model_recorder_separation_verified": false
  },
  "tools": [],
  "features": {
    "managed_cases": false,
    "source_writes": false,
    "external_agent": false
  }
}
```

`mock`은 테스트 코드가 허용 목록의 로컬 fake만 호출하는 모드다. `real`은 실제 버전·사용자 신원·원천 권한·수집 경로가 검증된 경우에만 사용한다. mock 인증을 real에서 사용할 수 없게 시작 시 검사한다. 미설정 real 어댑터는 `NOT_CONFIGURED`를 반환하고 실제 연결을 시도하지 않는다. 문서상의 미확인 값을 임의로 채우지 않는다.

각 Tool의 목록에는 아래 항목을 포함한다.

| 필드 | 기록할 내용 |
|---|---|
| tool_id / version | 실제 등록 이름·소스 또는 배포 버전 |
| transport | workspace_python / openapi / mcp / legacy |
| effect | read / write / unknown. 소스·소유자 확인 근거 포함 |
| input/output | 민감정보를 제거한 스키마·정상/오류 샘플 |
| identity | 검증된 사용자 문맥이 실제 호출까지 전달되는 경로 |
| trace | 래퍼/원천 로그/프록시 등 수집 위치, run/call 연결 방법 |
| retry | 원천 멱등 키·실행 조회 지원 여부 |
| effect_target | source / quality_state. 원천 변경과 근거 제안·검토 기록 저장을 구분 |
| restrictions | 원문 저장·공유·크기·네트워크·시간 제한 |
| readiness | verified / unsupported / unverified, 확인 근거 위치 |

### 3.2 실제 경로 검증

M1 대상 Tools를 선정한 다음 서로 다른 **호출 방식 + 인증 방식 + 결과 형태**별 대표 경로를 검증한다. 같은 유형이라도 각 대상 Tool의 실제 입력·출력·권한 회귀 검증은 수행한다.

정상, 권한 없음, 원천 오류, 시간초과, 중복 재전송, 동일 사용자 병렬 호출, 다른 사용자 동시 호출, 기록 저장 실패를 확인한다. `user → chat → request → run → invocation → call → source`가 구분되지 않는 경로는 관리 케이스의 확정 근거 생성에 사용하지 않는다.

Filter로 전체 Tool 결과를 받는다는 전제를 만들지 않는다. Workspace Tool은 기존 호출 전후의 공통 래퍼, 외부 Tool은 원천 기록/검증된 어댑터를 검토한다. 기존 호출 한 번과 결과 형식을 유지해야 한다. 불가능한 Tool이 첫 업무의 필수이면 범위를 줄이거나 그 경로의 PoC를 추가한다.

### 3.3 M0 산출물

`m0_report.md`에는 환경표, 대상 Tool 목록, 경로별 증거, 모델 평가 결과, 실패 시 자동/보조/수동 모드, 쓰기 차단 확인, 최소/기준/상한 공수와 의존성을 기록한다. PRD §5.4의 게이트별로 `통과 / 축소 범위에서 통과 / 미완료`를 판정한다. 하나의 Tool 성공을 전체 통과로 적지 않는다.

추가 서비스 배포 가능 여부는 확인된 사실이다. 실제 서비스 URL·port·인증·DB·artifact/outbox 연결은 아직 확인할 입력이다. 배포 가이드 D8 환경표를 채우고, 코드 준비 후 T-48~55 실제 배포 검증을 수행할 계획과 담당을 기록한다.

환경 확인 대기 중에는 스키마·도메인 로직·fake 기반 테스트를 작성할 수 있다. 실제 연결에 관한 주장은 실험 결과가 있어야 한다. 현재 가이드의 모든 버전·능력 검증은 아직 수행되지 않은 요구사항이다.

## 4. 단계별 구현 작업

한 번의 코드 생성 요청에는 아래 단계 하나를 지정한다. 변경 파일 수가 커지면 같은 단계 안에서 더 나누되 계약·테스트를 유지한다. 다음 단계로 넘어갈 때 이전 구현을 통째로 재작성하지 않는다.

| 단계 | 구현 범위 | 반드시 읽을 계약 | 완료 조건 |
|---|---|---|---|
| S0 | 환경표·Tool 목록·M0 PoC·범위·공수·분리 배포 입력 | C0–C2, C4, C6, 배포 D1–D5/D8 | M0 게이트 판정, mock/실연동 구분 |
| S1 | 스키마·오류·인증 주체·DB 제약·케이스 생성/목록 | C0–C3, C7 | 권한·스키마·동일 키 충돌 테스트 |
| S2 | run 접수·실제 호출 claim·Tool 기록·원천 자료 등록 | C2–C4 | 실제 원천 1회, 기록 실패 시 재호출 0회, 사용자 격리 |
| S3 | 작은 근거 제안 스키마·원천 카드·수동 입력·모델 fallback | C5–C6 | 거짓 출처 거부, 수정 최대 1회, 원천 재실행 없음 |
| S4 | 근거 검토 Action·결과 승인·케이스 완료·동시성 | C3, C5, C7–C8 | 사람이 채택, stale 승인 거부, 판단 없이 완료 |
| S5 | 선택형 보고서·산출물 다운로드·별도 서비스 배포/복구 | C7–C8, 배포 D1–D9 | 고정 스냅샷 사용, report 실패와 case 상태 분리, T-48~55 |
| S6 | 절차 초안·지침형/검증형·개인 지식 저장 | C5–C6, C10 | 사용자 승인, 다른 입력 검증, 미승인 검색 제외 |
| S7 | 팀 공유·큐레이션·만료·배포·필요한 판단 | C8, C10 | 원천 ACL 계승, 반려/병합 사람 확인, 회수 즉시 적용 |
| S8 | 선택 도입: 원천 쓰기 승인 | C2, C4, C9 | 모든 실행 경로에 gate 적용, 미승인·변조·중복 실행 차단 |

S1–S5가 M1 대상이다. S6은 M2, S7은 M3다. S8은 자동 착수하지 않는 별도 범위다. 첫 업무가 정식 판단을 필요로 하면 해당 C8 계약만 앞당기고 M1 공수를 다시 산정한다.

### 4.1 S1의 구체 지시

먼저 domain schema와 authorization을 만든다. 인증정보가 미확인인 동안 실제 서비스에 임의 `X-User-Id`를 신뢰하는 인증을 넣지 않는다. fake principal은 테스트 경로에만 주입한다. DB unique/FK와 revision 검사를 구현한 후 HTTP route를 연결한다.

처음부터 ORM 엔터티를 API 입력으로 그대로 노출하지 않는다. 모델 입력, 사용자 조작 입력, 기록 어댑터 입력, DB 저장 모델을 각각 만든다. 입력의 미정의 필드는 거부하고 JSON 스키마를 내보내 계약과 대조한다.

### 4.2 S2의 구체 지시

Tool 하나를 골라 **기존 원천 호출 함수를 그대로 감싸는** 경로를 만든다. 호출 전 claim 실패, 원천 실패, 원천 성공 후 저장 실패를 각각 다른 테스트로 구현한다. run_id를 재사용했다고 실제 API 호출도 중복 방지된다고 가정하지 않는다. 네트워크 호출 중 DB transaction을 유지하지 않는다.

원천 결과 반환과 기록 복구를 분리한다. 허용된 결과를 안전한 임시 보관/outbox에 남길 수 없다면 unavailable 상태를 기록하고 자동 복구가 가능하다고 표시하지 않는다. 기록 복구를 위해 원천 조회를 몰래 다시 하지 않는다.

### 4.3 S3의 구체 지시

먼저 운영 모델 없이 원천 결과를 카드로 보여주고 사용자가 출처를 선택해 요약을 입력할 수 있게 한다. 이후 같은 서비스에 모델 제안 경로를 연결한다. 모델이 실패해도 출처·검토 기능을 사용할 수 있어야 한다.

운영 모델의 JSON을 정규식으로 잘라 성공 처리하거나 소실된 필드를 추정해서 채우지 않는다. 기본 S3 정리 요청은 계약 C6의 ModelDraftEnvelope를 검증하고 유효한 proposal만 EvidenceProposal API로 전달한다. 입력 스키마·ref 범위를 검증한다. 순수 문법/필드 오류만 같은 자료로 1회 수정할 수 있고, 무권한 출처·승인 필드 삽입 같은 정책 위반은 즉시 거부한다. 오류를 근거로 원천 Tool을 재실행하지 않는다.

### 4.4 S4–S5의 구체 지시

Action이 실제 사용자·선택 근거·버전을 전달하는지 먼저 확인한다. 근거별 버튼이 기본 제공된다고 가정하지 말고 메시지 Action에서 대상 목록·내용·결정을 보여주는 흐름을 구현한다. Rich UI가 부족하면 Markdown+Action 또는 인증된 검토 페이지를 사용한다.

결과 승인과 새 run이 경합하는 테스트를 만들고, 같은 케이스의 상태 변경에 동일한 lock/CAS 규약을 쓴다. 보고서 작업은 받아둔 accepted snapshot만 읽는다. `decision_mode=none`에서 판단 필드를 요구하지 않는다. 케이스 완료 후 보고서 작업을 등록하되, 보고서 성공을 완료 트랜잭션의 선행 조건으로 두지 않는다.

S5의 배포 작업은 업무 API용 별도 이미지·서비스, runtime secret·DB/artifact/outbox, migration·live/ready probe·drain·복원 절차를 포함한다. 사내 플랫폼을 사용하면 Compose 도입을 강제하지 않는다. 기존 Open WebUI의 일반 대화가 업무 API 장애에도 동작하는지 확인한다. 실제 배포를 수행하지 못했으면 패키징 검증과 실배포 미검증을 구분한다.

## 5. 코드 작성 모델이 확인해야 할 불변식

| 번호 | 반드시 참이어야 하는 조건 |
|---|---|
| I-01 | 기존 일반 대화는 업무 확장을 끄면 기존 모델·Tools로 계속 동작한다. |
| I-02 | 관리 케이스의 원천 호출은 검증된 read allowlist 또는 별도 승인된 쓰기 경로만 사용한다. |
| I-03 | 같은 요청 키·같은 본문은 같은 결과, 같은 키·다른 본문은 충돌이다. |
| I-04 | 실제 원천 호출은 원자적 invocation claim의 승자만 한 번 수행한다. |
| I-05 | 기록만 재전송할 때 원천 API 호출 횟수는 증가하지 않는다. |
| I-06 | 모델은 실제 실행 기록·사용자 승인·observed 출처를 위조할 수 없다. |
| I-07 | 출처는 현재 사용자 ACL과 case/run 또는 허용된 import 연결을 모두 통과한다. |
| I-08 | 근거 채택은 내용 검토이며 legacy/user_provided를 observed로 바꾸지 않는다. |
| I-09 | accepted snapshot의 근거 버전과 수치는 이후에 덮어쓰지 않는다. |
| I-10 | 판단과 보고서를 끈 업무도 완료할 수 있다. |
| I-11 | 모델 정리 실패는 1회 수정 후 수동 처리로 이어지고 원천 재실행으로 이어지지 않는다. |
| I-12 | 동시 요청·재접속·오래된 화면에서도 권한·revision·승인 1회성이 유지된다. |
| I-13 | 개인 지식 저장과 팀 공유는 별도 사용자 결정이다. |
| I-14 | 원천 저장/공유 제한은 결과·지식·보고서에도 계승된다. |
| I-15 | 실제로 확인하지 못한 연결·테스트·모델 품질을 완료로 보고하지 않는다. |

## 6. 코드 생성 요청의 출력 형식

```text
단계: Sx
구현 범위: QFR-... / 계약 C...
기존 파일 확인: 경로와 확인한 사실
변경 파일: 신규/수정, 각 파일의 역할
완료한 기능: 실제 구현 내용
실행한 검증: 명령, 결과, 실패 시 원인
실연동 범위: mock / 실제 DB / 실제 OWUI / 실제 원천 / 실제 모델 구분
남은 미확인: 임의로 채우지 않은 값과 영향
불변식 확인: 이번 단계의 I-... 결과
다음 단계: 가능 또는 불가, 근거
```

코드 일부만 생략한 채 “나머지는 동일하게 구현”이라고 하지 않는다. 완성하지 못한 파일은 미완료로 적는다. 실제 연결 adapter의 stub은 `NOT_CONFIGURED`를 반환해야 하며 빈 정상 응답으로 성공처럼 보이게 하지 않는다. 장애를 숨기는 광범위한 예외 무시를 사용하지 않는다.

## 7. 바로 사용할 단계별 요청 예시

```text
[첫 요청]
S0만 수행해라. 현재 제공된 Tool 코드·설정에서 사실을 추출해 환경표와
M1 대상 Tool 목록을 작성하고, 유형별 추적 PoC를 설계·실행해라.
원천 쓰기는 실행하지 마라. 데이터가 없으면 합성 fixture로 검증할 부분과
실제 환경에서 남은 부분을 분리해라. 실제 API 이름·인증 형식을 추측하지 마라.
M0 게이트와 소/중/대 공수 조건을 판정하되 미검증 항목은 통과 처리하지 마라.

[다음 요청 예시]
S1만 수행해라. 계약 C0–C3/C7의 스키마·권한·DB 제약과 케이스 API를 구현해라.
기존 프로젝트 스택을 유지하고, 사람/모델/기록 어댑터의 권한 분리를 테스트해라.
아직 Open WebUI 실제 연결·모델 호출·원천 API 호출·보고서 생성은 구현하지 마라.
같은 멱등 키의 다른 본문 충돌과 다른 사용자 접근 거부를 검증해라.
```

## 8. 실제 설치와 공식 자료를 사용하는 방법

현재 설치의 소스·API 스키마·작동하는 Tool 예제가 최우선 연결 근거다. 공식 문서의 최신 기능을 설치된 버전에도 있다고 단정하지 않는다. 함수 인자·metadata·event 형식·Tool approval·structured output 옵션은 실제 버전에서 검증한다.

Open WebUI의 Tools/Functions와 vLLM 구조화 출력은 아래 공식 자료에서 확인할 수 있다. 문서는 일반 기능 근거이며 이번 사내 환경의 검증 결과가 아니다. [Tool 개발](https://docs.openwebui.com/features/extensibility/plugin/tools/development/), [Functions](https://docs.openwebui.com/features/extensibility/plugin/functions/), [Tool approval](https://docs.openwebui.com/features/extensibility/plugin/tools/), [vLLM Tool calling](https://docs.vllm.ai/en/stable/features/tool_calling/), [vLLM 구조화 출력](https://docs.vllm.ai/en/stable/features/structured_outputs/)
