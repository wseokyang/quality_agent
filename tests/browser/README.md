# 실제 API 연결 브라우저 검증

이 테스트는 임시 SQLite DB와 파일 저장소, 무작위 인증 키, 비어 있는 로컬 포트를 사용해 업무 API를 직접 실행합니다. 운영 배포나 기존 DB에 연결하지 않습니다. 주요 업무 흐름은 Chromium에서 API 응답을 모킹하지 않고 실제 화면과 보고서 생성 작업으로 검증합니다. 별도의 작은 모킹 검사로 `/service/ui/` 아래 배포했을 때 API 주소에 프록시 접두사가 유지되는지도 확인합니다. Playwright는 테스트 전용 개발 의존성입니다.

## 준비

Node.js 20 이상과 프로젝트의 Python 의존성이 필요합니다. 아래 명령은 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cd tests/browser
npm ci
npx playwright install chromium
```

Linux에서 Chromium 시스템 라이브러리가 없는 경우 해당 환경의 관리자 설치 절차로 준비합니다. Playwright가 지원하는 시스템에서는 `npx playwright install --with-deps chromium`으로 준비할 수도 있습니다. 사내망에서는 승인된 npm/Python 미러와 브라우저 설치 경로를 사용합니다.

## 실행

```bash
cd tests/browser
npm test
```

스크립트는 임시 DB를 마이그레이션하고 API 준비 상태를 확인한 뒤, 개발 전용 CLI로 발급한 일회용 티켓을 사용합니다. 보고서는 `worker --once` 명령으로 생성합니다. 성공 또는 실패 시 브라우저와 임시 API 프로세스를 종료하고 임시 DB·내부 파일 저장소를 삭제합니다.

스크린샷, 다운로드한 보고서, 지식 ZIP, 검증 결과는 출력 폴더에 남깁니다. 기본 출력 폴더는 OS 임시 디렉터리에 실행별로 생성하며 종료 메시지에 경로를 표시합니다. 인증 키, 티켓, Authorization 헤더는 저장하거나 출력하지 않습니다.

| 환경변수 | 용도 |
|---|---|
| `QUALITY_PYTHON` | 프로젝트 Python 실행 파일. 기본값은 저장소 루트의 `.venv/bin/python`이며 Windows에서는 `.venv/Scripts/python.exe`입니다. |
| `QUALITY_BROWSER_EXECUTABLE` | 이미 설치된 Chromium 실행 파일 경로. 생략하면 Playwright가 설치한 Chromium을 사용합니다. |
| `QUALITY_BROWSER_OUTPUT_DIR` | 결과 저장 폴더. 생략하면 실행별 OS 임시 폴더를 사용합니다. |

예를 들어 별도로 준비한 Python 환경과 Chromium을 사용할 수 있습니다.

```bash
QUALITY_PYTHON=/path/to/venv/bin/python \
QUALITY_BROWSER_EXECUTABLE=/path/to/chromium \
QUALITY_BROWSER_OUTPUT_DIR=./outputs/latest \
npm test
```

프로젝트 폴더에 DB나 인증 키를 작성하지 않으며, 부모 프로세스의 운영용 `QUALITY_*` 설정도 자식 API에 전달하지 않습니다. 결과 폴더에 같은 경로를 지정하면 이전 실행의 동명 출력 파일을 덮어씁니다.

## 확인 범위

- 등록된 업무 유형으로 케이스 생성
- 사용자 자료와 근거 등록, HTML 형태 원문을 텍스트로만 표시
- 사람의 근거 채택, 불변 결과 확정
- Markdown·DOCX 작업 실행과 인증된 파일 다운로드
- 업무 완료와 재개
- 지식 초안·개인 저장 승인·ZIP 내보내기
- 공유 불가 원천 자료의 팀 공유 요청 거부
- 근거 새 버전 작성 시 결과와 기존 보고서의 재검토 상태 전환
- 실제 API의 CSP 적용, 브라우저 오류·인증 정보 저장 여부
- 데스크톱·모바일 화면과 가로 넘침 여부
- `/service/ui/index.html`에서 `/service/quality/v1` 호출 여부 (별도 모킹 검사)

일반적인 실행은 약 61개의 실제 API 요청을 수행합니다. 보고서 상태 조회 시점에 따라 요청 수는 달라질 수 있으므로, 고정 호출 횟수 대신 결과와 실패 조건을 검사합니다.

이 테스트는 사내 Open WebUI·SSO·vLLM·실제 품질 API의 연결 검증을 대체하지 않습니다. 운영 PostgreSQL의 동시성 검증은 별도 테스트에서 수행합니다.
