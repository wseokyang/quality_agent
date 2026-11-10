# 기존 CDEP Open WebUI 연결

이 디렉터리는 기존 Open WebUI에 설치할 **서버용 Python 연결부, Action, 모델 Tools**다. 업무 API는 별도 서버에서 실행한다. 기존 `docker/dockerfile`은 참고용이며 수정하지 않았다. 실제 백업 스크립트·사내 API Tool 본문·배포된 Open WebUI 버전은 이 작업 폴더에 없으므로 대체하거나 추측하지 않는다.

현재 참고 파일은 `/app/backend/data`와 S3 백업 감독 프로세스를 사용하며, `CMD`는 `openwebui_s3_backup.py`다. 이 기능의 업무 DB와 산출물은 새 업무 API 서버에서 따로 영속화한다. 이 설치로 기존 DB 백업이 업무 DB까지 백업하게 되는 것은 아니다. 업로드/RAG 경로도 별도 검증 대상이다.

## 1. 설치되는 기능과 실제 경계

| 코드 | 동작 |
|---|---|
| `plugins/quality_action.py` | 사람의 버튼 클릭·확인 후 현재 대화에 케이스 생성, 인증된 검토 화면 연결 |
| `plugins/quality_tools.py` | 현재 케이스/출처 조회, 근거·지식·절차 초안 제안, 승인된 자산 검색 |
| `quality_bridge/tracing.py` | **원래 Tool 함수를 호출하는 위치**에서 read claim → 함수 1회 실행 → 실제 결과 기록 |
| `quality_bridge/outbox.py` | 이미 나온 결과의 전송만 재시도. 원천 함수를 실행하지 않음 |
| `quality_bridge/drafting.py` | 선택 연결: 저장된 출처만 모델로 정리, 형식 오류 1회 수정, 실패 시 원본과 수동 검토로 전환 |
| `examples/existing_tool_adapter.py` | 기존 API·OAuth·DRM/EDM 함수 안에 넣을 최소 연결 예 |
| `examples/dispatcher_lifecycle.py` | 신뢰된 dispatcher의 run 시작·호출·종료 연결 예 |

Action/Tools는 설치 가능한 코드다. 단, **Action/Tools 등록만으로 기존 모든 Tool의 실행이 자동 수집되지는 않는다.** 기존 사내 Tool 코드와 실제 dispatcher를 확인해 마지막 두 예제를 연결해야 한다. Filter가 Tool의 실제 결과와 안정적인 논리 호출 ID를 제공한다고 가정하지 않았다.

현재 제공되는 운영 경로는 등록된 **읽기 Tool**이다. 기존 쓰기 Tool이 관리 모델에 직접 노출되면 이 래퍼를 우회할 수 있다. 관리 케이스용 모델에는 검증된 읽기 래퍼와 품질 제안 Tools만 허용하고, 원래 Tool·외부 OpenAPI/MCP·내장 실행 도구 등 별도 노출 경로를 점검한다. 기존 쓰기 기능의 승인 실행은 이 코드에 구현된 것으로 간주하지 않는다. 품질 API의 승인·확정은 모델 Tools에 노출하지 않는다.

## 2. 기존 Docker 배포에 Python 패키지 추가

기존 이미지에 다음 패키지를 설치한다. 빌드 맥락은 `quality_agent/`다.

```dockerfile
# 기존 FROM, COPY /project, 파일 권한, SSO/부서/LLM/백업 설정 유지
COPY openwebui/pyproject.toml /tmp/quality-openwebui-bridge/pyproject.toml
COPY openwebui/quality_bridge/ /tmp/quality-openwebui-bridge/quality_bridge/
RUN python -m pip install --no-cache-dir /tmp/quality-openwebui-bridge \
    && python -c "import quality_bridge" \
    && rm -rf /tmp/quality-openwebui-bridge
# 기존 ENTRYPOINT/CMD 그대로 유지
```

또는 기존 최종 이미지를 베이스로 `docker/Dockerfile.addon`을 빌드한다.

```bash
docker build -f openwebui/docker/Dockerfile.addon \
  --build-arg OPENWEBUI_BASE_IMAGE='사내-승인-QREW-이미지@sha256:확인된-digest' \
  -t 사내-레지스트리/qrew-quality:검증태그 .
```

위 이미지 이름은 실제 값으로 바꿔야 한다. 사내 패키지 미러와 기존 빌드 사용자 권한을 그대로 사용한다. 설치 단계에 필요한 쓰기 권한은 기존 Dockerfile의 의존성 설치 단계에 맞춘다. 최종 실행 사용자를 임의로 root로 변경하지 않는다. Python 3.11 이상, `httpx`, `PyJWT`가 필요하며 Open WebUI의 FastAPI/Pydantic을 사용한다.

**파일 COPY와 플러그인 등록은 별개다.** 이미지를 배포한 뒤 관리자 화면에서 다음을 등록한다.

1. Workspace/관리자 Functions에 `plugins/quality_action.py`를 붙여 넣고 활성화한다. 사용할 품질 모델에 연결한다.
2. Workspace Tools에 `plugins/quality_tools.py`를 붙여 넣는다. 사용자에게 실행 접근권한을 주고 품질 모델에 선택한다.
3. 기존 Tool의 관리자에게 `examples/existing_tool_adapter.py`와 §5를 전달해 실제 원천 호출 위치에 적용한다.
4. 프로덕션 배포 전에 고정한 Open WebUI 버전에서 Action 입력·확인·Rich UI 링크와 예약 인자 주입을 검증한다. `latest` 문자열만으로 호환성 검증을 끝내지 않는다.

새 Filter나 Pipe는 설치하지 않는다. 기존 모델 연결과 도구 선택 방식을 유지하며 관리 상태만 업무 API에 기록한다.

## 3. Open WebUI 컨테이너에만 추가할 환경변수

| 변수 | 값/의미 |
|---|---|
| `QUALITY_API_BASE_URL` | Open WebUI 서버가 호출하는 업무 API 주소. 예: `https://quality-api.사내도메인` 또는 같은 네트워크의 `http://quality-api:8000`. 끝에 `/quality/v1`은 붙이지 않는다. 과거 설정의 정확한 `/quality/v1` 접미사는 중복되지 않게 정규화한다. 리버스 프록시의 앞쪽 경로는 유지한다. |
| `QUALITY_UI_BASE_URL` | **사용자 Windows 브라우저에서 접속 가능한 HTTPS 주소**. 내부 Docker 서비스명 사용 금지. 끝에 `/ui/index.html`은 붙이지 않는다. |
| `QUALITY_INSTANCE_ID` | Open WebUI 인스턴스 고유값. 개발/운영을 분리하고 이후 임의 변경하지 않는다. |
| `QUALITY_HUMAN_JWT_SECRET` | Action ↔ 업무 API 사람 역할 서명키. 32자 이상, 런타임 secret 주입. |
| `QUALITY_MODEL_JWT_SECRET` | 제안 Tools 전용의 다른 키. |
| `QUALITY_TRACE_JWT_SECRET` | 원천 기록 연결부 전용의 다른 키. |
| `QUALITY_JWT_ISSUER` / `QUALITY_JWT_AUDIENCE` | 기본 `quality-openwebui` / `quality-api`. 양쪽 설정 일치. |
| `QUALITY_RECOVERY_EPOCH` | 업무 DB 복원 후 새 값으로 교체할 복구 세대 문자열. API와 연결부가 정확히 동일해야 한다. 필수 설정. |
| `QUALITY_ENVIRONMENT` | 운영 `production`. 개발 환경만 HTTP UI 허용. |
| `QUALITY_HTTP_TIMEOUT_SECONDS` | 기본 15. 품질 API HTTP 제한시간이며 원천 Tool 제한시간과 별개. |
| `QUALITY_OUTBOX_PATH` | 선택: 예 `/config/work/sharedworkspace/qrew-prod-quality/outbox.sqlite`. Open WebUI 재시작 후 보존되는 **인스턴스 전용 로컬 디스크**를 실제 확인한 경우만 지정. |

업무 API 서버는 같은 세 키와 별도 worker 키를 가진다. **worker 키는 Open WebUI에 배포할 필요가 없다.** 키를 Dockerfile, 모델 프롬프트, UserValves, 브라우저 JS에 넣지 않는다. 플러그인 Python은 신뢰된 관리자만 수정할 수 있어야 한다. 같은 프로세스의 임의 Python 코드를 실행할 수 있는 관리자는 환경변수도 읽을 수 있으므로, 키별 API 권한 분리는 신뢰되지 않은 플러그인의 코드 실행 격리를 대신하지 않는다.

기존 `NO_PROXY/no_proxy`를 덮어쓰지 말고 실제 업무 API 호스트를 필요한 경우 추가한다. `httpx`는 기존 프록시 환경을 사용하며 TLS 검증을 끄지 않는다. 사내 CA가 필요하면 승인된 CA 번들을 기존 컨테이너 신뢰 저장소/`SSL_CERT_FILE`로 배포한다. 서버 간 연결 주소와 사용자 브라우저 주소는 같을 수도, 다를 수도 있다.

`OAUTH_EMAIL_CLAIM=mail`, DS SSO discovery/callback, DeptName 접근 제한, 기존 integration/branding, 기존 OpenAI 호환 API base URL은 이 설치의 변경 대상이 아니다. 참고 Dockerfile의 실제 LLM base URL은 전달받은 과거 HTTPS 기록과 다를 수 있으므로 그대로 유지하고 운영 설정과 비교한다.

## 4. 인증과 화면 흐름

```text
Open WebUI가 인증한 __user__ (role=user/admin)
  → 서버 플러그인이 60초 HS256 JWT 생성 (역할별 key, issuer/audience/복구 세대)
  → 업무 API가 사용자·현재 케이스 접근권한 재검증
  → Action은 1회용 짧은 UI ticket 발급
  → 사용자 브라우저의 /ui/index.html#ticket=... 에서 exchange
  → 검토 UI는 짧은 access token을 메모리에만 보관
```

서명키와 JWT bearer는 Action의 HTML이나 모델 응답에 포함하지 않는다. 브라우저에는 일회용 ticket만 전달한다. Rich UI에 저장된 과거 연결을 다시 누르면 만료/소진될 수 있으므로 Action을 다시 실행한다. HTML 링크의 UI 주소는 관리자 환경변수만 사용하고 사용자 문자열은 escape한다. 링크는 새 탭으로 열며 `noopener noreferrer`를 사용한다. iframe sandbox 호환성은 배포 버전에서 확인한다.

계정 역할 `admin`은 임의의 케이스·팀 자료 접근권한을 뜻하지 않는다. 팀·큐레이터 권한은 업무 API 서버의 승인된 매핑으로 검사한다. 브라우저 입력/모델 JSON의 `user_id`, `role`, `approved=true`를 신뢰하지 않는다.

## 5. 기존 API Tool에 실제 실행 기록 붙이기

사내 Tool 소스가 없어 완성된 사내 endpoint·응답 DTO를 만들어 넣지 않았다. 다음 순서로 원래 Tool 함수를 그대로 감싼다.

1. **원천 목록 확정:** Tool ID/version, 실제 효과(read), 입력 스키마, 사용자별 인증, 반환 형태를 `QUALITY_TOOL_REGISTRY_PATH`가 가리키는 JSON에 등록한다. 승인되지 않은 Tool·write·unknown은 claim에서 거절한다.
2. **신뢰된 dispatch 맥락 연결:** 실제 응답 시도별 `request_id`, 논리 호출별 `invocation_id`를 서버 dispatch 기록에 바인딩한다. 재전송에서는 같은 ID, 사용자가 명시적으로 재요청하거나 동일 인자 호출을 새로 의도한 경우 다른 ID를 사용한다. 모델의 tool_call_id만 읽거나 인자 hash/시각으로 대신하지 않는다.
3. **run 시작:** `begin_run`에 서버 검증된 사용자/대화/케이스와 request_id를 전달한다. 최초 `expected_revision`도 dispatch 기록에 보존하고 HTTP 재전송에서는 같은 값으로 전달한다. 한 응답의 여러 read 호출은 동일 run을 공유한다.
4. **원래 함수 호출 위치 교체:** `execute_read(... invoke=original_call, context=TraceContext(...))`로 감싼다. 기존 OAuth 토큰·API 키는 `original_call` 클로저 안에만 남긴다. 기록되는 arguments에는 업무 인자만 넣는다.
5. **결과 매핑:** 명시적인 `result_mapper`로 업무 값·출처 정보만 선택한다. 비밀번호·헤더·토큰을 제외한다. 파일/바이너리/스트림은 별도 실제 어댑터 없이 성공 처리하지 않는다. 현재 transport는 유한한 JSON을 최대 500KB inline 전송한다. 큰 결과는 어댑터 단계에서 설계해야 하며 임의 잘라내기나 가짜 artifact URL을 만들지 않는다.
6. **run 종료:** 모든 논리 호출의 기록 완료를 확인한 후 dispatcher가 `close_run`을 한 번 호출한다. 호출/기록이 불명확하면 열린 상태로 남기고 검토 화면에서 해결한다. Filter outlet만으로 모든 실패·중단의 종료가 보장된다고 가정하지 않는다.

공식 예약 인자 목록에는 `__chat_id__`, `__message_id__`, `__metadata__`가 있지만, **모든 버전/도구 경로에서 사용 가능한 신뢰된 per-call invocation ID는 보장되어 있지 않다.** 이 부분은 M0에서 설치 버전의 dispatch 경로를 확인해야 한다. 제공한 예제는 연결할 seam을 명확히 보여주며, 이를 자동 설치되는 플랫폼 기능처럼 표시하지 않는다. 병렬 호출은 요청 공용 mutable state에 현재 invocation을 덮어쓰지 말고 호출별 컨텍스트/ContextVar로 전달한다.

관리 케이스가 없으면 래퍼는 기존 함수 결과를 그대로 반환한다. 관리 케이스인데 ID가 없거나 기록 API에 연결할 수 없으면 **원천 실행 전에 거절**한다. claim 응답 유실/중복은 재실행 권한으로 바꾸지 않는다. source 함수는 retry 블록 밖에서 정확히 한 번 호출하며, 원래 함수 내부의 자동 재시도 정책도 별도로 확인해야 한다.

### 결과 전송 장애

`QUALITY_OUTBOX_PATH`를 지정하면 결과를 SQLite WAL/FULL journal에 먼저 저장하고 outcome 전송을 시도한다. 이 DB에는 업무 원문과 call claim token이 있을 수 있으며, signing secret/JWT는 없다. OS 권한 0600과 제한된 상위 디렉터리를 사용한다. Open WebUI 기존 S3 DB 백업이 이 파일을 보존한다고 가정하지 않는다. 실제 볼륨·백업·만료/보존 정책에 추가한다. 네트워크 공유 파일시스템의 SQLite WAL 지원 여부를 검증하지 못했다면 outbox를 활성화하지 않는다.

관리자 서버 작업에서 다음을 호출하면 **원천 실행 없이 저장된 outcome만** 재전송한다.

```python
from quality_bridge.outbox import OutcomeOutbox

# client는 현재 인증된 같은 사용자/인스턴스의 trace_adapter BridgeClient
result = await OutcomeOutbox(client.settings.outbox_path).deliver(client)
```

복구 세대가 다르면 보관하고 전송하지 않는다. `deliver`는 자동으로 run을 종료하지 않으므로 성공 여부 확인 후 dispatcher가 적절한 close/reconciliation을 수행한다. outbox 미설정 상태의 기록 전송 실패는 복구할 원문이 없을 수 있으며 `unknown`으로 표시된다. 프로세스가 원천 응답과 journal 저장 사이에 죽는 상황도 결과 불명으로 남는다. 기록을 성공으로 꾸미거나 원천을 자동 재실행하지 않는다.

## 6. 저장된 자료 정리용 모델 실패 대응 — 선택 연결

기본 `quality_tools.py`는 Open WebUI native 도구 호출로 근거 제안을 받는다. `drafting.py`는 **이미 조회·저장된 출처를 별도로 정리하는 경우** 연결하는 재사용 함수다. 모든 native 모델 호출을 자동 가로채거나 기존 대화 루프를 교체하지 않는다. 사내 모델 endpoint·클라이언트가 제공되지 않았으므로 임의 endpoint를 만들어 넣지 않았다.

```python
from quality_bridge import DraftRequest, draft_evidence

async def organize_saved_refs(refs, reviewed_model_only_callback):
    # refs는 권한 검사 후 이미 가져온 source ref 기록이다.
    # callback: async (DraftRequest) -> JSON 문자열 또는 dict.
    # 운영자가 검토한 기존 모델 호출 어댑터를 주입한다.
    result = await draft_evidence(
        refs, reviewed_model_only_callback,
        user_task="저장된 검사 결과의 확인된 사실을 요약하세요.",
        timeout_seconds=30,
    )
    if result["status"] == "proposal":
        # 검증된 proposal만 기존 model_delegate 제안 API에 전달할 수 있다.
        # 근거 채택/승인은 수행하지 않는다. 사람이 검토해야 한다.
        return result
    # needs_input이면 question을 표시한다.
    # manual_review_required이면 raw_refs를 보여주고 사람이 직접 정리한다.
    return result
```

모델 어댑터가 받는 `DraftRequest`에는 `system_prompt`, `source_refs_json`, `user_task`, `attempt`, `validation_errors`만 있다. 출처는 한 번 직렬화한 불변 문자열이며 형식 수정 시에도 정확히 같은 자료를 사용한다. **이 어댑터는 source Tools·agent 실행·쓰기 기능을 비활성화한 순수 모델 호출이어야 한다.** 임의 Python callback의 내부 동작을 이 라이브러리가 격리하는 것은 아니므로 어댑터 코드를 실제 확인해야 한다.

검증은 `ModelDraftEnvelope`의 `proposal`/`needs_input` 분기와 `EvidenceProposal`의 정확한 필드·길이·출처 범위를 검사한다. JSON 형식이나 필수 필드/길이 오류만 같은 저장 자료로 **최대 한 번** 수정한다. 제공하지 않은 출처, 승인/지표/식별자 필드, 추가 필드는 즉시 수동 검토로 전환한다. 모델 연결 실패·시간초과는 재시도하지 않는다. 원천 함수를 다시 실행하는 경로는 없다.

결과에는 `raw_refs`, `attempts`, `manual_review_required`, `evidence_review_required`가 있다. `manual_review_required=false`는 초안 자동 정리가 성공했다는 뜻일 뿐이며 **근거 채택이 완료되었다는 뜻이 아니다.** `evidence_review_required`는 항상 true다. 원본 입력은 수정하지 않으며 모델 예외 메시지의 토큰·endpoint 등은 사용자에게 전달하지 않는다.

## 7. 설치 검증

자동화 경계 테스트:

```bash
# quality_agent/ 루트에서
.venv/bin/python -m pytest tests/test_bridge.py -q
```

실제 CDEP에서는 별도로 다음을 확인한다.

- 기존 DS SSO 로그인·부서 접근·vLLM·기존 백업이 그대로 동작한다.
- 비활성/pending 사용자는 Action에 들어올 수 없고, 타인의 케이스는 읽을 수 없다.
- Windows 브라우저에서 Action 링크가 열리고 ticket 재사용/만료가 거절된다.
- read Tool 한 개의 정상·빈 결과·원천 오류·기록 전송 오류를 실제 호출한다.
- 같은 invocation의 전송 재시도에서 원천 호출 카운터가 증가하지 않는다.
- 신뢰된 invocation ID가 없는 관리 호출은 원천을 실행하지 않는다.
- 등록되지 않은/write Tool 경로가 관리 모델에서 우회 실행되지 않는다.
- 근거 초안은 모델이 제안할 수 있지만 채택·결과 확정·게시 API는 모델 키로 거절된다.
- Open WebUI/업무 API 재시작 뒤 케이스·보고서와 설정된 outbox가 유지된다.

최초 적용은 검증된 읽기 Tool 하나와 수동 근거 등록으로 시작할 수 있다. 자동 절차 실행, 임의 파일 실행, 원천 쓰기 승인은 제공하지 않으며 해당 API를 새 도구로 임의 등록하지 않는다.

## 8. 참고한 공식 확장 계약

- [Action 구조와 반환 형식](https://docs.openwebui.com/features/extensibility/plugin/functions/action/): async action, 주입된 사용자, 입력/확인 이벤트, HTMLResponse.
- [Tools 개발·예약 인자](https://docs.openwebui.com/features/extensibility/plugin/tools/development/): `Tools` 클래스, 모델 인자와 `__...__` 서버 인자 구분.
- [Rich UI와 iframe 제약](https://docs.openwebui.com/features/extensibility/plugin/development/rich-ui/): inline HTMLResponse, 링크·sandbox 동작.

공식 계약을 확인해 구현했지만 현재 운영 CDEP의 실제 버전을 확인하거나 재배포한 것은 아니다.
