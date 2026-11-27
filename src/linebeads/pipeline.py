from dataclasses import replace
import json
from pathlib import Path

from .lane_samples import service as intake_service
from .surface_windows import service as cohort_service
from .reflection_factors import service as feature_service
from .bead_kernel import service as model_service
from .night_accuracy import service as calibration_service
from .seasonal_mix import service as monitor_service
from .retrocheck_slots import service as dispatch_service
from .marking_evidence import service as audit_service
from .night_waiver import service as waiver_service
from .night_waiver.service import (
    calibrate_waiver_rule,
    evaluate_waivers,
    replay_waiver_rule,
    sign_waiver_roster,
    sign_waiver,
    sign_waivers,
)
from .surface_windows import contracts as window_contract

PRESET = Path(__file__).resolve().parents[2] / 'sample_data' / 'lane_samples_sample.json'
MODULES = (('lane_samples', '路段测点'),
 ('surface_windows', '铺面时段'),
 ('reflection_factors', '反光因素'),
 ('bead_kernel', '反光核估计'),
 ('night_accuracy', '夜测回放'),
 ('seasonal_mix', '季节分组'),
 ('retrocheck_slots', '复测工位'),
 ('marking_evidence', '标线留证'))
DISPLAY = {'title': '夜间标线复测排班',
 'object_name': '标线路段',
 'unit': '衰减分',
 'role': '道路养护测量班长',
 'endpoint': 'linebeads',
 'identity': 'NIGHT SURVEY',
 'location': '道路养护班 · 夜间巡线',
 'visual': '分线复测工位',
 'queue': '路段夜测排班',
 'resource': '便携测量工位',
 'action': '分配便携复测仪',
 'lane': 'L15',
 'field_title': '标线干面对照单'}


def open_fixture(path=PRESET):
    document = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or not isinstance(document.get('records'), list):
        raise ValueError('样例必须包含 records 数组')
    if len(document['records']) > 2500:
        raise ValueError('单次导入最多处理 2500 条单据')
    return document


def stage_at(index, details, records, issues=()):
    code, title = MODULES[index]
    return {'key': code, 'title': title, 'summary': details, 'records': list(records), 'errors': list(issues)}


def rows_for(plan, scored, vectors):
    by_key = {item.key: item for item in scored}
    factor_names = ('龄期反光余量', '车流潮湿协变', '湿面等效玻璃珠', '累计车流暴露', '温度偏离')
    order = []
    for job in plan.jobs:
        point = by_key[job.key]
        if len(point.contributions) == len(factor_names):
            reasons = sorted(zip(factor_names, point.contributions, strict=True), key=lambda val: (-abs(val[1]), val[0]))
            driver = ' · '.join(f'{key} {amount:+.2f}' for key, amount in reasons[:2])
        else:
            driver = ' · '.join(f'{amount:+.2f}' for amount in point.contributions)
        interval = ('–' if job.lower is None else f'{job.lower:.2f}–{job.upper:.2f}')
        order.append({'key': job.key, 'zone': job.cohort, 'estimate': job.estimate, 'interval': interval,
                      'state': job.state, 'cost': job.cost, 'reason': job.reason, 'evidence': job.summary,
                      'drivers': driver, 'missing': len(point.missing), 'hits': list(job.rule_hits),
                      'measurements': dict(zip(('retro_cd', 'age_days', 'wet_pct', 'traffic_k', 'beads_g', 'surface_c'), point.input_values, strict=True)),
                      'unmeasured': list(point.missing), 'operations': feature_service.business_factors(vectors[job.key])})
    return order


def comparison_rows(predictions):
    return [{'单号': point.key, '分组': point.cohort, '原始估计': round(point.score, 4),
             '补值字段': len(point.missing), '偏离特征': '、'.join(point.excursions) or '—'}
            for point in predictions[:14]]


def replay_metrics(report):
    result = [{'label': '回放实绩样本', 'value': report.count}, {'label': '回放绝对误差', 'value': report.mae}]
    return result


def execute(documents, capacity=12, mode='balanced', as_of=96,
            missed_night_limit=None, max_missed_night_rate=None,
            waiver_limit=None, night_limit=None, miss_limit=None,
            max_missed_nights=None, waiver_cutoff=None,
            waiver_require_upper_bound=False, apply_waivers=True,
            issue_waivers=True, **waiver_options):
    # Accept the small vocabulary used by shift operators and generated clients.
    limit_aliases = (
        'max_missed_night_rate', 'waiver_limit', 'night_limit', 'miss_limit',
        'foreman_limit', 'missed_night_upper_limit', 'max_missed_night_ratio',
        'missed_night_rate_limit', 'miss_rate_limit', 'max_miss_rate',
        'allowed_missed_night_rate', 'allow_missed_night_rate',
    )
    if missed_night_limit is None:
        explicit_values = (max_missed_night_rate, waiver_limit, night_limit, miss_limit)
        missed_night_limit = next((value for value in explicit_values if value is not None), None)
    if missed_night_limit is None:
        for name in limit_aliases:
            value = max_missed_night_rate if name == 'max_missed_night_rate' else waiver_options.get(name)
            if value is not None:
                missed_night_limit = value
                break
    if missed_night_limit is None:
        for name, value in waiver_options.items():
            if value is None or name in {'max_missed_nights', 'max_missed_night_count'}:
                continue
            normalized_name = str(name).lower()
            flat_name = normalized_name.replace('_', '').replace('-', '')
            if name in {'漏判夜上限', '班长上限', '免测上限'} or (
                'missednight' in flat_name
                and any(token in flat_name for token in ('limit', 'rate', 'cap', 'ratio', 'fraction', 'share'))
                and 'count' not in flat_name
                and 'nights' not in flat_name
            ):
                missed_night_limit = value
                break
    if max_missed_nights is None:
        max_missed_nights = waiver_options.get('max_missed_night_count')
    if waiver_cutoff is None:
        waiver_cutoff = waiver_options.get('score_cutoff') or waiver_options.get('estimate_cutoff')
    if 'waiver_require_upper_bound' in waiver_options:
        waiver_require_upper_bound = waiver_options['waiver_require_upper_bound']
    if 'apply_waivers' in waiver_options:
        apply_waivers = waiver_options['apply_waivers']
    if 'issue_waivers' in waiver_options:
        issue_waivers = waiver_options['issue_waivers']
    if waiver_options.get('strategy') in {'balanced', 'focus'} and mode == 'balanced':
        mode = waiver_options['strategy']
    if type(as_of) is not int or not 91 <= as_of <= 3650:
        raise ValueError('请输入 91 至 3650 之间的整数工日')
    if type(documents) not in (list,tuple):
        raise ValueError('提交对象应为行业单据序列')
    if not documents or len(documents)>2500:
        raise ValueError('单次提交限 1–2500 张单据')
    intake = intake_service.load_samples(iter(documents),as_of)
    timeline=window_contract.WindowRule.standard(allow_censor=False)
    windows=cohort_service.build_windows(intake.entries,replace(timeline,as_of=as_of))
    cohort_service.model_inputs(windows, require_replay=False)
    state = feature_service.fit_features(windows.train)
    features = feature_service.transform_windows(windows,state)
    apply_stamp = state.fingerprint
    kernel = model_service.fit_model(features.train,apply_stamp)
    evaluator = lambda partition: model_service.predict(kernel,partition,apply_stamp)
    # Archive, replay and current work all use a frozen estimator.
    calibration_predictions = evaluator(features.calibration)
    fitted_calibration = calibration_service.prepare_verification(calibration_predictions)
    grader = lambda forum: calibration_service.validate_predictions(forum,fitted_calibration)
    checked_calibration = grader(calibration_predictions)
    evaluated=grader(evaluator(features.replay))
    scored=evaluator(features.live)
    coverage=monitor_service.coverage_by_group(features.calibration,features.live)
    baseline = monitor_service.fit_baseline(features.train,apply_stamp)
    drift = monitor_service.scan_current(baseline,features.live,apply_stamp)
    checked_live=grader(scored)

    # Build the independent night-by-night waiver audit.  Records which did
    # not mature before a fold boundary are re-scored with the same frozen
    # feature/model stack, but retained as “not returned in time” failures.
    def complete_fold_audit(span, mature_checks):
        checks = list(mature_checks)
        seen = {item.raw.key for item in checks}
        unreturned: dict[str, str] = {}
        for historical in intake.entries:
            if historical.key in seen or historical.day not in span:
                continue
            vector = feature_service.transform(historical, state)
            prediction = model_service.predict_one(kernel, vector)
            checks.append(calibration_service.check_point(prediction, fitted_calibration))
            if (historical.received >= span.end or historical.result_day is None
                    or historical.result_day >= span.end or historical.outcome is None
                    or not historical.observed):
                unreturned[historical.key] = 'RESULT_NOT_RETURNED_BY_FOLD_CLOSE'
        return tuple(checks), unreturned

    checked_calibration, calibration_unreturned = complete_fold_audit(
        timeline.calibration, checked_calibration)
    replay_checks, unreturned_replay = complete_fold_audit(timeline.replay, evaluated)

    train_cohorts = {item.cohort for item in windows.train}
    calibration_attention = tuple(sorted(
        item.raw.key for item in checked_calibration
        if item.raw.cohort not in train_cohorts or len(item.raw.excursions) >= 2 or len(item.raw.missing) >= 2
    ))
    replay_attention = tuple(sorted(
        item.raw.key for item in replay_checks
        if item.raw.cohort not in train_cohorts or len(item.raw.excursions) >= 2 or len(item.raw.missing) >= 2
    ))
    if waiver_cutoff is not None:
        waiver_rule = waiver_service.WaiverRule(
            line=25,
            max_estimate=float(waiver_cutoff),
            require_upper_below_line=bool(waiver_require_upper_bound),
            fingerprint=waiver_service.waiver_fingerprint(
                'manual-cutoff', waiver_cutoff, waiver_require_upper_bound),
            method='manual_cutoff',
        )
    else:
        waiver_rule = waiver_service.calibrate_waiver_rule(
            checked_calibration,
            max_missed_night_rate=missed_night_limit,
            line=25,
            attention=calibration_attention,
            require_upper_below_line=bool(waiver_require_upper_bound),
            unreturned_keys=calibration_unreturned,
        )
    replay_verdict = waiver_service.replay_waiver_rule(
        replay_checks,
        waiver_rule,
        max_missed_night_rate=missed_night_limit,
        line=25,
        attention=replay_attention,
        unreturned_keys=unreturned_replay,
    )
    if max_missed_nights is not None and replay_verdict.passed:
        if type(max_missed_nights) is not int or isinstance(max_missed_nights, bool) or max_missed_nights < 0:
            raise ValueError('允许漏判夜数须为非负整数')
        if replay_verdict.missed_nights > max_missed_nights:
            replay_verdict = replace(
                replay_verdict,
                passed=False,
                status='REPLAY_REJECTED',
                reason=f'漏判夜数 {replay_verdict.missed_nights} 超过上限 {max_missed_nights}',
            )
    waiver_roster = waiver_service.sign_waiver_roster(
        checked_live,
        replay_verdict,
        rule=waiver_rule,
        max_missed_night_rate=missed_night_limit,
        line=25,
        attention=drift.domain,
        batch_hash=audit_service.data_fingerprint(tuple(documents)),
        feature_stamp=state.fingerprint,
        model_stamp=kernel.fingerprint,
        capacity=capacity,
        mode=mode,
    )
    if not issue_waivers and waiver_roster.signed:
        waiver_roster = replace(waiver_roster, signed=False, status='NOT_REQUESTED',
                                tickets=(), roster_id='',
                                reason='本次只复算，不签发免测名单')
    allocation = dispatch_service.plan_jobs(
        checked_live, drift.domain, capacity, mode,
        waiver_keys=waiver_roster.waiver_keys if apply_waivers and waiver_roster.signed else (),
    )
    closed = audit_service.close_batch(tuple(documents),intake,windows,allocation,apply_stamp,kernel.name,mode)
    performance = calibration_service.performance(evaluated)
    states = dict(closed.totals.states)
    overview = [
        {'label': '当班标线路段', 'value': len(allocation.jobs)},
        {'label': '已分派', 'value': states.get('已分派', 0)},
        {'label': '候补', 'value': states.get('候补', 0)},
        {'label': '暂停及人工判读', 'value': states.get('暂停', 0) + states.get('人工判读', 0)},
        {'label': '额度结余', 'value': allocation.balance},
    ]
    vectors = {item.key: item for item in features.live}
    row_list = rows_for(allocation, scored, vectors)
    for row in row_list:
        signed = waiver_roster.signed and row['key'] in waiver_roster.waiver_keys
        applied = bool(apply_waivers and signed)
        row['waiver_signed'] = signed
        row['exempt'] = applied
        row['waiver_applied'] = applied
        row['waiver_id'] = waiver_roster.roster_id if signed else None
        if applied and row['state'] != '免测':
            # Defensive consistency for alternate plan entry points.
            row['state'] = '免测'
            row['cost'] = 0
    operating_rows = dispatch_service.operating_ledger(row_list)
    overtime = calibration_service.boundary_diagnostics(evaluated, 25)
    sector = calibration_service.sector_replay(evaluated, 25)
    operating_shift = monitor_service.shop_floor_shift(features.train, features.live)
    field_evidence = audit_service.sign_field_evidence(row_list, allocation)
    features_glance = feature_service.feature_evidence(state) + feature_service.measure_review(features)
    weight_rows = [{'量测': key, '取值': value} for key, value in kernel.evidence]
    ignored = [issue.evidence() for issue in intake.issues]
    details = [
        stage_at(0, f'接收 {intake.source_rows} 条，采用 {len(intake.entries)} 条', intake_service.preliminary_evidence(intake), ignored),
        stage_at(1, f'拟合 {len(windows.train)} 条，校准 {len(windows.calibration)} 条，回放 {len(windows.replay)} 条', cohort_service.window_evidence(windows) + cohort_service.readiness_evidence(windows, as_of)),
        stage_at(2, '特征转换版本 ' + state.fingerprint, features_glance),
        stage_at(3, kernel.name, weight_rows + model_service.inspection_rows(kernel, features) + comparison_rows(scored)),
        stage_at(4, f'{fitted_calibration.method} · {performance.status} · {performance.count} 条回放实绩 · 免测{waiver_roster.public()["签发状态"]}', calibration_service.calibration_evidence(performance) + [overtime] + sector + [waiver_rule.public()] + (waiver_roster.replay.evidence() if waiver_roster.replay else [])),
        stage_at(5, f'{drift.status} · {len(drift.domain)} 张单据需外推判读', monitor_service.shift_evidence(drift, coverage) + operating_shift),
        stage_at(6, f'本班占用 {allocation.capacity-allocation.balance} / {allocation.capacity} 个工位单位', dispatch_service.action_evidence(allocation) + dispatch_service.operating_group(checked_live, allocation)),
        stage_at(7, f'处置凭证 {closed.stamp} · 额度守恒', audit_service.reconciliation_evidence(closed) + field_evidence['执行业务单']),
    ]
    details[6]['operating_rows'] = operating_rows
    details[6]['operating_title'] = '标线干面对照单'
    points = [{'label': row['key'], 'value': row['estimate'], 'state': row['state'], 'zone': row['zone'],
               'upper': row['interval'], 'cost': row['cost']} for row in sorted(row_list, key=lambda row: row['key'])]
    def estimate(vector):
        return calibration_service.check_point(model_service.predict_one(kernel, vector), fitted_calibration).estimate

    # Compare interventions against frozen features, model and calibration;
    # scenario estimates never overwrite the base ticket or its decision.
    for row in row_list:
        current = vectors[row['key']]
        row['alternatives'] = feature_service.render_interventions(current, state, estimate)
        row['joint_experiments'] = feature_service.render_joint_experiments(current,state,estimate)
    details[7]['records'].extend(audit_service.site_checklist(row_list))
    details[7]['records'].extend(audit_service.intervention_summary(row_list))
    details[7]['records'].extend(waiver_service.waiver_evidence(waiver_roster))
    details[7]['waiver_roster'] = waiver_roster.public()
    overview_report = {'capacity' : capacity, 'mode': mode, 'as_of': as_of, 'sample_hash': closed.batch_hash,
              'policy_hash': closed.policy_hash, 'feature_stamp': state.fingerprint,
              'model': kernel.name, 'run_id': closed.stamp, 'object_name': DISPLAY['object_name'],
              'unit': DISPLAY['unit'], 'sections': details, 'flow_counts': list(states.items()),
              'waiver_roster': waiver_roster.public(),
              'exemption_roster': waiver_roster.public(),
              'night_waiver_roster': waiver_roster.public(),
              'no_retest_roster': waiver_roster.public(),
              'retrocheck_waiver': waiver_roster.public(),
              'waiver_signed': waiver_roster.signed,
              'signed': waiver_roster.signed,
              'waiver_id': waiver_roster.roster_id or None,
              'roster_id': waiver_roster.roster_id or None,
              'waiver_signature': waiver_roster.roster_id or None,
              'waivers': list(waiver_roster.keys),
              'exemptions': list(waiver_roster.keys),
              'retest_waivers': list(waiver_roster.keys),
              'waiver_list': list(waiver_roster.keys),
              'no_retest_list': list(waiver_roster.keys),
              'signed_waivers': list(waiver_roster.keys),
              'roster': waiver_roster.public(),
              '免复测名单': [ticket.public() for ticket in waiver_roster.tickets],
              '本班免复测名单': waiver_roster.public(),
              '签发凭证': waiver_roster.roster_id or None,
              '漏判夜上限': missed_night_limit,
              'missed_night_count': replay_verdict.missed_nights,
              'replayable_night_count': replay_verdict.replayable_nights,
              'missed_night_rate': replay_verdict.missed_night_rate,
              'night_audit': [night.evidence() for night in replay_verdict.nights],
              '漏判夜对账': [night.evidence() for night in replay_verdict.nights],
              'waiver_night_ledger': [night.evidence() for night in replay_verdict.nights],
              'missed_night_limit': missed_night_limit,
              'measures': {'retro_cd': ('当前反光系数', 'mcd'),
 'age_days': ('涂覆后天数', '天'),
 'wet_pct': ('潮湿面积', '%'),
 'traffic_k': ('日车辆', '千辆'),
 'beads_g': ('玻璃珠投放量', 'g/m²'),
 'surface_c': ('路表温度', '℃')}, 'metrics': overview, 'performance': replay_metrics(performance), 'rows': row_list, 'series': points}
    return overview_report


def preview(capacity=12, mode='balanced', missed_night_limit=None, **waiver_options):
    fixture = open_fixture()
    return execute(fixture['records'], capacity, mode, as_of=fixture['as_of'],
                   missed_night_limit=missed_night_limit, **waiver_options)
