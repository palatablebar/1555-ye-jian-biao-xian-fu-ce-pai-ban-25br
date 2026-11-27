from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class NightAudit:
    """One calendar/measurement night in the independent replay."""

    day: int
    considered: int = 0
    selected_keys: tuple[str, ...] = ()
    over_line_keys: tuple[str, ...] = ()
    unreturned_keys: tuple[str, ...] = ()
    late_keys: tuple[str, ...] = ()
    missed: bool = False
    reasons: tuple[str, ...] = ()

    # Compatibility aliases for callers using “waived” terminology.
    @property
    def night(self) -> int:
        return self.day

    @property
    def keys(self) -> tuple[str, ...]:
        return self.selected_keys

    @property
    def waived_keys(self) -> tuple[str, ...]:
        return self.selected_keys

    @property
    def over_keys(self) -> tuple[str, ...]:
        return self.over_line_keys

    @property
    def missing_keys(self) -> tuple[str, ...]:
        return self.unreturned_keys

    @property
    def missed_night(self) -> bool:
        return self.missed

    @property
    def is_missed(self) -> bool:
        return self.missed

    @property
    def failed(self) -> bool:
        return self.missed

    def evidence(self) -> dict[str, Any]:
        return {
            '夜次': self.day,
            '可判单数': self.considered,
            '免测单数': len(self.selected_keys),
            '免测单': '、'.join(self.selected_keys) or '—',
            '实绩超线单': '、'.join(self.over_line_keys) or '—',
            '未回单': '、'.join(self.unreturned_keys) or '—',
            '迟回单': '、'.join(self.late_keys) or '—',
            '漏判夜': '是' if self.missed else '否',
            '漏判原因': '、'.join(self.reasons) or '—',
        }

    def to_dict(self) -> dict[str, Any]:
        return self.evidence()


@dataclass(frozen=True, slots=True)
class WaiverRule:
    """Frozen calibration-time inclusion rule.

    ``max_estimate`` is inclusive.  ``line`` is the actual-decay service line;
    an estimate at/above that line is never waived.  When an interval is
    available, callers may additionally require ``upper < line``.
    """

    line: float = 25.0
    max_estimate: float = 25.0
    require_upper_below_line: bool = False
    require_certain: bool = True
    require_clean_domain: bool = True
    reject_rule_hits: tuple[str, ...] = ('WET_LOW_LIGHT', 'LONG_SERVICE', 'BEAD_SUPPLY')
    calibration_nights: int = 0
    calibration_missed_nights: int = 0
    calibration_missed_rate: float | None = None
    waived_count: int = 0
    method: str = 'calibrated_night_cutoff'
    fingerprint: str = ''

    @property
    def cutoff(self) -> float:
        # The historical API language is “below cutoff”; expose the inclusive
        # calibrated boundary under both names.
        return self.max_estimate

    @property
    def estimate_cutoff(self) -> float:
        return self.max_estimate

    @property
    def threshold(self) -> float:
        return self.max_estimate

    def public(self) -> dict[str, Any]:
        return {
            '规则版本': self.fingerprint or 'uncalibrated',
            '方法': self.method,
            '实绩线': self.line,
            '估计上限': round(float(self.max_estimate), 6),
            '要求区间上限低于实绩线': self.require_upper_below_line,
            '拒绝未校准判断': self.require_certain,
            '拒绝外推/缺测域': self.require_clean_domain,
            '拒绝规则码': list(self.reject_rule_hits),
            '校准夜次': self.calibration_nights,
            '校准漏判夜': self.calibration_missed_nights,
            '校准漏判夜占比': self.calibration_missed_rate,
            '校准入选单量': self.waived_count,
        }


@dataclass(frozen=True, slots=True)
class ReplayVerdict:
    status: str
    line: float
    limit: float | None
    nights: tuple[NightAudit, ...] = ()
    replayable_nights: int = 0
    missed_nights: int = 0
    missed_night_rate: float | None = None
    passed: bool = False
    reason: str = ''
    rule: WaiverRule | None = None

    @property
    def total_nights(self) -> int:
        return self.replayable_nights

    @property
    def rate(self) -> float | None:
        return self.missed_night_rate

    @property
    def missed_night_ratio(self) -> float | None:
        return self.missed_night_rate

    @property
    def allow_signing(self) -> bool:
        return self.passed

    @property
    def issuable(self) -> bool:
        return self.passed

    def evidence(self) -> list[dict[str, Any]]:
        summary = {
            '核验': self.status,
            '可回放夜次': self.replayable_nights,
            '漏判夜数': self.missed_nights,
            '漏判夜占比': self.missed_night_rate,
            '班长上限': self.limit,
            '独立回放结论': '通过' if self.passed else '暂不签发',
            '原因': self.reason or '—',
        }
        return [summary, *(night.evidence() for night in self.nights)]

    def to_dict(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'line': self.line,
            'limit': self.limit,
            'replayable_nights': self.replayable_nights,
            'missed_nights': self.missed_nights,
            'missed_night_rate': self.missed_night_rate,
            'passed': self.passed,
            'reason': self.reason,
            'nights': [item.evidence() for item in self.nights],
        }


@dataclass(frozen=True, slots=True)
class WaiverTicket:
    key: str
    day: int | None
    cohort: str | None
    estimate: float
    upper: float | None
    cost: int = 0
    reasons: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            '单号': self.key,
            '夜次': self.day,
            '作业区': self.cohort,
            '估计': self.estimate,
            '区间上限': self.upper,
            '工位用量': self.cost,
            '入选依据': '、'.join(self.reasons) or '校准入选规则',
            **self.evidence,
        }


@dataclass(frozen=True, slots=True)
class WaiverRoster:
    signed: bool
    status: str
    line: float
    limit: float | None
    rule: WaiverRule | None
    replay: ReplayVerdict | None
    tickets: tuple[WaiverTicket, ...] = ()
    roster_id: str = ''
    idempotency_key: str = ''
    reason: str = ''

    @property
    def issued(self) -> bool:
        return self.signed

    @property
    def approved(self) -> bool:
        return self.signed

    @property
    def signature(self) -> str:
        return self.roster_id

    @property
    def stamp(self) -> str:
        return self.roster_id

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(ticket.key for ticket in self.tickets)

    @property
    def waiver_keys(self) -> frozenset[str]:
        return frozenset(self.keys)

    def public(self) -> dict[str, Any]:
        return {
            'signed': self.signed,
            'issued': self.signed,
            'status': self.status,
            '签发状态': '已签发' if self.signed else '暂不签发',
            'roster_id': self.roster_id or None,
            'waiver_id': self.roster_id or None,
            'idempotency_key': self.idempotency_key or None,
            'line': self.line,
            'missed_night_limit': self.limit,
            'count': len(self.tickets),
            'keys': list(self.keys),
            'reason': self.reason,
            'rule': self.rule.public() if self.rule else None,
            'replay': self.replay.to_dict() if self.replay else None,
            'night_ledger': [night.evidence() for night in self.replay.nights] if self.replay else [],
            'tickets': [ticket.public() for ticket in self.tickets],
        }
