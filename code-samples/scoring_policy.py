"""ScoringPolicy — 점수 산식 단일 진실원 (SSoT, PR 1).

리뷰 반영:
  - SCORING_SPEC_PATH env 우선 (C1)
  - raw % vs component score Public API 분리 (C2)
  - private method 호출 금지 — public API 만 노출 (H6)
  - ROUND_HALF_UP quantize 명시 (M10)
"""
from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from domain.recommendation.exceptions import SpecValidationError, SpecNotFoundError
from domain.recommendation.score import Score


# 기본 경로: backend repo 루트의 scoring_spec.yaml
# src/domain/recommendation -> src/domain -> src -> data-engine -> backend
_FALLBACK_DEFAULT = Path(__file__).resolve().parents[4] / "scoring_spec.yaml"
_QTZ_2 = Decimal("0.01")


class ScoringPolicy:
    """spec 파일로부터 로드된 점수 정책 (immutable after load)."""

    def __init__(self, spec: dict[str, Any]):
        self._spec = spec
        self._validate_invariants(spec)

    # ── factory ─────────────────────────────────────────
    @classmethod
    def load(cls, path: str) -> "ScoringPolicy":
        p = Path(path)
        if not p.exists():
            raise SpecNotFoundError(f"scoring spec not found: {path}")
        with open(p, "r", encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        return cls(spec)

    @classmethod
    @lru_cache(maxsize=4)
    def _cached_default(cls, spec_path: str) -> "ScoringPolicy":
        return cls.load(spec_path)

    @classmethod
    def load_default(cls) -> "ScoringPolicy":
        """Cached default (SCORING_SPEC_PATH 우선 → fallback 경로).

        경로를 캐시 키로 사용 — SCORING_SPEC_PATH 변경(테스트 격리/핫스왑)이 캐시에 반영된다.
        """
        path = os.environ.get("SCORING_SPEC_PATH") or str(_FALLBACK_DEFAULT)
        return cls._cached_default(path)

    # ── invariant 검증 ─────────────────────────────────
    @staticmethod
    def _validate_invariants(spec: dict[str, Any]) -> None:
        axes = spec.get("axes") or {}
        if set(axes.keys()) != {"ai", "technical", "sentiment"}:
            raise SpecValidationError(
                f"axes keys must be {{ai, technical, sentiment}}; got {set(axes.keys())}"
            )

        w_sum = sum(Decimal(str(axes[k]["weight"])) for k in axes)
        if abs(w_sum - Decimal("1.0")) > Decimal("0.001"):
            raise SpecValidationError(f"axes weights sum must be 1.0; got {w_sum}")

        # ADR 0006: 각 축 0~1 정규화 후 가중합 → ×100. 따라서 composite_max = (Σ weight) * 100.
        derived = w_sum * Decimal("100")
        declared = Decimal(str(spec.get("composite_max", 0)))
        if abs(derived - declared) > Decimal("0.001"):
            raise SpecValidationError(
                f"composite_max mismatch: declared {declared} vs derived {derived}"
            )

        grades = spec.get("grades") or {}
        order = ["S", "A", "B", "C", "D"]
        thresholds = [Decimal(str(grades[g]["min"])) for g in order if g in grades]
        if thresholds != sorted(thresholds, reverse=True):
            raise SpecValidationError(
                f"grade thresholds must be monotonic decreasing; got {thresholds}"
            )

        # PR 3b: negative_policy ∈ {zero, veto}
        ai_negative_policy = (axes.get("ai") or {}).get("negative_policy", "zero")
        if ai_negative_policy not in {"zero", "veto"}:
            raise SpecValidationError(
                f"axes.ai.negative_policy must be 'zero' or 'veto'; got '{ai_negative_policy}'"
            )

        # ADR 0006 §2.5: veto_threshold_pct 는 0 이하 (강한 하락 임계, raw rise %)
        veto_threshold = (axes.get("ai") or {}).get("veto_threshold_pct")
        if veto_threshold is not None:
            try:
                if Decimal(str(veto_threshold)) > 0:
                    raise SpecValidationError(
                        f"axes.ai.veto_threshold_pct must be <= 0; got {veto_threshold}"
                    )
            except (ValueError, TypeError) as e:
                raise SpecValidationError(
                    f"axes.ai.veto_threshold_pct must be numeric; got {veto_threshold!r}"
                ) from e

        # ADR 0006: missing_policy ∈ {zero, redistribute} (정의된 경우)
        for k in axes:
            mp = axes[k].get("missing_policy")
            if mp is not None and mp not in {"zero", "redistribute"}:
                raise SpecValidationError(
                    f"axes.{k}.missing_policy must be 'zero' or 'redistribute'; got '{mp}'"
                )

        # PR 5: macro_gates.vix (선택 — 미정의 시 gate 비활성)
        vix_gate = (spec.get("macro_gates") or {}).get("vix") or {}
        if vix_gate:
            # enabled=True 일 때 threshold 필수 — 누락 시 runtime KeyError 방지
            enabled = vix_gate.get("enabled", True)
            threshold = vix_gate.get("threshold")
            if enabled and threshold is None:
                raise SpecValidationError(
                    "macro_gates.vix.threshold required when enabled=true"
                )
            if threshold is not None:
                try:
                    if Decimal(str(threshold)) <= 0:
                        raise SpecValidationError(
                            f"macro_gates.vix.threshold must be > 0; got {threshold}"
                        )
                except (ValueError, TypeError) as e:
                    raise SpecValidationError(
                        f"macro_gates.vix.threshold must be numeric; got {threshold!r}"
                    ) from e
            missing_policy = vix_gate.get("missing_policy", "skip")
            if missing_policy not in {"skip", "block"}:
                raise SpecValidationError(
                    f"macro_gates.vix.missing_policy must be 'skip' or 'block'; got '{missing_policy}'"
                )

    # ── 속성 ─────────────────────────────────────────
    @property
    def formula_version(self) -> str:
        return self._spec["formula_version"]

    @property
    def composite_max(self) -> Decimal:
        return Decimal(str(self._spec["composite_max"]))

    @property
    def axes(self) -> dict[str, dict[str, Any]]:
        return self._spec["axes"]

    @property
    def rsi_threshold(self) -> Decimal:
        return Decimal(str(self._spec["axes"]["technical"]["rsi_threshold"]))

    @property
    def ai_cap_pct(self) -> Decimal:
        return Decimal(str(self._spec["axes"]["ai"]["cap_threshold_pct"]))

    @property
    def min_composite_score(self) -> Decimal:
        return Decimal(str(self._spec["recommendation_filter"]["min_composite_score"]))

    # PR 3b: negative AI veto 게이트
    def is_negative_veto_enabled(self) -> bool:
        """spec.axes.ai.negative_policy == 'veto' 여부. False (zero) 면 PR 1 동작 보존."""
        return (self.axes.get("ai") or {}).get("negative_policy") == "veto"

    # ADR 0006 §2.5: veto 활성 시 "강한 하락"만 차단 (약한 하락은 ai 저점수로 흡수)
    @property
    def veto_threshold_pct(self) -> Decimal:
        """veto 후보 임계 (raw rise %, 예: -10.0). 미정의 시 0 = 모든 하락 (PR 3b 동작)."""
        t = (self.axes.get("ai") or {}).get("veto_threshold_pct")
        return Decimal(str(t)) if t is not None else Decimal("0")

    def should_veto_rise_pct(self, rise_pct: Decimal | None) -> bool:
        """raw rise % 기준 veto 판정 — veto 활성 AND rise_pct < veto_threshold_pct."""
        if rise_pct is None or not self.is_negative_veto_enabled():
            return False
        return rise_pct < self.veto_threshold_pct

    def should_veto_normalized(self, normalized: Decimal | None) -> bool:
        """0~1 normalized 상승확률 기준 veto 판정.

        normalized = 0.5 + rise_pct/(2*cap) 이므로 동일 임계는
        0.5 + veto_threshold_pct/(2*cap). (예: cap 20, 임계 -10% → 0.25 미만 veto)
        """
        if normalized is None or not self.is_negative_veto_enabled():
            return False
        threshold = Decimal("0.5") + self.veto_threshold_pct / (Decimal("2") * self.ai_cap_pct)
        return normalized < threshold

    # PR 5: VIX 거시 gate
    @property
    def vix_gate(self) -> dict[str, Any] | None:
        """spec.macro_gates.vix dict 반환. 미정의 또는 enabled=False 면 None."""
        gate = (self._spec.get("macro_gates") or {}).get("vix") or {}
        if not gate or gate.get("enabled") is False:
            return None
        return gate

    def is_vix_gate_enabled(self) -> bool:
        """VIX gate 활성 여부."""
        return self.vix_gate is not None

    @property
    def vix_threshold(self) -> Decimal | None:
        """VIX > threshold 시 발동. gate 미활성 시 None."""
        gate = self.vix_gate
        if not gate:
            return None
        t = gate.get("threshold")
        return Decimal(str(t)) if t is not None else None

    def should_block_on_vix(self, vix_value: float | Decimal | None) -> bool:
        """VIX 값 → gate 발동 여부 (True 면 추천 전체 차단).

        - gate 미활성: 항상 False
        - vix_value=None: missing_policy 에 따름 ('skip' 정상 진행 / 'block' 차단)
        - 임계 초과: True
        - 비정상 값 (NaN, negative, > 200): None 으로 간주 → missing_policy 적용
        """
        threshold = self.vix_threshold  # invariant 가 enabled=True 시 None 보장
        if threshold is None:
            return False
        gate = self.vix_gate  # invariant 거친 dict
        if vix_value is None:
            return gate.get("missing_policy", "skip") == "block"
        # PR 5 review #7: sanity range 검증 (yfinance 비정상 값 방어)
        try:
            v = Decimal(str(vix_value))
        except Exception:
            return gate.get("missing_policy", "skip") == "block"
        # NaN 은 비교 자체가 InvalidOperation — 먼저 차단
        if v.is_nan() or not (Decimal("0") < v < Decimal("200")):
            return gate.get("missing_policy", "skip") == "block"
        return v > threshold

    # ── Private: clipped normalized → AI score (공통 tail) ──────
    def _score_from_clipped_normalized(self, normalized: Decimal) -> Decimal:
        """0~1 normalized probability → AI score (0~max_ai).

        ADR 0006 §2.3: 0.5 컷오프 제거 — 선형 연속 매핑.
        ai_score = clip(normalized, 0, 1) * ai_max.
        (중립 0.5 → 5.0, rise_pct -20% → normalized 0 → 0점, 하락도 연속 저점수.)
        """
        clipped = max(Decimal("0"), min(Decimal("1"), normalized))
        ai_max = Decimal(str(self.axes["ai"]["max"]))
        return (clipped * ai_max).quantize(_QTZ_2, rounding=ROUND_HALF_UP)

    # ── Public: raw % → 0~1 normalized 상승확률 ─────────
    def normalized_from_rise_pct(self, rise_pct: Decimal | None) -> Decimal | None:
        """raw rise percentage (%, 예: 16.25) → 0~1 normalized 상승확률.

        normalized = clip(0.5 + rise_pct / (2 * cap), 0, 1)
        (0% → 0.5 중립, +cap → 1.0 최대 강세, -cap → 0.0 최대 약세.)
        sync_service 의 rise_probability 저장값도 이 산식 — /40 하드코딩 금지
        (cap_threshold_pct 변경 시 본 메서드만 따라가면 됨).
        """
        if rise_pct is None:
            return None
        normalized = Decimal("0.5") + rise_pct / (Decimal("2") * self.ai_cap_pct)
        return max(Decimal("0"), min(Decimal("1"), normalized))

    # ── Public: raw % → AI 점수 변환 (리뷰 C2) ──────────
    def normalize_rise_pct_to_score(self, rise_pct: Decimal | None) -> Decimal:
        """raw rise percentage (%, 예: 20.0) → AI score (0~max_ai).

        ADR 0006 §2.3 선형 연속 매핑 (0.5 컷오프 제거):
          ai_score = normalized_from_rise_pct(rise_pct) * max_ai
        (중립 0% → 5.0, +20% → 10, -20% → 0. 하락도 연속 저점수.)
        """
        normalized = self.normalized_from_rise_pct(rise_pct)
        if normalized is None:
            return Decimal("0")
        return self._score_from_clipped_normalized(normalized)

    # ── Public: tech indicators → tech score ─────────────
    def tech_score_from_indicators(self, indicators: dict[str, Any] | None) -> Decimal:
        if not indicators:
            return Decimal("0")
        cfg = self.axes["technical"]
        comps = cfg["components"]
        s = Decimal("0")
        if indicators.get("golden_cross"):
            s += Decimal(str(comps["golden_cross"]))
        rsi = indicators.get("rsi")
        if rsi is not None and Decimal(str(rsi)) < self.rsi_threshold:
            s += Decimal(str(comps["rsi_below_threshold"]))
        if indicators.get("macd_buy_signal"):
            s += Decimal(str(comps["macd_buy_signal"]))
        return s.quantize(_QTZ_2, rounding=ROUND_HALF_UP)

    def count_tech_signals(self, indicators: dict[str, Any] | None) -> int:
        if not indicators:
            return 0
        cnt = 0
        if indicators.get("golden_cross"):
            cnt += 1
        rsi = indicators.get("rsi")
        if rsi is not None and Decimal(str(rsi)) < self.rsi_threshold:
            cnt += 1
        if indicators.get("macd_buy_signal"):
            cnt += 1
        return cnt

    # ── Public: normalized AI probability (0~1) → AI score ──────
    def ai_score_from_normalized(self, normalized: Decimal | None) -> Decimal:
        """이미 normalized 된 rise_probability (0~1) → AI score (0~max_ai).

        sync_service 가 이미 normalized 한 값을 가지고 있는 경우 사용.
        raw rise_pct 가 있다면 normalize_rise_pct_to_score() 사용.
        """
        if normalized is None:
            return Decimal("0")
        return self._score_from_clipped_normalized(Decimal(str(normalized)))

    # ── Public: sentiment (raw) → sentiment score ─────
    def sentiment_score_from_raw(self, sentiment: Decimal | None) -> Decimal:
        """raw sentiment (Alpha Vantage, 실범위 ±raw_clip) → sentiment score (0~max).

        ADR 0006 §2.3: 선형 매핑 — 중립(raw 0)은 중간 점수.
          score = (clip(raw, -clip, +clip) + clip) / (2*clip) * max
        (raw 0 → max/2 = 5.0/중립, +0.35 → 10, -0.35 → 0.) v<=0 → 0 컷오프 제거.
        """
        if sentiment is None:
            return Decimal("0")
        cfg = self.axes["sentiment"]
        raw_clip = Decimal(str(cfg.get("raw_clip", "0.35")))
        sent_max = Decimal(str(cfg["max"]))
        v = Decimal(str(sentiment))
        clipped = max(-raw_clip, min(raw_clip, v))
        normalized = (clipped + raw_clip) / (Decimal("2") * raw_clip)  # 0~1
        return (normalized * sent_max).quantize(_QTZ_2, rounding=ROUND_HALF_UP)

    # ── Public: compose components → Score ──────────────
    def compose_components(
        self,
        ai_score: Decimal,
        tech_score: Decimal,
        sentiment_score: Decimal,
        has_ai: bool,
        has_tech: bool,
        has_sentiment: bool,
        tech_signal_count: int,
        veto_reasons: tuple[str, ...] = (),
        warnings: tuple[str, ...] = (),
    ) -> Score:
        """이미 normalized 된 component score 들로부터 Score 계산.

        ADR 0006 (0~100 재설계):
          - 각 축 0~1 정규화(score/max) 후 present 축만 weight 가중합.
          - 결측 축 제외 후 남은 weight 를 합=1 로 재정규화 → ×100 (composite_max=100).
          - axis_contributions: present 축별 기여 점수 보존 (XAI §2.9).
          - score_coverage: present 축들의 원본 weight 합 (재정규화 전).
          - coverage guard (§2.4): has_tech=False 거나 (available_axes<2 AND coverage<0.8) 면 추천 불가.

        PR 3b (2026-05-22): veto_reasons 비어있지 않고 spec.negative_policy=='veto' 면
        composite_score=0, grade='D', label='NONE' 강제. caller 가 raw 신호 부호 판단해서 전달.
        """
        ax = self.axes
        w_ai = Decimal(str(ax["ai"]["weight"]))
        w_tech = Decimal(str(ax["technical"]["weight"]))
        w_sent = Decimal(str(ax["sentiment"]["weight"]))
        max_ai = Decimal(str(ax["ai"]["max"]))
        max_tech = Decimal(str(ax["technical"]["max"]))
        max_sent = Decimal(str(ax["sentiment"]["max"]))

        effective_ai = ai_score if has_ai else Decimal("0")
        effective_tech = tech_score if has_tech else Decimal("0")
        effective_sent = sentiment_score if has_sentiment else Decimal("0")

        # present 축: (원본 weight, 정규화값 0~1, 축 이름)
        present_axes: list[tuple[str, Decimal, Decimal]] = []
        if has_ai:
            present_axes.append(("ai", w_ai, ai_score / max_ai if max_ai > 0 else Decimal("0")))
        if has_tech:
            present_axes.append(("tech", w_tech, tech_score / max_tech if max_tech > 0 else Decimal("0")))
        if has_sentiment:
            present_axes.append(("sentiment", w_sent, sentiment_score / max_sent if max_sent > 0 else Decimal("0")))

        # 결측 재정규화: 남은 weight 합으로 나눠 합=1 보장.
        weight_present = sum((w for _, w, _ in present_axes), Decimal("0"))
        score_coverage = weight_present  # 원본 weight 합 (재정규화 전) = 커버리지

        composite = Decimal("0")
        axis_contributions: dict[str, Decimal] = {}
        if weight_present > 0:
            for name, w, norm in present_axes:
                contrib = (w / weight_present) * norm * Decimal("100")
                axis_contributions[name] = contrib.quantize(_QTZ_2, rounding=ROUND_HALF_UP)
                composite += contrib

        composite_max = self.composite_max

        # PR 3b: veto 발동 시 composite 강제 0. spec 의 negative_policy 가 veto 인 경우만 실행 (rollback safety).
        veto_active = bool(veto_reasons) and self.is_negative_veto_enabled()
        if veto_active:
            composite = Decimal("0")
            axis_contributions = {}

        confidence = (
            (composite / composite_max).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            if composite_max > 0
            else Decimal("0")
        )

        missing = tuple(
            name
            for name, present in [
                ("ai", has_ai),
                ("tech", has_tech),
                ("sentiment", has_sentiment),
            ]
            if not present
        )

        # coverage guard (ADR 0006 §2.4): tech 없으면 추천 불가.
        # is_recommended = has_tech AND (available_axes >= 2 OR coverage >= 0.8)
        available_axes = len(present_axes)
        is_recommended = has_tech and (
            available_axes >= 2 or score_coverage >= Decimal("0.8")
        )
        if veto_active:
            is_recommended = False

        if veto_active:
            grade = "D"
            label = "NONE"
        else:
            grade = self.grade_for_composite(composite)
            if is_recommended:
                label = self.label_from_confidence_and_signals(confidence, tech_signal_count)
            else:
                # 추천 불가 → label NONE 강제 (점수/grade 는 노출).
                label = "NONE"

        # spec.negative_policy=='zero' 일 때는 caller 가 veto_reasons 전달해도 무시 (rollback safety).
        # 즉 zero 모드에선 항상 veto_reasons=() 반환.
        final_veto = veto_reasons if self.is_negative_veto_enabled() else ()

        return Score(
            ai_score=effective_ai.quantize(_QTZ_2, rounding=ROUND_HALF_UP),
            tech_score=effective_tech.quantize(_QTZ_2, rounding=ROUND_HALF_UP),
            sentiment_score=effective_sent.quantize(_QTZ_2, rounding=ROUND_HALF_UP),
            composite_score=composite.quantize(_QTZ_2, rounding=ROUND_HALF_UP),
            composite_max=composite_max.quantize(_QTZ_2, rounding=ROUND_HALF_UP),
            confidence=confidence,
            grade=grade,
            recommendation_label=label,
            missing_axes=missing,
            veto_reasons=final_veto,
            warnings=warnings,
            axis_contributions=axis_contributions,
            score_coverage=score_coverage.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP),
            is_recommended=is_recommended,
        )

    # score_from_raw_signals 헬퍼는 cleanup PR (2026-05-22) 에서 제거됨.
    # prod path (sync_service / buy_criteria) 는 자체 veto detection 후 compose_components 직접 호출 —
    # 호출처가 raw 신호의 부호를 알아야 일관 처리 가능. test 도 compose_components 직접 사용.

    # ── Grade / Label ───────────────────────────────────
    def grade_for_composite(self, composite: Decimal) -> str:
        for g in ["S", "A", "B", "C"]:
            if composite >= Decimal(str(self._spec["grades"][g]["min"])):
                return g
        return "D"

    def label_from_confidence_and_signals(
        self, confidence: Decimal, tech_signals: int
    ) -> str:
        """confidence(0~1) → 추천 라벨 key (STRONG/RECOMMEND/WATCH/NONE).

        임계 SSoT = spec.recommendation_labels (ADR 0006 §2.8 — 하드코딩 금지).
        RecommendationGrade.from_scores() 가 본 메서드에 위임한다.
        tech_signals 는 게이트가 아니다(2026-06-10 재보정 — tech 는 composite 50% weight 로
        이미 반영, 이중 반영 금지). 파라미터는 향후 확장 예약으로만 유지.
        """
        labels = self._spec["recommendation_labels"]
        for key in ["STRONG", "RECOMMEND", "WATCH"]:
            cfg = labels.get(key) or {}
            min_c = Decimal(str(cfg.get("min_confidence", 0)))
            if confidence >= min_c:
                return key
        return "NONE"

    # ── Public: label_metadata (PR 2, label/emoji SSoT) ──
    def label_metadata(self, key: str) -> dict[str, str]:
        """recommendation_labels[key] 의 label/emoji 를 반환. unknown key 는 NONE 매핑."""
        labels = self._spec.get("recommendation_labels") or {}
        cfg = labels.get(key) or labels.get("NONE") or {}
        return {
            "label": cfg.get("label", "추천 없음"),
            "emoji": cfg.get("emoji", "⚪"),
        }
