# 현재 Open WebUI 참조 파일 확인

이 기록은 저장소의 파일을 읽은 결과다. CDEP에 실제 적용된 환경변수, 이미지 내부 파일, SSO 응답, 실행 중인 DB를 확인한 결과가 아니다.

대상: `openwebui/docker/dockerfile` (원본 유지)

SHA-256: `5257a1296f2b08c9496a0d80cec8c7468f3852a712e12633586d876edd36ee41`

| 항목 | 전달받은 과거 기록 | 현재 파일에서 확인한 값 / 판단 |
|---|---|---|
| LLM 연결 | HTTPS 사내 프록시 | 현재 파일은 같은 프록시의 **HTTP** 주소. 임의 변경하지 않았으며 실제 TLS 경로는 운영자가 확인한다. |
| 기본 이미지 | 사내 Open WebUI 이미지 | 파일에 `FROM`이 없다. CDEP가 주입하는 베이스 또는 별도 원본 Dockerfile을 사용한다는 주석이 있다. 독립적인 `docker build` 입력이라고 단정할 수 없다. |
| 프로젝트 복사 | `/project` 전체 복사 | 현재 파일은 `PYTHONPATH=/project`를 지정하지만 프로젝트 전체 COPY는 없다. 백업 스크립트 하나만 COPY한다. |
| 실행 사용자 | 기존 권한 유지 | 현재 `USER root`. 이번 신규 API는 UID/GID 10001이며 기존 Open WebUI 실행 사용자는 변경하지 않는다. |
| SSO | DS SSO, ADFS OIDC, 이메일 `mail` | 동일하게 명시됨. callback URL은 이 파일에 명시되지 않아 CDEP 런타임 설정 확인이 필요하다. |
| 부서 클레임 | `DeptName` | 현재 **`deptname` 소문자**. 실제 토큰 확인 없이 과거 표기로 바꾸지 않는다. |
| 허용 부서 | 품질팀 한 항목 | 현재 품질팀 및 5개 품질 그룹, 총 6개 항목. 기존 목록 유지. |
| persistent config | 비활성화 이력 | 현재 `ENABLE_PERSISTENT_CONFIG=true`, `ENABLE_OAUTH_PERSISTENT_CONFIG=false`, `RESET_CONFIG_ON_START=false`. 일반 설정과 OAuth 설정을 구분해야 한다. |
| ID-token cookie | 비활성화 | 현재 `ENABLE_OAUTH_ID_TOKEN_COOKIE=false`. |
| 데이터 | sharedworkspace 또는 로컬 DB | 현재 `DATA_DIR=/app/backend/data`. sharedworkspace DB라고 가정하지 않는다. |
| 백업 | S3, 12시간, 10개 | 현재 주기 43200초, 보관 10개, `ALLOW_EMPTY_DB=false`, WAL/FULL 명시. prefix는 여전히 `openwebui/prod/db-backups-test/`. 실제 운영 prefix인지 확인 필요. |
| 시작 명령 | 백업 래퍼가 start.sh 실행 | 현재 CMD는 `/app/backend/openwebui_s3_backup.py`. 해당 스크립트의 내용은 제공되지 않았으므로 내부 시작·복원·제외 대상은 검증하지 못했다. |
| 정적 브랜딩 | QREW 로고와 WEBUI_NAME | 현재 파일에는 `WEBUI_NAME`·branding COPY가 없다. 베이스 이미지나 외부 설정 적용 여부는 미확인. 이번 작업은 브랜딩을 변경하지 않는다. |
| proxy 예외 | 사내 도메인 | 현재 대소문자 `NO_PROXY`/`no_proxy`가 둘 다 있음. 기존 값을 보존하고 신규 API 호스트만 추가할 수 있다. |

현재 제공되지 않은 자료: `scripts/openwebui_s3_backup.py` 실물, integration/branding 리소스, CDEP 런타임 환경변수, 이미지 digest, 사용 중인 Open WebUI 버전, 품질 API와 DRM/EDM skill 구현. 기존 백업 프로그램을 추측해서 재작성하지 않았다.

기존 Open WebUI DB 백업과 새 품질 서비스의 PostgreSQL·산출물 백업은 별개다. 전자를 복원했다고 후자가 복원되지 않으며, 과거 DB-only 백업에서 uploads/vector_db까지 복원된다고 가정하지 않는다.
