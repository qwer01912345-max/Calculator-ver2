"""CMA/ISA를 합산하여 신호를 만들고, 계좌별 현금으로 수량을 배분한다.

신한 계좌에 접속하거나 주문하는 코드는 포함하지 않는다.
"""
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from core import dec, defaults, cash_rebalance, validate_cash_policy

D = Decimal
CASH_KEYS = {('CMA', 'KRW'), ('CMA', 'USD'), ('ISA', 'KRW')}


def default_assets():
    result = defaults()
    for a in result:
        a.pop('account')
        a['market'] = 'GOLD' if a['id'] == 'GOLD' else 'US' if a['currency'] == 'USD' else 'KR'
        a['preferred_account'] = 'ISA' if a['market'] == 'KR' else 'CMA'
    return result


def allowed(a, account, gold_enabled):
    if account == 'ISA':
        return a['market'] == 'KR' and a['currency'] == 'KRW'
    return account == 'CMA' and (a['market'] != 'GOLD' or gold_enabled)


def validate_assets(assets):
    seen = set()
    total = D(0)
    if not assets:
        raise ValueError('자산 목록이 비어 있습니다.')
    for a in assets:
        if not a.get('id') or a['id'] in seen:
            raise ValueError('자산 ID가 없거나 중복됩니다.')
        seen.add(a['id'])
        if not a.get('name') or a.get('market') not in ('KR', 'US', 'GOLD'):
            raise ValueError('자산명과 시장을 확인하세요.')
        if a.get('currency') != ('USD' if a['market'] == 'US' else 'KRW'):
            raise ValueError('시장과 통화가 일치하지 않습니다.')
        lo, target, hi = (dec(a[k], k) for k in ('lower', 'target', 'upper'))
        if not lo <= target <= hi <= 100 or dec(a['lot']) <= 0:
            raise ValueError('하단 ≤ 목표 ≤ 상단 ≤ 100, 거래단위 > 0이어야 합니다.')
        if a.get('preferred_account') not in ('CMA', 'ISA'):
            raise ValueError('매수 우선 계좌를 확인하세요.')
        if a['market'] != 'KR' and a['preferred_account'] != 'CMA':
            raise ValueError('미국 ETF와 금현물의 매수 계좌는 CMA로 지정하세요.')
        total += target
    if abs(total - 100) > D('0.000001'):
        raise ValueError(f'목표 비중 합계가 {total}%입니다. 100%로 맞추세요.')
    return assets


def timestamp(value, label, max_age_hours, now=None):
    try:
        t = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if t.tzinfo is None:
            raise ValueError()
        now = now or datetime.now(timezone.utc)
        age = (now - t).total_seconds()
        if age < -300 or age > max_age_hours * 3600:
            raise ValueError()
        return t
    except (ValueError, TypeError):
        raise ValueError(f'{label}: 기준 시각을 확인하세요. 허용 기간은 {max_age_hours}시간입니다.') from None


def floor_lot(qty, lot):
    return (qty / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def money(value, currency, buy=False):
    return value.quantize(D(1) if currency == 'KRW' else D('.01'),
                          rounding=ROUND_CEILING if buy else ROUND_FLOOR)


def calculate(snapshot, assets=None, *, fee_pct=.3, include_sales=False,
              gold_enabled=False, cash_policy=None, now=None):
    assets = validate_assets(assets or default_assets())
    if type(include_sales) is not bool or type(gold_enabled) is not bool:
        raise ValueError('계산 옵션은 true/false여야 합니다.')
    if snapshot.get('complete') is not True:
        raise ValueError('CMA·ISA의 국내/해외 보유자산과 현금을 모두 확인해야 합니다.')
    if snapshot.get('open_orders') is not False:
        raise ValueError('미체결 주문이 있거나 확인되지 않았습니다. 주문 상태와 잔고를 갱신하세요.')
    timestamp(snapshot.get('asof'), '잔고', 24, now)
    fx = dec(snapshot['fx']['price'], '환율')
    if fx <= 0:
        raise ValueError('환율은 0보다 커야 합니다.')
    timestamp(snapshot['fx'].get('asof'), '환율', 168, now)
    fee = dec(fee_pct, '비용 여유') / 100
    if fee >= 1:
        raise ValueError('비용 여유는 100% 미만이어야 합니다.')
    validate_cash_policy(cash_policy)
    convert = lambda value, currency: value * (fx if currency == 'USD' else D(1))
    by_id = {a['id']: a for a in assets}
    balances = {}
    nav = D(0)
    for c in snapshot['cash']:
        key = (c['account'], c['currency'])
        if key in balances or key not in CASH_KEYS:
            raise ValueError('현금 행이 중복되었거나 계좌·통화가 잘못됐습니다.')
        row = {k: dec(c[k], k) for k in ('total', 'available', 'rp', 'reserve')}
        if row['available'] > row['total'] or row['reserve'] > row['total']:
            raise ValueError('가용현금·유보현금은 RP를 제외한 순현금 이하여야 합니다.')
        balances[key] = row
        nav += convert(row['total'] + row['rp'], key[1])
    if set(balances) != CASH_KEYS:
        raise ValueError('CMA 원화·달러, ISA 원화 현금 행을 모두 입력하세요.')
    positions = {}
    for p in snapshot['positions']:
        asset_id, account = p['asset'], p['account']
        key = (asset_id, account)
        if asset_id not in by_id:
            raise ValueError(f'설정에 없는 보유자산 {asset_id}: 설정에 추가해야 총자산이 정확합니다.')
        if key in positions:
            raise ValueError('동일 계좌·종목이 중복되었습니다.')
        qty, sellable = dec(p['qty'], '수량'), dec(p['sellable'], '매도가능 수량')
        if sellable > qty:
            raise ValueError('매도가능 수량은 보유 수량을 초과할 수 없습니다.')
        if account not in ('CMA', 'ISA') or (qty and not allowed(by_id[asset_id], account, gold_enabled)):
            raise ValueError(f'{asset_id}: 계좌의 거래 가능 상품 또는 금 거래 신청 상태를 확인하세요.')
        positions[key] = dict(qty=qty, sellable=sellable)
    rows = []
    for a in assets:
        qty = sum((v['qty'] for (aid, _), v in positions.items() if aid == a['id']), D(0))
        quote = snapshot.get('quotes', {}).get(a['id'])
        price = dec(quote['price'], a['name'] + ' 가격') if quote else D(0)
        if price > 0:
            if quote.get('currency') != a['currency']:
                raise ValueError(a['name'] + ': 시세 통화가 일치하지 않습니다.')
            timestamp(quote.get('asof'), a['name'] + ' 시세', 168, now)
        elif qty > 0:
            raise ValueError(a['name'] + ': 보유자산 가격이 없어 총자산을 계산할 수 없습니다.')
        value = convert(qty * price, a['currency'])
        nav += value
        rows.append(dict(a, qty=qty, price=price, value=value))
    if nav <= 0:
        raise ValueError('보유자산이나 현금을 입력하세요.')
    budgets = {k: max(D(0), v['available'] - v['reserve']) for k, v in balances.items()}
    initial = budgets.copy()
    sales = {k: D(0) for k in balances}
    spent = {k: D(0) for k in balances}
    orders = []
    for r in rows:
        weight = r['value'] / nav * 100
        signal = '매수' if weight < dec(r['lower']) else '매도' if weight > dec(r['upper']) else '유지'
        dest = (dec(r['lower']) + dec(r['target'])) / 2 if signal == '매수' else (
            (dec(r['upper']) + dec(r['target'])) / 2 if signal == '매도' else weight)
        gap = nav * dest / 100 - r['value']
        needed = floor_lot(abs(gap) / convert(r['price'], r['currency']), dec(r['lot'])) if r['price'] else None
        r.update(weight=weight, signal=signal, destination_pct=dest, gap=gap,
                 needed_qty=needed, planned_qty=D(0), reason='')
        if needed is None:
            r['reason'] = '가격 입력 필요'
        elif signal != '유지' and needed == 0:
            r['reason'] = '거래단위 미만'
        if signal != '매도' or not needed:
            continue
        remaining = needed
        # CMA 보유분을 먼저 매도한다. 세금 최소화 최적화는 아니다.
        for account in ('CMA', 'ISA'):
            if not allowed(r, account, gold_enabled):
                continue
            p = positions.get((r['id'], account), {'sellable': D(0)})
            q = min(remaining, floor_lot(p['sellable'], dec(r['lot'])))
            if q <= 0:
                continue
            key = (account, r['currency'])
            net = money(q * r['price'] * (1 - fee), r['currency'])
            sales[key] += net
            if include_sales:
                budgets[key] += net
            orders.append(dict(asset=r['id'], name=r['name'], account=account,
                               side='매도', qty=q, price=r['price'], currency=r['currency'],
                               amount=q * r['price'], cash_change=net))
            remaining -= q
            r['planned_qty'] += q
        if remaining:
            r['reason'] = '매도가능 수량 부족'
    # 매수 중간값까지 부족한 원화 금액이 큰 순서. 총자산/신호는 처음 값으로 고정.
    for r in sorted(rows, key=lambda x: x['gap'], reverse=True):
        if r['signal'] != '매수' or not r['needed_qty']:
            continue
        remaining = r['needed_qty']
        accounts = sorted(('ISA', 'CMA'), key=lambda x: x != r['preferred_account'])
        eligible = [a for a in accounts if allowed(r, a, gold_enabled)]
        for account in eligible:
            key = (account, r['currency'])
            unit = r['price'] * (1 + fee)
            q = min(remaining, floor_lot(budgets[key] / unit, dec(r['lot'])))
            cost = money(q * unit, r['currency'], buy=True)
            if cost > budgets[key]:
                q = max(D(0), q - dec(r['lot']))
                cost = money(q * unit, r['currency'], buy=True)
            if q <= 0:
                continue
            budgets[key] -= cost
            spent[key] += cost
            orders.append(dict(asset=r['id'], name=r['name'], account=account,
                               side='매수', qty=q, price=r['price'], currency=r['currency'],
                               amount=q * r['price'], cash_change=-cost))
            remaining -= q
            r['planned_qty'] += q
        if remaining:
            r['reason'] = '계좌·통화별 자금 부족' if eligible else '거래 가능한 계좌 설정 필요'
    funding = []
    for key, b in balances.items():
        funding.append(dict(account=key[0], currency=key[1], initial=initial[key],
                            remaining=budgets[key], sales=sales[key], spent=spent[key],
                            post_total=b['total'] + sales[key] - spent[key], rp=b['rp'],
                            post_available=max(D(0), b['available'] + sales[key] - spent[key] - b['reserve'])))
    for r in rows:
        r['unfilled_qty'] = None if r['needed_qty'] is None else r['needed_qty'] - r['planned_qty']
        r['post_qty'] = r['qty'] + r['planned_qty'] * (1 if r['signal'] == '매수' else -1 if r['signal'] == '매도' else 0)
    post_nav = sum((convert(r['post_qty'] * r['price'], r['currency']) for r in rows), D(0))
    post_nav += sum((convert(c['post_total'] + c['rp'], c['currency']) for c in funding), D(0))
    for r in rows:
        r['post_weight'] = convert(r['post_qty'] * r['price'], r['currency']) / post_nav * 100 if post_nav else D(0)
        r['outside_after'] = not dec(r['lower']) <= r['post_weight'] <= dec(r['upper'])
    agg = lambda field, currency: sum((c[field] for c in funding if c['currency'] == currency), D(0))
    cma = {c['currency']: c for c in funding if c['account'] == 'CMA'}
    cash_plan = cash_rebalance(agg('post_total', 'KRW'), agg('post_total', 'USD'), fx,
                              cma['KRW']['post_available'], cma['USD']['post_available'],
                              agg('rp', 'KRW'), agg('rp', 'USD'), policy=cash_policy)
    cash_plan['deferred'] = any(r['signal'] != '유지' and (r['needed_qty'] is None or r['unfilled_qty'] > 0) for r in rows)
    return dict(nav=nav, post_nav=post_nav, rows=rows, orders=orders, funding=funding,
                cash_plan=cash_plan, include_sales=include_sales,
                estimated_cost=nav - post_nav)
