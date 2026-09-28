# 다음 운영 단계 — 2026-09-05

## 09-28 오경보 수정 — 배포 완료, 자연 실행 검증 대기

이 절이 아래의 이전 운영 상태보다 우선한다. 20:17 UTC 뉴스 성공·원본 커밋
8분 뒤에 120분 초과 경고가 발송돼 오경보임을 확인했다. 캐시가 원인인지는
확정하지 않았다.

- 승인된 범위만 별도 작업 트리에서 반영했다. `50501f2`로 메일의 선택적
  판단 근거 입력을 먼저 추가하고 `39cd8ab`로 재조회 로직을 반영했다.
- main의 dispatch 실행 최근 20개를 생성 시각으로 비교한다. GET 캐시를
  우회하고, 지연·기록 없음은 최근 생성 구간으로 다시 조회한다. 실패·미완료도
  재조회한다. 조회 오류를 실제 지연으로 단정하지 않는다.
- 메일/로그에 점검 시각, 임계값, 조회별 run ID·생성 시각·경과 분·상태와
  최종 판단을 남긴다. 기존 주기·임계값·POST 중복 재시도 금지는 유지한다.
- 20:41:21 UTC 새 Worker `a777dc88` 100% 배포와 실제 배포 소스의 수정 내용을
  확인했다. 기존 Python 수정·등급·평가 기준은 운영 반영하지 않았다.
- 검증: Worker 20개, 운영 원격 기반 Python 929개/20개 제외, 로컬 통합
  981개/20개 제외 통과. Ruff·포맷·dry-run·diff 검사 통과. 경고는 각각
  946/1,060개로 남아 있다. 테스트 더블의 필드 오류는 실제 결과 타입으로 수정했다.
- **아직 남음:** 배포 후 자연 뉴스와 원본 커밋, 21:25·22:25 UTC 두 번의
  watchdog 판단 로그를 Actions와 대조한다. 예정 뉴스는 21:17·22:17 UTC다.
  다음 아침은 22:07 UTC, 저녁은 09-29 12:37 UTC이며 아직 성공 증거가 아니다.
- 수정 전 09-28 저녁 보고는 KR 09-28 자료를 저장하고 vault/email 전송에
  성공했다. 달력 수집 오류는 별도 미해결이다. 오경보 수정으로 닫지 않는다.
- 원본 삭제·유료 백필·수동 보고·시험 경고 발송·추가 결제는 하지 않았다.
  실시간 로그 연결은 영구 모니터링이 아니다. 이 환경에는 이벤트 구독 도구가
  없어 자동 후속 점검 예약을 만들지 못했다. 무메일만으로 정상 판정하지 않는다.

전제: 이 문서는 Cloudflare 스케줄러 변경이 검토·병합된 뒤에만 따른다. 여기의 어떤
단계도 OpenAI/Anthropic 유료 호출이나 뉴스 점수 백필을 승인하지 않는다.

## 1. Cloudflare Free Worker 만들기 (Ricky, 약 10분)

1. Cloudflare 대시보드에서 Workers Free 플랜으로 Worker를 만든다.
2. 이 저장소의 `infra/cloudflare-scheduler/`를 배포 대상으로 선택한다.
3. GitHub에서 fine-grained PAT를 새로 만든다. 이 저장소만 선택하고 권한은
   **Actions: write**만 준다. 만료일은 90일로 잡는다.
4. Worker secret 이름 `GITHUB_DISPATCH_TOKEN`에 PAT를 넣는다. 채팅, 코드,
   저장소 파일에는 넣지 않는다.
5. Worker와 secret까지만 먼저 준비한다. GitHub의 기존 `schedule:`은 이 변경에서
   제거되므로, 저장소 변경을 GitHub에 올리기 전에는 이 저장소 설정으로 Worker를
   배포하지 않는다.

## 2. 스케줄 전환 (Codex + Ricky, 약 5분)

GitHub 변경을 `main`에 올린 직후 Worker를 배포한다. 이 순서라야 GitHub의 기존
스케줄과 Worker cron이 겹치지 않으며, 전환 공백도 짧다.

1. Codex가 scheduler 변경을 GitHub `main`에 push한다.
2. Codex가 `npx wrangler deploy --config infra/cloudflare-scheduler/wrangler.toml`을
   실행한다.
3. Cloudflare Dashboard의 **Workers & Pages → market-briefing-scheduler → Triggers**에서
   cron 5개가 보이는지 Ricky가 확인한다.
4. 보고서 workflow를 수동 실행하지 않는다. 다음 정해진 cron에서 자동 실행되는지
   GitHub Actions 목록으로 확인한다.

## 3. 비용 없는 연결 확인 (Codex + Ricky, 약 10분)

1. Cloudflare 로그에서 뉴스 수집 `workflow_dispatch` 한 번이 204로 끝나는지 본다.
2. GitHub Actions에서 `collect-news` 실행이 생성되는지 확인하고, 새 raw run file이
   커밋됐는지 확인한다.
3. Worker의 아침·저녁 cron은 다음 슬롯에 각각 한 번만 확인한다. **테스트를 위해
   `report.yml`을 수동 실행하지 않는다.** 점수 채점 비용이 생길 수 있다.
4. 120분 이상 뉴스 실행이 없을 때 `scheduler-watchdog`가 이메일만 보내는지 확인한다.
   이 워크플로는 vault에 보고서를 쓰지 않는다.

## 4. 계속 지킬 비용 규칙

- `synthesis.enabled: false`를 유지한다. ⑤/⑧을 다시 켜려면, 필요한 이유·월 예산·중단
  조건을 먼저 기록하고 Ricky가 명시적으로 승인한다.
- 점수 채점은 `MAX_PAIRS_PER_RUN=180` 및 OpenAI 계정 $5 한도를 유지한다.
- 과거 RSS 뉴스 점수 백필은 다시 승인받기 전에는 실행하지 않는다.
- Cloudflare Free 플랜에서 유료 플랜으로 바꾸지 않는다. 업그레이드는 별도 승인이다.
- PAT는 90일 안에 교체하고, 교체 뒤 Worker dispatch 한 번만 다시 확인한다.

## 이후 순서

스케줄러가 안정화된 뒤에만 `config/rating.yaml` 캘리브레이션과 collector known-value
검증을 별도 변경으로 검토한다. `news_polarity` 가중치는 2026-11-13 게이트 전에는
활성화하지 않는다.
