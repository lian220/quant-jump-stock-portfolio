# 코드 샘플

> 전체 소스는 비공개입니다. 아래는 **시크릿이 전혀 없는 순수 도메인 로직**으로, 설계 의도를 보여주기 위한 발췌입니다.
> 두 파일 모두 [ADR 0006 — 점수 모델 재설계](../docs/decisions.md#adr-0006--추천-점수-모델-재설계-)의 결과물입니다.

## 1. `scoring_spec.yaml` — 점수 산식 SSoT

추천 점수의 **단일 진실원(Single Source of Truth)**. Python(Data Engine)과 Kotlin(Core API)이 동일하게 이 한 파일을 참조해, 이전에 발생했던 "두 언어 간 산식 값 분기(드리프트)"를 원천 차단한다.

이 파일에서 볼 수 있는 것:
- 축별 weight·max·정규화 정책을 **설정으로 외부화** (코드 하드코딩 제거)
- 등급/라벨 임계를 **운영 데이터 462표본 percentile로 재보정**한 근거를 주석으로 추적
- `spec_version` / `formula_version` 분리 — 산식 변경과 파라미터 조정을 독립 버저닝
- 변경 이력을 파일 내 changelog로 관리 (PR·ADR 추적)

## 2. `scoring_policy.py` — 산식 구현 (도메인 계층)

`scoring_spec.yaml`을 로드해 점수를 계산하는 **불변(immutable) 도메인 정책 객체**.

설계 포인트:
- **로드 시 invariant 검증** (`_validate_invariants`) — weight 합 = 1.0, `composite_max` = 파생값 일치, 등급 임계 단조 감소 등을 부팅 시점에 보장 (fail-fast)
- **금융 계산 정밀도** — `Decimal` + `ROUND_HALF_UP`로 부동소수점 오차 차단
- **결측 축 재정규화** — 데이터 없는 축은 0점 벌점 대신 제외 후 weight 재정규화 (ADR 0006 §2.4)
- **XAI** — `axis_contributions`로 축별 기여도를 보존해 "왜 이 점수인지" 설명 가능 (서비스 화면의 신호별 강도 막대로 노출)
- **선형 매핑** — 0.5 컷오프 제거로 하락 종목도 연속 저점수 (0점 쏠림 해소)
- **롤백 안전성** — `negative_policy`(zero/veto), VIX gate를 spec 플래그로 토글, 코드 변경 없이 정책 전환

## 적용 아키텍처

Hexagonal Architecture의 **domain 계층**에 위치 — 외부 기술(DB, 프레임워크) 의존성이 전혀 없는 순수 비즈니스 규칙이다. 그래서 단위 테스트(golden CSV 50건 + property test)로 빠르게 검증 가능하다.
