"""실행: python -m streamlit run app.py

아이폰 Safari에서 배포된 주소에 접속한다. 신한 잔고 자동조회는 미연결 상태다.
"""
import copy
import hmac
import json
import os
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from core import DEFAULT_CASH_POLICY, dec, validate_cash_policy
from engine import default_assets, validate_assets, calculate, CASH_KEYS
from prices import latest_quote

st.set_page_config(page_title='동우의 리밸런싱', page_icon='⚖️', layout='centered')
st.markdown('''<style>
.block-container{max-width:980px;padding-top:2rem}h1{font-size:1.8rem!important}
div[data-testid="stMetric"]{padding:1rem;background:#f4f7fa;border-radius:12px}
</style>''', unsafe_allow_html=True)
st.title('동우의 리밸런싱')


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def secret(name):
    value = os.environ.get(name, '')
    if value:
        return value
    try:
        return str(st.secrets.get(name, ''))
    except FileNotFoundError:
        return ''


def require_login():
    password = secret('APP_PASSWORD')
    if len(password) < 20:
        st.info('먼저 배포 설정의 Secrets에 APP_PASSWORD를 20자 이상으로 설정하세요.')
        st.caption('증권사 비밀번호가 아닌, 이 계산기에만 사용할 별도의 비밀번호입니다.')
        st.stop()
    if st.session_state.get('authenticated'):
        return
    with st.form('login'):
        given = st.text_input('계산기 비밀번호', type='password')
        submit = st.form_submit_button('열기', type='primary')
    if submit:
        if hmac.compare_digest(given.encode(), password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        st.error('비밀번호를 확인하세요.')
    st.stop()


def blank_state():
    assets = default_assets()
    return dict(version=3, assets=assets, gold_enabled=False, cash_policy=dict(DEFAULT_CASH_POLICY),
                fee_pct=.3, include_sales=False,
                snapshot=dict(asof='', complete=False, open_orders=True,
                              fx=dict(price=0, asof='', source='직접 입력'), quotes={},
                              positions=[dict(asset=a['id'], account=account, qty=0, sellable=0)
                                         for a in assets for account in ('ISA', 'CMA')
                                         if account == 'CMA' or a['market'] == 'KR'],
                              cash=[dict(account=account, currency=currency, total=0,
                                         available=0, rp=0, reserve=0)
                                    for account, currency in [('ISA', 'KRW'), ('CMA', 'KRW'), ('CMA', 'USD')]]))


def serial(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def imported_state(raw):
    if len(raw) > 1_000_000:
        raise ValueError('파일은 1MB 이하여야 합니다.')
    value = json.loads(raw.decode('utf-8-sig'))
    if value.get('version') != 3:
        raise ValueError('이 앱에서 저장한 버전 3 백업 파일을 사용하세요. 이전 통합 계좌 입력은 CMA·ISA별로 옮겨주세요.')
    validate_assets(value['assets'])
    for a in value['assets']:
        for field in ('target','lower','upper','lot'):
            a[field] = float(dec(a[field], field))
    for flag in ('include_sales','gold_enabled'):
        if type(value.get(flag)) is not bool:
            raise ValueError('백업의 계산 옵션은 true/false여야 합니다.')
    value['fee_pct'] = float(dec(value['fee_pct']))
    value['cash_policy'] = validate_cash_policy(value.get('cash_policy'))
    if value['fee_pct'] > 10 or value['cash_policy']['fx_cost_pct'] > 10:
        raise ValueError('화면에서 지원하는 비용 여유 범위는 0~10%입니다.')
    value['snapshot']['fx']['price'] = float(dec(value['snapshot']['fx']['price']))
    if not isinstance(value['snapshot']['quotes'], dict):
        raise ValueError('시세 형식 오류입니다.')
    for q in value['snapshot']['quotes'].values():
        q['price'] = float(dec(q['price']))
    if not isinstance(value.get('gold_enabled'), bool):
        raise ValueError('금 거래 설정을 확인하세요.')
    for key in ('positions', 'cash'):
        if not isinstance(value['snapshot'][key], list):
            raise ValueError('백업 형식 오류: ' + key)
    ids = {a['id'] for a in value['assets']}
    positions_seen = set()
    for p in value['snapshot']['positions']:
        pair = (p['asset'], p['account'])
        if p['asset'] not in ids or p['account'] not in ('ISA', 'CMA') or pair in positions_seen:
            raise ValueError('보유자산 목록에 알 수 없는 종목·계좌 또는 중복 행이 있습니다.')
        if p['account'] == 'ISA' and next(a['market'] for a in value['assets'] if a['id'] == p['asset']) != 'KR':
            raise ValueError('ISA에 국내 상품 외의 자산이 입력되어 있습니다.')
        positions_seen.add(pair)
        for field in ('qty','sellable'):
            p[field] = float(dec(p[field], field))
        if p['sellable'] > p['qty']:
            raise ValueError('매도가능 수량은 보유수량 이하여야 합니다.')
    if {(c['account'], c['currency']) for c in value['snapshot']['cash']} != CASH_KEYS or len(value['snapshot']['cash']) != 3:
        raise ValueError('현금 행은 ISA 원화·CMA 원화·CMA 달러 3개여야 합니다.')
    for c in value['snapshot']['cash']:
        for field in ('total','available','rp','reserve'):
            c[field] = float(dec(c[field], field))
    # 과거 확인 체크를 새 세션으로 가져오지 않는다.
    value['snapshot']['complete'] = False
    value['snapshot']['open_orders'] = True
    value['limited'] = any(p['sellable'] < p['qty'] for p in value['snapshot']['positions'])
    return value


require_login()
if 'model' not in st.session_state:
    st.session_state.model = blank_state()
    st.session_state.rev = 0
model = st.session_state.model
assets = model['assets']
snapshot = model['snapshot']
rev = st.session_state.rev

st.caption('CMA + ISA 합산 · 밴드 밖에서만 중간값까지 조정')
st.warning('신한 계좌 자동조회는 연결되지 않았습니다. 보유 수량·현금을 직접 갱신하거나 이 앱의 백업을 불러오세요.')
st.caption('가격·환율 조회는 잔고 조회와 별개입니다. 이 앱에서 주문·환전은 실행되지 않습니다.')

with st.expander('저장한 입력 불러오기 · 사용 방법'):
    st.write('1. 계좌별 잔고와 현금 입력 → 2. 가격·환율 갱신 → 3. 확인 체크 → 4. 계산')
    st.write('설정과 입력은 서버 파일에 저장하지 않습니다. 끝날 때 JSON 백업을 아이폰 파일 앱에 저장하세요.')
    uploaded = st.file_uploader('이 앱에서 저장한 JSON 백업', type=['json'])
    if st.button('백업 불러오기', disabled=uploaded is None):
        try:
            st.session_state.model = imported_state(uploaded.getvalue())
            st.session_state.rev += 1
            st.rerun()
        except (ValueError, KeyError, TypeError) as e:
            st.error(str(e))
    st.caption('신한 앱에서 내보낸 파일을 직접 해석하는 기능은 아직 없습니다. 백업 파일은 이 계산기 전용 형식입니다.')

with st.expander('목표 비중과 매수 우선 계좌'):
    st.caption('기존 파일의 11개 자산 설정입니다. 국내 ETF는 ISA 우선, 잔액 부족 시 CMA에서도 매수합니다. 매도는 CMA부터 배분합니다.')
    edited_assets = st.data_editor(pd.DataFrame(assets), hide_index=True,
        disabled=['id', 'name', 'ticker', 'currency', 'market', 'unit', 'lot'],
        column_config={'id':None, 'ticker':None, 'currency':None, 'market':None, 'unit':None, 'lot':None,
                       'name':'자산', 'target':'목표 %', 'lower':'하단 %', 'upper':'상단 %',
                       'preferred_account':st.column_config.SelectboxColumn('매수 우선', options=['ISA','CMA'])},
        key=f'assets_{rev}')
    if st.button('목표 설정 적용'):
        try:
            model['assets'] = validate_assets(edited_assets.to_dict('records'))
            st.session_state.rev += 1
            st.rerun()
        except ValueError as e:
            st.error(str(e))
    st.caption('자산 종류를 바꿀 때는 백업의 assets·positions·quotes를 함께 수정해야 합니다. 보유자산을 제외하면 총자산이 줄어 잘못 계산됩니다.')

model['gold_enabled'] = st.checkbox('내 CMA 계좌에서 KRX 금현물 거래가 가능한 상태임을 확인했습니다.',
                                   value=model.get('gold_enabled', False), key=f'gold_{rev}')
st.caption('금은 국내 금현물의 원/g 단가를 직접 입력합니다. 별도 금 계좌를 보유했다면 이 2계좌용 설정에 임의로 합치지 마세요.')
st.subheader('1. 보유 수량')
limited = st.checkbox('일부 보유 수량에 매도 제한이 있습니다.',
    value=model.get('limited', any(float(p['sellable']) < float(p['qty']) for p in snapshot['positions'])), key=f'limited_{rev}')
model['limited'] = limited
positions = []
position_map = {(p['asset'], p['account']): p for p in snapshot['positions']}
for account in ('ISA', 'CMA'):
    st.write(f'**{account}**')
    table = []
    for a in assets:
        if account == 'ISA' and a['market'] != 'KR':
            continue
        old = position_map.get((a['id'], account), {})
        row = dict(asset=a['id'], name=a['name'], qty=float(old.get('qty', 0)))
        if limited:
            row['sellable'] = float(old.get('sellable', old.get('qty', 0)))
        table.append(row)
    edited = st.data_editor(pd.DataFrame(table), hide_index=True, disabled=['asset','name'],
        column_config={'asset':None, 'name':'자산',
                       'qty':st.column_config.NumberColumn('보유 수량', min_value=0, format='%.4f'),
                       'sellable':st.column_config.NumberColumn('매도 가능', min_value=0, format='%.4f')},
        key=f'positions_{account}_{rev}_{limited}')
    for p in edited.to_dict('records'):
        positions.append(dict(asset=p['asset'], account=account, qty=p['qty'], sellable=p.get('sellable', p['qty'])))
snapshot['positions'] = positions

st.subheader('2. 현금')
st.caption('순현금에는 미결제 대금까지 반영하고 RP·발행어음은 제외하세요. 별도 RP·발행어음은 아래 RP 칸에 한 번만 넣습니다. '
           '가용현금에는 현재 통화로 사용할 수 있는 자기자금만 넣으세요. 통합증거금 환산액·신용·대출 한도는 제외합니다.')
cash_table = st.data_editor(pd.DataFrame(snapshot['cash']), hide_index=True, disabled=['account','currency'],
    column_config={'account':'계좌', 'currency':'통화',
        'total':st.column_config.NumberColumn('순현금', min_value=0, format='%.2f'),
        'available':st.column_config.NumberColumn('가용현금', min_value=0, format='%.2f'),
        'rp':st.column_config.NumberColumn('별도 RP·어음', min_value=0, format='%.2f'),
        'reserve':st.column_config.NumberColumn('남겨둘 현금', min_value=0, format='%.2f')}, key=f'cash_{rev}')
snapshot['cash'] = cash_table.to_dict('records')
st.caption('출금가능금액과 주문가능금액을 더하지 않습니다. RP 환매 후에는 RP와 순현금/가용현금을 함께 수정해야 합니다.')

st.subheader('3. 가격·환율')
if st.button('공개 시세·환율 조회', type='primary'):
    jobs = [(a['id'], a['ticker'], a['currency']) for a in assets if a.get('ticker')]
    jobs.append(('FX', 'KRW=X', 'KRW'))
    with st.spinner('공개 시세를 조회하고 있습니다…'):
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda j: (j[0], latest_quote(j[1], j[2])), jobs))
    failed = []
    for asset_id, q in results:
        if q.get('ok'):
            if asset_id == 'FX':
                snapshot['fx'] = dict(price=q['price'], asof=q['asof'], source=q['source'])
            else:
                a = next(a for a in assets if a['id'] == asset_id)
                snapshot['quotes'][asset_id] = dict(price=q['price'], asof=q['asof'], source=q['source'], currency=a['currency'])
        else:
            failed.append(asset_id + ': ' + q.get('error', '조회 실패'))
    st.session_state.quote_errors = failed
    st.session_state.rev += 1
    st.rerun()
for error in st.session_state.get('quote_errors', []):
    st.warning(error + ' · 이전 값은 자동으로 새 시세가 되지 않습니다.')
st.caption('Yahoo Finance 참고 가격입니다. 지연·휴장·제공사 제한이 있을 수 있습니다. 실패 시 신한 화면의 단가를 입력하세요.')
qrows = []
for a in assets:
    q = snapshot['quotes'].get(a['id'], {})
    qrows.append(dict(asset=a['id'], name=a['name'], currency=a['currency'], price=float(q.get('price', 0)), asof=q.get('asof', ''), source=q.get('source','')))
qtable = st.data_editor(pd.DataFrame(qrows), hide_index=True, disabled=['asset','name','currency','asof','source'],
    column_config={'asset':None,'name':'자산','currency':'통화','price':st.column_config.NumberColumn('단가',min_value=0,format='%.4f'),
                   'asof':'시세 시각','source':'출처'}, key=f'quotes_{rev}')
fx_input = st.number_input('1달러당 원화', min_value=0., value=float(snapshot['fx']['price']), format='%.4f', key=f'fx_{rev}')
st.caption('환율 기준 시각: ' + (snapshot['fx'].get('asof') or '미입력'))
if st.button('직접 입력한 가격·환율 적용'):
    for q in qtable.to_dict('records'):
        old = snapshot['quotes'].get(q['asset'], {})
        if q['price'] != float(old.get('price', 0)):
            snapshot['quotes'][q['asset']] = dict(price=q['price'], currency=q['currency'], asof=utcnow(), source='직접 입력')
    if fx_input != float(snapshot['fx']['price']):
        snapshot['fx'] = dict(price=fx_input, asof=utcnow(), source='직접 입력')
    st.session_state.rev += 1
    st.rerun()

with st.expander('계산 옵션'):
    model['fee_pct'] = st.number_input('매매 비용 여유 (%)', min_value=0., max_value=10., value=float(model.get('fee_pct',.3)), step=.05)
    model['include_sales'] = st.checkbox('매도 완료 후 같은 계좌·통화에서 대금 재사용을 가정', value=model.get('include_sales',False))
    st.caption('기본값은 꺼짐입니다. 켜면 매도 체결·재사용 가능 여부를 확인한 뒤 실행할 조건부 계획입니다.')
    policy = model.get('cash_policy', dict(DEFAULT_CASH_POLICY))
    st.write(f"현금끼리의 원화 목표 {policy['target']}%, 밴드 {policy['lower']}~{policy['upper']}%")
    policy['include_rp'] = st.checkbox('현금 비율 계산에 RP·어음 포함', value=policy.get('include_rp',False))
    policy['fx_cost_pct'] = st.number_input('환전 비용 여유 (%)',min_value=0.,max_value=10.,value=float(policy.get('fx_cost_pct',0)),step=.05)
    model['cash_policy'] = policy

complete = st.checkbox('CMA·ISA의 모든 보유자산과 현금을 확인했고, 목록 밖 자산이 없습니다.', key=f'complete_{rev}')
no_orders = st.checkbox('현재 미체결 주문이 없습니다.', key=f'no_orders_{rev}')
st.caption('입력값을 바꾸면 결과를 다시 계산하세요. 매도 제한이 있으면 위에서 매도가능 수량도 지정해야 합니다.')
if st.button('리밸런싱 계산', type='primary'):
    # 잔고 시각만 현재로 갱신한다. 시세·환율 시각을 덮어쓰지 않는다.
    snapshot.update(complete=complete, open_orders=not no_orders, asof=utcnow())
    try:
        if fx_input != float(snapshot['fx']['price']) or any(q['price'] != float(snapshot['quotes'].get(q['asset'],{}).get('price',0)) for q in qtable.to_dict('records')):
            raise ValueError('수정한 가격·환율을 먼저 적용하세요.')
        result = calculate(snapshot, assets, fee_pct=model['fee_pct'], include_sales=model['include_sales'],
                           gold_enabled=model['gold_enabled'], cash_policy=model['cash_policy'])
        st.session_state.result = result
        st.session_state.result_model = serial(model)
    except (ValueError, KeyError, TypeError) as e:
        st.session_state.pop('result', None)
        st.error(str(e))

result = st.session_state.get('result')
if result and st.session_state.get('result_model') != serial(model):
    st.info('입력 또는 설정이 바뀌었습니다. 다시 계산하세요.')
    result = None
if result:
    st.subheader('리밸런싱 결과')
    st.metric('현금·RP 포함 총자산', f"{result['nav']:,.0f}원")
    if result['include_sales']:
        st.warning('매도 체결 및 대금 재사용을 가정한 계획입니다.')
    orders = result['orders']
    if orders:
        st.write('**계좌별 제안 수량**')
        for o in orders:
            amount_unit = '원' if o['currency'] == 'KRW' else '달러'
            unit = next(a['unit'] for a in assets if a['id'] == o['asset'])
            st.write(f"**{o['account']} · {o['side']} {o['qty']:g}{unit} · {o['name']}**")
            st.caption(f"참고 단가 {o['price']:,.4f}{amount_unit} · 예상 거래액 {o['amount']:,.2f}{amount_unit}")
    else:
        st.info('제안 가능한 거래가 없습니다. 아래 보류 사유도 확인하세요.')
    summary = []
    for r in result['rows']:
        summary.append({'자산':r['name'],'현재 %':float(r['weight']),'신호':r['signal'],
                        '도달 기준 %':float(r['destination_pct']),'필요 수량':None if r['needed_qty'] is None else float(r['needed_qty']),
                        '제안 수량':float(r['planned_qty']),'매매 후 %':float(r['post_weight']),
                        '사유':r['reason'] or ('매매 후에도 밴드 밖' if r['outside_after'] else '')})
    st.dataframe(pd.DataFrame(summary), hide_index=True)
    for r in result['rows']:
        if r['unfilled_qty'] and r['signal']=='매수':
            st.write(f"미충족: {r['name']} {r['unfilled_qty']:g}{r['unit']} · 비용 여유 포함 약 {r['unfilled_qty']*r['price']*(1+Decimal(str(model['fee_pct']))/100):,.2f} {r['currency']} 추가 재원 필요")
    c = result['cash_plan']
    st.write('**매매·결제 완료 후 현금 비율**')
    if c['krw_pct'] is None:
        st.write('잔여 현금이 없습니다.')
    else:
        st.write(f"원화 {c['krw_pct']:.1f}% / 달러 {c['usd_pct']:.1f}%")
        if c['deferred']:
            st.info('자산 매매 계획이 일부 미충족입니다. 재원을 확보하고 잔고를 갱신한 뒤 현금 비율 환전을 다시 계산하세요.')
        elif c['direction'] == '유지':
            st.write('현금 비율에 따른 추가 환전은 없습니다.')
        else:
            st.write(f"CMA · {c['direction']} · {c['proposed_usd']:,.2f}달러 / 약 {c['krw_amount']:,.0f}원")
            st.caption(f"{c['status']} · ISA 현금은 비율에 포함되지만 환전 재원에서는 제외됩니다.")
    st.download_button('계산 결과 CSV 저장', pd.DataFrame(summary).to_csv(index=False).encode('utf-8-sig'), 'rebalance_result.csv', 'text/csv')
    st.download_button('계좌별 거래 제안 CSV 저장', pd.DataFrame(orders).astype(str).to_csv(index=False).encode('utf-8-sig'), 'proposed_trades.csv', 'text/csv')
st.download_button('설정·입력 JSON 백업 저장', serial(model), 'rebalancer_backup.json', 'application/json')
st.caption('비용은 입력한 여유율로만 추정합니다. 양도소득세·상품별 매도 과세·정확한 수수료는 계산하지 않습니다. 거래 단위는 중간값을 넘기지 않도록 내림합니다.')
if st.button('잠그기'):
    st.session_state.clear()
    st.rerun()
