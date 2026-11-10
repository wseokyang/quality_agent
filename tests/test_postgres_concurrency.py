"""Run against a DEDICATED PostgreSQL *_test database via QUALITY_TEST_DATABASE_URL."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from quality_api.models import Case, Run, ToolCall


@pytest.fixture(autouse=True)
def postgres_only(database):
    if database.engine.dialect.name != 'postgresql':
        pytest.skip('Real PostgreSQL locking test: set QUALITY_TEST_DATABASE_URL to a dedicated *_test database')


def parallel(fn, count=6):
    gate = Barrier(count)
    def call(i):
        gate.wait(timeout=20)
        return fn(i)
    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(call, range(count)))


def create_case(client, headers):
    r = client.post('/quality/v1/cases', headers=headers(), json={'title': '동시성 검증', 'purpose': 'fixture'})
    assert r.status_code == 200, r.text
    return r.json()['data']


def start_run(client, headers, case):
    r = client.post(f'/quality/v1/cases/{case["id"]}/runs', headers=headers(kind='trace_adapter', case_id=case['id']),
                    json={'request_id': 'request-1', 'user_message': 'read', 'input_ref_ids': [], 'expected_revision': case['revision']})
    assert r.status_code == 200, r.text
    return r.json()['data']


def test_concurrent_identical_case_creation(client, headers, db_factory):
    payload = {'title': '동시 케이스', 'purpose': 'fixture'}
    responses = parallel(lambda _: client.post('/quality/v1/cases', headers=headers(key='same-create'), json=payload))
    assert all(r.status_code == 200 for r in responses), [(r.status_code,r.text) for r in responses]
    assert len({r.json()['data']['id'] for r in responses}) == 1
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Case)) == 1


def test_same_key_conflicting_payload_is_not_two_rows(client, headers, db_factory):
    responses = parallel(lambda i: client.post('/quality/v1/cases', headers=headers(key='conflict'),
                                              json={'title': f'title-{i}', 'purpose': 'fixture'}))
    assert sum(r.status_code == 200 for r in responses) == 1, [(r.status_code,r.text) for r in responses]
    assert sum(r.status_code == 409 for r in responses) == 5
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Case)) == 1


def test_concurrent_run_retransmission_is_single_run(client, headers, db_factory):
    case = create_case(client, headers)
    payload = {'request_id': 'same-request', 'user_message': 'read', 'input_ref_ids': [], 'expected_revision': case['revision']}
    responses = parallel(lambda _: client.post(f'/quality/v1/cases/{case["id"]}/runs',
                           headers=headers(kind='trace_adapter', case_id=case['id'], key='same-run'), json=payload))
    assert all(r.status_code == 200 for r in responses), [(r.status_code,r.text) for r in responses]
    assert len({r.json()['data']['id'] for r in responses}) == 1
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 1


def test_claim_grants_execution_exactly_once_even_when_first_reply_lost(client, headers, db_factory):
    case = create_case(client, headers)
    run = start_run(client, headers, case)
    payload = {'invocation_id': 'same-call', 'tool_id': 'fixture_read', 'tool_version': '1', 'arguments': {}}
    def invoke(_):
        return client.post(f'/quality/v1/runs/{run["id"]}/calls/claim',
                           headers=headers(kind='trace_adapter', case_id=case['id'], run_id=run['id'], key='same-claim'), json=payload)
    responses = parallel(invoke)
    assert all(r.status_code == 200 for r in responses), [(r.status_code,r.text) for r in responses]
    assert sum(r.json()['data']['execute'] is True for r in responses) == 1
    # Discard the winning reply; even another retransmission cannot regain execution.
    replay = invoke(0).json()['data']
    assert replay['execute'] is False
    assert not replay.get('claim_token')
    with db_factory() as db:
        assert db.scalar(select(func.count()).select_from(ToolCall)) == 1


def test_independent_parallel_reads_do_not_share_an_invocation(client, headers):
    case = create_case(client, headers)
    run = start_run(client, headers, case)
    responses = parallel(lambda i: client.post(f'/quality/v1/runs/{run["id"]}/calls/claim',
                       headers=headers(kind='trace_adapter', case_id=case['id'], run_id=run['id']),
                       json={'invocation_id': f'call-{i}', 'tool_id': 'fixture_read', 'tool_version': '1', 'arguments': {}}))
    assert all(r.status_code == 200 and r.json()['data']['execute'] for r in responses), [(r.status_code,r.text) for r in responses]
    assert len({r.json()['data']['call_id'] for r in responses}) == 6


def test_ui_ticket_concurrent_exchange_is_single_use(client, headers):
    ticket = client.post('/quality/v1/ui/tickets', headers=headers()).json()['data']['ticket']
    responses = parallel(lambda _: client.post('/quality/v1/ui/exchange', json={'ticket': ticket}))
    assert sum(r.status_code == 200 for r in responses) == 1
    assert sum(r.status_code == 401 for r in responses) == 5
