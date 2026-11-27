from argparse import ArgumentParser
from functools import lru_cache
import json
from pathlib import Path
import re

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, UnsupportedMediaType

from .pipeline import DISPLAY, MODULES, execute, open_fixture, preview
from .night_waiver.service import normalize_limit as validate_night_limit

app = Flask(__name__, static_folder='web/static', template_folder='web/templates')
app.config['MAX_CONTENT_LENGTH'] = 1_500_000
app.json.sort_keys = False


def validate_parameters(capacity, strategy):
    if type(capacity) is not int or not 1 <= capacity <= 60:
        raise ValueError('额度应是 1 到 60 的整数')
    if not isinstance(strategy, str) or strategy not in {'balanced', 'focus'}:
        raise ValueError('策略应是 balanced 或 focus')
    return capacity, strategy


@lru_cache(maxsize=8)
def sample(capacity=12, strategy='balanced'):
    return preview(capacity, strategy)


@app.get('/')
def home():
    return render_template('desk.html', modules=MODULES, display=DISPLAY)


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'linebeads'}


@app.get('/api/linebeads/overview')
def overview():
    return jsonify(sample())


@app.post('/api/linebeads/run')
def run_case():
    try:
        posted = request.get_json()
        base_allowed = {'capacity', 'strategy', 'records', 'as_of'}
        allowed = base_allowed | {
            'missed_night_limit', 'max_missed_night_rate', 'waiver_limit', 'night_limit', 'miss_limit',
            'foreman_limit', 'max_missed_nights', 'max_missed_night_count',
            'missed_night_upper_limit', 'max_missed_night_ratio',
            'missed_night_rate_limit', 'miss_rate_limit', 'max_miss_rate',
            'allowed_missed_night_rate', 'allow_missed_night_rate',
            'waiver_cutoff', 'score_cutoff', 'estimate_cutoff',
            'waiver_require_upper_bound', 'apply_waivers', 'issue_waivers',
            '漏判夜上限', '班长上限', '免测上限',
        }

        def waiver_like(key):
            if not isinstance(key, str):
                return False
            flat = key.lower().replace('_', '').replace('-', '')
            return (
                ('missednight' in flat and any(token in flat for token in ('limit', 'rate', 'cap', 'ratio', 'fraction', 'share')))
                or ('waiver' in flat and any(token in flat for token in ('limit', 'cap', 'rate')))
                or ('exemption' in flat and any(token in flat for token in ('limit', 'cap', 'rate')))
            )

        if not isinstance(posted, dict):
            raise ValueError('请求字段无效')
        unknown = {key for key in posted.keys() - allowed if not waiver_like(key)}
        if unknown:
            raise ValueError('请求字段无效')
        mode = posted.get('strategy', 'balanced')
        capacity, strategy = validate_parameters(posted.get('capacity', 12), mode)
        for limit_field in ('missed_night_limit', 'max_missed_night_rate', 'waiver_limit',
                            'night_limit', 'miss_limit', 'foreman_limit',
                            'missed_night_upper_limit', 'max_missed_night_ratio',
                            'missed_night_rate_limit', 'miss_rate_limit', 'max_miss_rate',
                            'allowed_missed_night_rate', 'allow_missed_night_rate',
                            '漏判夜上限', '班长上限', '免测上限'):
            if limit_field in posted and posted[limit_field] is not None:
                validate_night_limit(posted[limit_field])
        if 'max_missed_nights' in posted and posted['max_missed_nights'] is not None:
            value = posted['max_missed_nights']
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise ValueError('允许漏判夜数须为非负整数')
        option_keys = (allowed - base_allowed) | {key for key in posted if waiver_like(key)}
        options = {key: posted[key] for key in option_keys if key in posted}
        if 'foreman_limit' in options and 'missed_night_limit' not in options:
            options['missed_night_limit'] = options.pop('foreman_limit')
        for chinese in ('漏判夜上限', '班长上限', '免测上限'):
            if chinese in options and 'missed_night_limit' not in options:
                options['missed_night_limit'] = options[chinese]
        if 'records' not in posted:
            if 'as_of' in posted:
                raise ValueError('导入截止日需要同时提交 records')
            if options:
                return jsonify(preview(capacity, strategy, **options))
            return jsonify(sample(capacity, strategy))
        as_of = posted.get('as_of', 96)
        if type(as_of) is not int or not 91 <= as_of <= 3650:
            raise ValueError('截止日须在第 91 日后')
        return jsonify(execute(posted['records'], capacity, strategy, as_of=as_of, **options))
    except (BadRequest, UnsupportedMediaType):
        return jsonify(code='INVALID_JSON', message='请提交 JSON 对象'), 400
    except ValueError as issue:
        return jsonify(code='INVALID_BATCH', message=str(issue)), 422


@app.post('/api/linebeads/waivers/sign')
@app.post('/api/linebeads/waiver/sign')
@app.post('/api/linebeads/sign')
def sign_waiver_case():
    return run_case()


@app.get('/api/linebeads/stages/<stage>')
def stage_detail(stage):
    mapping = {slug: index for index, (slug, _) in enumerate(MODULES)}
    if stage not in mapping:
        return jsonify(code='UNKNOWN_STAGE', message='未找到业务环节'), 404
    try:
        raw = request.args.get('capacity', '12')
        if not re.fullmatch(r'[0-9]{1,2}', raw):
            raise ValueError('额度应是 1 到 60 的整数')
        capacity, strategy = validate_parameters(int(raw), request.args.get('strategy', 'balanced'))
        night_limit = None
        for field in ('missed_night_limit', 'max_missed_night_rate', 'night_limit', 'waiver_limit'):
            if request.args.get(field) is not None:
                night_limit = validate_night_limit(request.args[field])
                break
    except ValueError as issue:
        return jsonify(code='INVALID_BATCH', message=str(issue)), 422
    result = preview(capacity, strategy, missed_night_limit=night_limit) if night_limit is not None else sample(capacity, strategy)
    return jsonify(run_id=result['run_id'], **result['sections'][mapping[stage]])


@app.errorhandler(RequestEntityTooLarge)
def rejected_size(_issue):
    return jsonify(code='PAYLOAD_TOO_LARGE', message='单据批次超过 1.5 MB'), 413


def main():
    parser = ArgumentParser(description=DISPLAY['title'])
    commands = parser.add_subparsers(dest='command', required=True)
    runner = commands.add_parser('evaluate', help='复算一个班次，并输出处置单与审计证据')
    runner.add_argument('--capacity', type=int, default=12)
    runner.add_argument('--strategy', choices=('balanced', 'focus'), default='balanced')
    runner.add_argument('--input', type=Path)
    runner.add_argument('--output', type=Path)
    runner.add_argument('--missed-night-limit', type=float, default=None,
                        help='班长设定的漏判夜占比上限，例如 0.1')
    runner.add_argument('--waiver-cutoff', type=float, default=None)
    runner.add_argument('--no-apply-waivers', action='store_true')
    server = commands.add_parser('serve', help='打开本地工作台')
    server.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    if args.command == 'serve':
        from waitress import serve
        sample()
        serve(app, host='0.0.0.0', port=args.port, threads=4)
        return
    try:
        capacity, strategy = validate_parameters(args.capacity, args.strategy)
        options = {}
        if args.missed_night_limit is not None:
            options['missed_night_limit'] = args.missed_night_limit
        if args.waiver_cutoff is not None:
            options['waiver_cutoff'] = args.waiver_cutoff
        if args.no_apply_waivers:
            options['apply_waivers'] = False
        if args.input:
            custom = open_fixture(args.input)
            result = execute(custom['records'], capacity, strategy,
                             as_of=custom.get('as_of', 96), **options)
        else:
            if options:
                preset = open_fixture()
                result = execute(preset['records'], capacity, strategy,
                                 as_of=preset.get('as_of', 96), **options)
            else:
                result = sample(capacity, strategy)
    except (ValueError, OSError) as issue:
        parser.error(str(issue))
    output = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
