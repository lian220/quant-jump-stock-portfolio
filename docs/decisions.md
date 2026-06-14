# 기술 의사결정 (ADR 요약)

프로젝트의 주요 기술 결정은 ADR(Architecture Decision Record)로 근거·대안·결과와 함께 기록했다. 아래는 공개 가능한 요약본이다.

| ADR | 상태 | 제목 |
|---|---|---|
| 0001 | Accepted | Kafka → GCP Pub/Sub 전환 |
| 0002 | Superseded by 0006 | Negative AI Veto + Source Disagreement Policy |
| 0003 | Accepted | Quartz Scheduler → GCP Cloud Scheduler 전환 |
| 0004 | Accepted | GCE VM → Cloud Run 전환 |
| 0005 | Accepted | AES-CBC → AES-GCM 암호화 전환 |
| 0006 | Accepted | 추천 점수 모델 재설계 (0\~100 정규화 가중합) |

---

## ADR 0001 — Kafka → GCP Pub/Sub

**배경** 1인 운영 환경에서 Kafka+Zookeeper 클러스터는 합산 메모리 1GB+ 상시 점유, 브로커 장애 대응 불가, scale-to-zero 불가.

**결정** 관리형 메시지 서비스 GCP Pub/Sub로 전환.

**결과** 브로커 운영 부담 제거, 사용량 기반 과금, Cloud Run과 자연스러운 통합.

## ADR 0003 — Quartz → Cloud Scheduler

**배경** Quartz는 JVM 스레드풀 상시 점유 + QRTZ_* 메타 테이블 11개를 PostgreSQL에 점유, 스케줄 변경마다 재배포 필요, Cloud Run scale-to-zero와 양립 불가.

**결정** GCP Cloud Scheduler로 전환, Quartz 완전 제거.

**결과** 스레드풀·테이블 11개 제거, 스케줄을 Terraform(IaC)으로 관리, Cloud Run 호환.

## ADR 0004 — GCE VM → Cloud Run

**배경** e2-medium VM 월 ~$30 고정비, SSH 수동 배포, 24/7 가동(트래픽 없어도 과금), OS 패치 등 인프라 관리 부담.

**결정** 모든 서비스를 Cloud Run으로 전환 (All Cloud Run).

**결과** 월 ~$30 → ~$5 (83%↓), `git push` 자동 배포(~10분), revision 롤백, 자동 수평 확장.

## ADR 0005 — AES-CBC → AES-GCM

**배경** 외부 API 시크릿(KIS 등) 암호화에 무결성 검증이 없는 모드 사용.

**결정** 인증 암호화(AEAD)인 AES-GCM으로 전환, V1→V2 마이그레이션 구조 도입.

**결과** 암호문 무결성 검증 확보, 신규 등록은 GCM 전용. prod에서 키 미설정 시 부팅 차단(fail-fast).

## ADR 0006 — 추천 점수 모델 재설계 ⭐

**배경** 운영 데이터(189 종목-일) 기준 추천 점수 비정상(전 종목 B·C, 최고 52/100). 산식 결함 6종 + Python↔Kotlin 이중 정의 드리프트 확인.

발견된 결함:
1. 만점 도달 불가 (composite_max 7.4지만 실효 상한 ~5.45)
2. weight 모순 (tech가 weight 최대인데 실제 기여도 최소)
3. 이중 스케일 버그 (숫자는 0\~100, 색·등급은 0\~7.4 → "52점인데 녹색")
4. 0점 쏠림 (0.5 컷오프로 절반이 0점)
5. sentiment 분모 불일치 (실범위 ±0.35인데 분모 10)
6. SSoT 드리프트 (yaml ↔ Kotlin 값 분기)

**결정**
- 축별 0\~1 정규화 → weight 가중합 → 0\~100 단일 스케일
- 컷오프 제거 → 선형 매핑, 결측은 축 제외 후 weight 재정규화
- `scoring_spec.yaml` 단일 SSoT (Kotlin/FE pass-through)
- 462 표본(11거래일 × 42종목) percentile 등급 재보정

**결과**
- 0점 쏠림 해소 (ai 50%→2%, sentiment 43%→0.4%)
- 등급 분포 정상화 (S 11% / A 13% / B 25% / C 24% / D 27%)
- 회귀 검증 — pytest 200 / Kotlin gradle / Vitest 19 + golden CSV 50건 + property test
- 모바일·PC 동일 스케일 (Playwright E2E), SSoT 값 분기 0건
