from hashlib import sha256
import json
from dataclasses import replace
from math import isfinite
from typing import Any, Callable, Iterable, Mapping, NamedTuple

from .contracts import NightAudit, ReplayVerdict, WaiverRoster, WaiverRule, WaiverTicket

RISK_LINE = 25.0


def _compact(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def waiver_fingerprint(*parts: Any) -> str:
    return sha256(_compact(parts).encode('utf-8')).hexdigest()[:16]


def _first(obj: Any, names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None or name in {'outcome', 'day', 'upper'}:
                return value
    return default


def raw_of(point: Any) -> Any:
    return _first(point, ('raw', 'source', 'prediction', 'raw_point'), point)


def point_key(point: Any) -> str:
    value = _first(point, ('key', '单号', 'id', 'ticket_key'))
    if value is None:
        raw = raw_of(point)
        value = _first(raw, ('key', '单号', 'id', 'ticket_key'))
    if value is None:
        raise ValueError('免测单据缺少单号')
    return str(value)


def point_day(point: Any) -> int | None:
    for candidate in (point, raw_of(point)):
        value = _first(candidate, ('day', 'night', '夜次', '测量工日', '工作日', 'work_day'))
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            return value
    return None


def point_cohort(point: Any) -> str | None:
    for candidate in (point, raw_of(point)):
        value = _first(candidate, ('cohort', 'zone', '作业区', '分组'))
        if value is not None:
            return str(value)
    return None


def point_estimate(point: Any) -> float:
    value = _first(point, ('estimate', 'score', 'predicted', '估计', '预测'))
    if value is None:
        value = _first(raw_of(point), ('score', 'estimate', 'predicted', '估计'))
    if value is None:
        raise ValueError('免测规则缺少回归估计')
    value = float(value)
    if not isfinite(value):
        raise ValueError('免测估计必须有限')
    return value


def point_upper(point: Any) -> float | None:
    value = _first(point, ('upper', 'upper_bound', '上限', '区间上限'))
    if value is None:
        value = _first(raw_of(point), ('upper', 'upper_bound', '上限'))
    if value is None:
        return None
    value = float(value)
    return value if isfinite(value) else None


def point_lower(point: Any) -> float | None:
    value = _first(point, ('lower', 'lower_bound', '下限', '区间下限'))
    if value is None:
        value = _first(raw_of(point), ('lower', 'lower_bound', '下限'))
    if value is None:
        return None
    value = float(value)
    return value if isfinite(value) else None


def point_uncertain(point: Any) -> bool:
    value = _first(point, ('uncertain', 'needs_review', 'manual_review'))
    if isinstance(value, bool):
        return value
    return False


def point_outcome(point: Any) -> float | None:
    for candidate in (point, raw_of(point)):
        value = _first(candidate, ('outcome', 'actual', '实绩', 'actual_outcome', 'observed_outcome'))
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def point_observed(point: Any) -> bool:
    for candidate in (point, raw_of(point)):
        value = _first(candidate, ('observed', 'is_observed', '实测'))
        if isinstance(value, bool):
            return value
    return True


def point_input_values(point: Any) -> tuple[float, ...] | None:
    raw = raw_of(point)
    value = _first(raw, ('input_values', 'measurements', 'values'))
    if value is None:
        value = _first(point, ('input_values', 'measurements'))
    if isinstance(value, Mapping):
        try:
            from ..reflection_factors.contracts import RAW_KEYS
            value = tuple(value.get(name) for name in RAW_KEYS)
        except Exception:
            value = tuple(value.values())
    if value is None:
        return None
    try:
        return tuple(float(item) if item is not None else None for item in value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def point_missing(point: Any) -> tuple[str, ...]:
    for candidate in (point, raw_of(point)):
        value = _first(candidate, ('missing', 'unmeasured', 'missing_fields'))
        if value:
            return tuple(str(item) for item in value)
    values = point_input_values(point)
    if values:
        try:
            from ..reflection_factors.contracts import RAW_KEYS
            return tuple(name for name, value in zip(RAW_KEYS, values) if value is None)
        except Exception:
            return ()
    return ()


def point_excursions(point: Any) -> tuple[str, ...]:
    for candidate in (point, raw_of(point)):
        value = _first(candidate, ('excursions', 'out_of_domain', 'domain_flags'))
        if value:
            return tuple(str(item) for item in value)
    return ()


def point_rule_hits(point: Any) -> tuple[str, ...]:
    value = _first(point, ('rule_hits', 'hits', 'rules'))
    if value:
        out = []
        for item in value:
            if isinstance(item, str):
                out.append(item)
            else:
                code = _first(item, ('rule_id', 'code', 'id'))
                if code:
                    out.append(str(code))
        return tuple(out)
    values = point_input_values(point)
    if values and len(values) == 6:
        try:
            from ..retrocheck_slots.service import signals
            return tuple(notice.rule_id for notice in signals(values, point_estimate(point), point_missing(point)))
        except Exception:
            retro, age, wet, traffic, beads, surface = values
            hits = []
            if None not in (retro, wet) and wet >= 75 and retro <= 140:
                hits.append('WET_LOW_LIGHT')
            if None not in (age, traffic) and age >= 300 and traffic >= 40:
                hits.append('LONG_SERVICE')
            if None not in (retro, beads) and beads < 210 and retro < 230:
                hits.append('BEAD_SUPPLY')
            return tuple(hits)
    return ()


def normalize_limit(limit: Any, *, default: float | None = None, allow_none: bool = True) -> float | None:
    if limit is None:
        if allow_none:
            return default
        raise ValueError('漏判夜上限不能为空')
    if isinstance(limit, bool):
        raise ValueError('漏判夜上限必须是数值')
    if isinstance(limit, int) and not isinstance(limit, bool):
        # A count is handled separately; here integers 0/1 are valid ratios.
        value = float(limit)
    else:
        try:
            value = float(limit)
        except (TypeError, ValueError) as error:
            raise ValueError('漏判夜上限必须是 0 到 1 的比例') from error
    if not isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError('漏判夜占比上限应处于 [0,1]')
    return value


def _rule_from(rule: Any, *, line: float = RISK_LINE, limit: float | None = None) -> WaiverRule:
    if rule is None:
        return WaiverRule(line=line, max_estimate=line)
    if isinstance(rule, WaiverRule):
        return rule
    if isinstance(rule, (int, float)) and not isinstance(rule, bool):
        return WaiverRule(line=line, max_estimate=float(rule))
    if isinstance(rule, Mapping):
        max_estimate = _first(rule, ('max_estimate', 'cutoff', 'threshold', 'estimate_cutoff'), line)
        return WaiverRule(
            line=float(_first(rule, ('line', 'risk_line'), line)),
            max_estimate=float(max_estimate),
            require_upper_below_line=bool(_first(rule, ('require_upper_below_line', 'use_upper_bound'), False)),
            require_certain=bool(_first(rule, ('require_certain',), True)),
            require_clean_domain=bool(_first(rule, ('require_clean_domain', 'clean_only'), True)),
        )
    raise TypeError('未知的免测入选规则')


def is_eligible(
    point: Any,
    rule: Any = None,
    *,
    line: float = RISK_LINE,
    attention: Iterable[str] | None = None,
    candidate_keys: Iterable[str] | None = None,
) -> bool:
    """Apply the frozen calibration rule to one online/historical point."""
    if candidate_keys is not None:
        return point_key(point) in frozenset(map(str, candidate_keys))

    policy = _rule_from(rule, line=line)
    try:
        estimate = point_estimate(point)
    except (ValueError, TypeError):
        return False
    if not isfinite(estimate) or estimate < 0 or estimate >= policy.line:
        return False
    if estimate > policy.max_estimate + 1e-12:
        return False
    if policy.require_certain and point_uncertain(point):
        return False
    upper = point_upper(point)
    if policy.require_upper_below_line:
        if upper is None or not upper < policy.line:
            return False
    if policy.require_clean_domain:
        if point_excursions(point):
            return False
        if len(point_missing(point)) >= 2:
            return False
    if attention is not None and point_key(point) in frozenset(map(str, attention)):
        return False
    blocked = frozenset(policy.reject_rule_hits)
    if blocked and blocked.intersection(point_rule_hits(point)):
        return False
    return True


def _night_number(point: Any, fallback: int) -> int:
    day = point_day(point)
    return fallback if day is None else day


def audit_nights(
    points: Iterable[Any],
    rule: Any = None,
    *,
    line: float = RISK_LINE,
    limit: float | None = None,
    attention: Iterable[str] | None = None,
    candidate_keys: Iterable[str] | None = None,
    unreturned_keys: Iterable[str] | Mapping[str, str] | None = None,
    late_keys: Iterable[str] | None = None,
) -> tuple[NightAudit, ...]:
    """Aggregate selected tickets into one verdict per measurement night.

    A night is missed at most once even when several waived tickets fail.
    ``unreturned_keys`` may be a mapping key -> reason for late/missing reads.
    """
    if candidate_keys is None and isinstance(rule, (set, frozenset, list, tuple)) and all(
            isinstance(item, str) for item in rule):
        candidate_keys = rule
        rule = None
    policy = _rule_from(rule, line=line)
    attention_set = frozenset(map(str, attention or ()))
    explicit_candidates = None if candidate_keys is None else frozenset(map(str, candidate_keys))
    if isinstance(unreturned_keys, Mapping):
        unreturned_set = frozenset(map(str, unreturned_keys.keys()))
    else:
        unreturned_set = frozenset(map(str, unreturned_keys or ()))
    late_set = frozenset(map(str, late_keys or ()))

    grouped: dict[int, list[Any]] = {}
    for index, point in enumerate(points):
        grouped.setdefault(_night_number(point, index), []).append(point)

    audits: list[NightAudit] = []
    for day in sorted(grouped):
        items = grouped[day]
        selected: list[str] = []
        over: list[str] = []
        missing: list[str] = []
        late: list[str] = []
        for point in sorted(items, key=point_key):
            key = point_key(point)
            if not is_eligible(
                point,
                policy,
                line=line,
                attention=attention_set,
                candidate_keys=explicit_candidates,
            ):
                continue
            selected.append(key)
            outcome = point_outcome(point)
            timely = key not in unreturned_set
            if key in late_set:
                late.append(key)
            if not timely or not point_observed(point) or outcome is None:
                missing.append(key)
                continue
            if outcome >= line:
                over.append(key)
        reasons = []
        if over:
            reasons.append('实绩超线')
        if missing:
            reasons.append('实绩未回')
        audit = NightAudit(
            day=day,
            considered=len(items),
            selected_keys=tuple(selected),
            over_line_keys=tuple(over),
            unreturned_keys=tuple(missing),
            late_keys=tuple(key for key in late if key in missing),
            missed=bool(over or missing),
            reasons=tuple(reasons),
        )
        audits.append(audit)
    return tuple(audits)


# Concise alias used by callers expecting a report-like return.
evaluate_nights = audit_nights
night_ledger = audit_nights


def replay_waiver_rule(
    points: Iterable[Any],
    rule: Any = None,
    max_missed_night_rate: Any = None,
    limit: Any = None,
    line: float = RISK_LINE,
    attention: Iterable[str] | None = None,
    candidate_keys: Iterable[str] | None = None,
    unreturned_keys: Iterable[str] | Mapping[str, str] | None = None,
    late_keys: Iterable[str] | None = None,
    max_missed_nights: int | None = None,
    **options: Any,
) -> ReplayVerdict:
    if max_missed_night_rate is None:
        for alias in ('max_missed_night_ratio', 'missed_night_limit', 'missed_night_upper_limit',
                      'foreman_limit', 'waiver_limit', 'night_limit', 'miss_limit'):
            if alias in options and options[alias] is not None:
                max_missed_night_rate = options[alias]
                break
    if line == RISK_LINE and options.get('threshold') is not None:
        line = float(options['threshold'])
    if max_missed_nights is None and options.get('max_missed_night_count') is not None:
        max_missed_nights = options['max_missed_night_count']
    if candidate_keys is None and options.get('exempt_keys') is not None:
        candidate_keys = options['exempt_keys']
    if unreturned_keys is None and options.get('missing_keys') is not None:
        unreturned_keys = options['missing_keys']
    if candidate_keys is None and isinstance(rule, (set, frozenset, list, tuple)) and all(
            isinstance(item, str) for item in rule):
        candidate_keys = rule
        rule = None
    cap = normalize_limit(max_missed_night_rate if max_missed_night_rate is not None else limit)
    if max_missed_nights is not None and (
            isinstance(max_missed_nights, bool) or type(max_missed_nights) is not int or max_missed_nights < 0):
        raise ValueError('允许漏判夜数须为非负整数')
    policy = _rule_from(rule, line=line, limit=cap)
    nights = audit_nights(
        points,
        policy,
        line=line,
        attention=attention,
        candidate_keys=candidate_keys,
        unreturned_keys=unreturned_keys,
        late_keys=late_keys,
    )
    total = len(nights)
    missed = sum(night.missed for night in nights)
    rate = missed / total if total else None
    if total == 0:
        status, passed, reason = 'NO_REPLAY_NIGHTS', False, '没有可回放的夜次'
    elif cap is None and max_missed_nights is None:
        status, passed, reason = 'LIMIT_NOT_SET', False, '班长尚未设定漏判夜占比上限'
    elif cap is not None and rate > cap + 1e-12:
        status, passed, reason = 'REPLAY_REJECTED', False, f'漏判夜占比 {rate:.4f} 超过上限 {cap:g}'
    elif max_missed_nights is not None and missed > max_missed_nights:
        status, passed, reason = 'REPLAY_REJECTED', False, f'漏判夜数 {missed} 超过上限 {max_missed_nights}'
    else:
        status, passed, reason = 'REPLAY_PASSED', True, '独立回放通过'
    return ReplayVerdict(status, line, cap, nights, total, missed, rate, passed, reason, policy)


# Common semantic aliases.
evaluate_replay = replay_waiver_rule
validate_waiver_replay = replay_waiver_rule
replay_exemption_rule = replay_waiver_rule
replay_night_waiver = replay_waiver_rule
run_night_replay = replay_waiver_rule
missed_night_report = replay_waiver_rule
waiver_night_audit = replay_waiver_rule


def calibrate_waiver_rule(
    calibration_points: Iterable[Any],
    max_missed_night_rate: Any = 0.0,
    limit: Any = None,
    line: float = RISK_LINE,
    attention: Iterable[str] | None = None,
    require_upper_below_line: bool = False,
    candidate_keys: Iterable[str] | None = None,
    unreturned_keys: Iterable[str] | Mapping[str, str] | None = None,
    **options: Any,
) -> WaiverRule:
    """Choose the broadest score cutoff satisfying the calibration cap.

    The rule class is intentionally one-dimensional and monotone.  Replay is
    kept independent: this function never reads replay/current points.
    """
    if max_missed_night_rate == 0.0 and limit is not None:
        cap = normalize_limit(limit, default=0.0)
    elif max_missed_night_rate is None:
        alias_value = None
        for alias in ('max_missed_night_ratio', 'missed_night_limit', 'foreman_limit',
                      'waiver_limit', 'night_limit', 'miss_limit'):
            if options.get(alias) is not None:
                alias_value = options[alias]
                break
        chosen = alias_value if alias_value is not None else limit
        cap = normalize_limit(chosen, default=0.0)
    else:
        cap = normalize_limit(max_missed_night_rate, default=0.0)
    points = tuple(calibration_points)
    attention_set = frozenset(map(str, attention or ()))
    explicit = None if candidate_keys is None else frozenset(map(str, candidate_keys))
    if isinstance(unreturned_keys, Mapping):
        unreturned_set = frozenset(map(str, unreturned_keys.keys()))
    else:
        unreturned_set = frozenset(map(str, unreturned_keys or ()))

    clean: list[Any] = []
    scores: list[float] = []
    for point in points:
        if explicit is not None and point_key(point) not in explicit:
            continue
        try:
            score = point_estimate(point)
        except (ValueError, TypeError):
            continue
        if not isfinite(score) or score < 0 or score >= line:
            continue
        if point_uncertain(point) or point_excursions(point) or len(point_missing(point)) >= 2:
            continue
        if attention_set and point_key(point) in attention_set:
            continue
        if {'WET_LOW_LIGHT', 'LONG_SERVICE', 'BEAD_SUPPLY'}.intersection(point_rule_hits(point)):
            continue
        upper = point_upper(point)
        if require_upper_below_line and (upper is None or upper >= line):
            continue
        clean.append(point)
        scores.append(score)

    all_nights = {_night_number(point, index) for index, point in enumerate(points)}
    night_count = len(all_nights)

    def rate_for(max_score: float) -> tuple[int, int, int]:
        selected = [point for point in clean if point_estimate(point) <= max_score + 1e-12]
        nights = audit_nights(selected, WaiverRule(line=line, max_estimate=max_score,
                                                  reject_rule_hits=(), require_certain=False,
                                                  require_clean_domain=False), line=line,
                              unreturned_keys=unreturned_set)
        missed = sum(night.missed for night in nights)
        return len(selected), missed, night_count

    best_score: float | None = None
    best_count = -1
    best_missed = 0
    # Ascending sweep: a later feasible cutoff necessarily waives at least as
    # many tickets.  Ties resolve to the lower fingerprint input by key order.
    for score in sorted(set(scores)):
        count, missed, total = rate_for(score)
        rate = missed / total if total else 0.0
        if total and rate <= cap + 1e-12 and count >= best_count:
            best_score, best_count, best_missed = score, count, missed
    if best_score is None:
        # No positive waiver set is calibration-safe.  Keep an impossible
        # cutoff so online eligibility is empty, while preserving diagnostics.
        best_score = -0.0
        best_count = 0
        best_missed = 0
    cal_rate = best_missed / night_count if night_count else None
    fingerprint = waiver_fingerprint(
        'linebeads-waiver-rule-v1', line, best_score, bool(require_upper_below_line), cap,
        tuple(sorted(point_key(point) for point in clean if point_estimate(point) <= best_score + 1e-12)),
    )
    return WaiverRule(
        line=line,
        max_estimate=best_score,
        require_upper_below_line=require_upper_below_line,
        calibration_nights=night_count,
        calibration_missed_nights=best_missed,
        calibration_missed_rate=cal_rate,
        waived_count=max(0, best_count),
        fingerprint=fingerprint,
    )


build_waiver_rule = calibrate_waiver_rule
establish_waiver_rule = calibrate_waiver_rule
fit_exemption_rule = calibrate_waiver_rule
calibrate_night_waiver = calibrate_waiver_rule
calibrate_exemption_policy = calibrate_waiver_rule
build_exemption_policy = calibrate_waiver_rule
derive_waiver_rule = calibrate_waiver_rule
fit_waiver_policy = calibrate_waiver_rule


def ticket_from_point(point: Any, rule: WaiverRule | None = None) -> WaiverTicket:
    key = point_key(point)
    reasons: list[str] = []
    hits = point_rule_hits(point)
    if hits:
        reasons.extend(hits)
    if rule and rule.require_upper_below_line:
        reasons.append('区间上限低于实绩线')
    if not hits:
        reasons.append('校准入选规则')
    estimate = round(point_estimate(point), 6)
    upper = point_upper(point)
    values = point_input_values(point)
    nominal_cost = 1 + int(values[3] >= 38) if values and len(values) == 6 and values[3] is not None else 1
    return WaiverTicket(
        key=key,
        day=point_day(point),
        cohort=point_cohort(point),
        estimate=estimate,
        upper=None if upper is None else round(upper, 6),
        cost=0,
        reasons=tuple(dict.fromkeys(reasons)),
        evidence={'原估工位量': nominal_cost,
                  '缺测': '、'.join(point_missing(point)) or '—',
                  '外推标记': '、'.join(point_excursions(point)) or '—'},
    )


def sign_waiver_roster(
    current_points: Iterable[Any],
    replay: ReplayVerdict | None = None,
    rule: Any = None,
    max_missed_night_rate: Any = None,
    limit: Any = None,
    line: float = RISK_LINE,
    attention: Iterable[str] | None = None,
    candidate_keys: Iterable[str] | None = None,
    batch_hash: str = '',
    feature_stamp: str = '',
    model_stamp: str = '',
    capacity: int | None = None,
    mode: str | None = None,
    force: bool = False,
    max_missed_nights: int | None = None,
    **extra: Any,
) -> WaiverRoster:
    """Freeze and sign the current shift roster if replay passes."""
    cap = normalize_limit(max_missed_night_rate if max_missed_night_rate is not None else limit,
                          default=(replay.limit if replay is not None else None))
    if cap is None:
        for alias in ('max_missed_night_ratio', 'missed_night_limit', 'foreman_limit',
                      'waiver_limit', 'night_limit', 'miss_limit'):
            if extra.get(alias) is not None:
                cap = normalize_limit(extra[alias])
                break
    if candidate_keys is None and extra.get('exempt_keys') is not None:
        candidate_keys = extra['exempt_keys']
    if candidate_keys is None and isinstance(rule, (set, frozenset, list, tuple)) and all(
            isinstance(item, str) for item in rule):
        candidate_keys = rule
        rule = None
    policy = rule or (replay.rule if replay is not None else WaiverRule(line=line, max_estimate=line))
    if replay is None:
        # Convenience for unit callers: an empty replay is explicitly unsafe.
        replay = replay_waiver_rule((), policy, max_missed_night_rate=cap, line=line,
                                    max_missed_nights=max_missed_nights)
    elif max_missed_nights is not None and replay.passed and replay.missed_nights > max_missed_nights:
        replay = replace(
            replay,
            passed=False,
            status='REPLAY_REJECTED',
            reason=f'漏判夜数 {replay.missed_nights} 超过上限 {max_missed_nights}',
        )

    eligible_points = [
        point for point in current_points
        if is_eligible(point, policy, line=line, attention=attention, candidate_keys=candidate_keys)
    ]
    eligible_points.sort(key=lambda point: (point_day(point) if point_day(point) is not None else 10**9,
                                            point_key(point)))
    tickets = tuple(ticket_from_point(point, policy) for point in eligible_points)
    keys = tuple(ticket.key for ticket in tickets)
    # The safety roster is independent of how many station slots are available;
    # capacity/mode therefore do not participate in its idempotency identity.
    idempotency_key = waiver_fingerprint(
        batch_hash, feature_stamp, model_stamp, line, cap,
        policy.fingerprint or policy.public(), keys,
    )
    if not replay.passed and not force:
        return WaiverRoster(
            False,
            replay.status,
            line,
            cap,
            policy,
            replay,
            (),
            '',
            idempotency_key,
            replay.reason or '独立回放未通过',
        )
    if not tickets:
        return WaiverRoster(
            False,
            'NO_ELIGIBLE_TICKETS',
            line,
            cap,
            policy,
            replay,
            (),
            '',
            idempotency_key,
            '没有符合校准入选规则的当班单',
        )
    roster_id = 'WV-' + idempotency_key
    return WaiverRoster(True, 'SIGNED', line, cap, policy, replay, tickets, roster_id, idempotency_key, '')


issue_waiver_roster = sign_waiver_roster
sign_exemption_roster = sign_waiver_roster
issue_exemption_roster = sign_waiver_roster
sign_night_waiver = sign_waiver_roster
sign_waiver = sign_waiver_roster
issue_night_waiver_roster = sign_waiver_roster
approve_waiver_roster = sign_waiver_roster


class IssuanceResult(NamedTuple):
    roster: WaiverRoster
    created: bool
    duplicate: bool


class WaiverIssuanceRegister:
    """In-process idempotency register for explicit signing actions."""

    def __init__(self) -> None:
        self._issued: dict[str, WaiverRoster] = {}

    def issue_once(self, roster: WaiverRoster) -> IssuanceResult:
        if not roster.signed or not roster.idempotency_key:
            return IssuanceResult(roster, False, False)
        existing = self._issued.get(roster.idempotency_key)
        if existing is not None:
            return IssuanceResult(existing, False, True)
        self._issued[roster.idempotency_key] = roster
        return IssuanceResult(roster, True, False)

    def issue(self, roster: WaiverRoster) -> tuple[WaiverRoster, bool]:
        result = self.issue_once(roster)
        return result.roster, result.created

    def is_issued(self, key: str) -> bool:
        return key in self._issued

    def get(self, key: str) -> WaiverRoster | None:
        return self._issued.get(key)

    def __len__(self) -> int:
        return len(self._issued)


def waiver_evidence(roster: WaiverRoster) -> list[dict[str, Any]]:
    report = roster.public()
    rows = [
        {'核对项': '签发状态', '数量': report['签发状态']},
        {'核对项': '免测单量', '数量': report['count']},
        {'核对项': '可回放夜次', '数量': report['replay']['replayable_nights'] if report['replay'] else 0},
        {'核对项': '漏判夜数', '数量': report['replay']['missed_nights'] if report['replay'] else 0},
        {'核对项': '漏判夜占比', '数量': report['missed_night_limit']},
        {'核对项': '签发凭证', '数量': report['roster_id'] or '未签发'},
    ]
    return rows


nightly_reconciliation = audit_nights
reconcile_nights = audit_nights
calculate_missed_nights = audit_nights
evaluate_waiver_policy = replay_waiver_rule
check_waiver_policy = replay_waiver_rule
select_waiver_candidates = calibrate_waiver_rule
build_waiver_roster = sign_waiver_roster
create_waiver_roster = sign_waiver_roster
sign_waiver_list = sign_waiver_roster
issue_waiver_list = sign_waiver_roster
sign_no_retest_list = sign_waiver_roster
issue_no_retest_list = sign_waiver_roster
WaiverPolicy = WaiverRule
WaiverDecision = WaiverRoster
ExemptionRule = WaiverRule
ExemptionRoster = WaiverRoster


def _split_line_and_limit(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[float, Any]:
    line = float(kwargs.pop('line', kwargs.pop('threshold', RISK_LINE)))
    limit = kwargs.pop('max_missed_night_rate',
                       kwargs.pop('limit', kwargs.pop('missed_night_limit', None)))
    numbers = []
    for value in args:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            numbers.append(float(value))
        else:
            kwargs.setdefault('rule', value)
    for value in numbers:
        if 0.0 <= value <= 1.0 and limit is None:
            limit = value
        elif value > 1.0 and 'line' not in kwargs:
            line = value
        elif limit is None:
            limit = value
    return line, limit


def evaluate_waivers(points: Iterable[Any], *args: Any, **kwargs: Any) -> ReplayVerdict:
    """Permissive entry point: numbers may be supplied as cap and/or line."""
    line, limit = _split_line_and_limit(args, kwargs)
    return replay_waiver_rule(points, line=line, max_missed_night_rate=limit, **kwargs)


waiver_report = evaluate_waivers
nightly_waiver_report = evaluate_waivers
evaluate_night_waivers = evaluate_waivers
evaluate_waiver = evaluate_waivers
evaluate_exemptions = evaluate_waivers
reconciliation_report = evaluate_waivers


def sign_waivers(current_points: Iterable[Any], replay: Any = None, *args: Any, **kwargs: Any) -> WaiverRoster:
    line, limit = _split_line_and_limit(args, kwargs)
    if isinstance(replay, ReplayVerdict):
        verdict = replay
    elif replay is None:
        verdict = None
    else:
        # Allow sign_waivers(live, replay_points, .1, 25).
        verdict = evaluate_waivers(replay, *args, line=line, max_missed_night_rate=limit, **{k: v for k, v in kwargs.items() if k in {'rule', 'attention', 'candidate_keys', 'unreturned_keys'}})
        args = ()
    return sign_waiver_roster(current_points, verdict, line=line, max_missed_night_rate=limit, **kwargs)


issue_waivers = sign_waivers
approve_waivers = sign_waivers
