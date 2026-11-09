from datetime import UTC

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prices import PriceTable
from evowiki.llm.scripted import ScriptedChatProvider
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import GROUPS, UsageControl
from evowiki.service.app import create_app
from evowiki.store.usage import BudgetNotConfirmed, PgUsageMeter, get_settings, update_settings
from evowiki.store.users import UnknownUser
from tests.runtime.helpers import make_user
from tests.service.conftest import SURVEY, H


def test_new_account_cannot_spend_before_explicit_budget_confirmation(services):
    c = TestClient(create_app(services))
    assert c.post('/users', json={'user_id': 'new-budget', 'survey': SURVEY}).status_code == 201
    s = c.get('/usage/settings', headers=H('new-budget')).json()
    assert (s['daily_limit_usd'], s['monthly_limit_usd'], s['budget_confirmed']) == (5, 30, False)
    meter = PgUsageMeter(services.pool, clock=services.clock, tz=UTC)
    account = Account(user_id='new-budget', group='assistant')
    with pytest.raises(BudgetNotConfirmed):
        meter.check(account)
    c.patch('/usage/settings', json={'daily_limit_usd': None}, headers=H('new-budget'))
    with pytest.raises(BudgetNotConfirmed):
        meter.check(account)
    response = c.patch('/usage/settings', json={'monthly_limit_usd': 12, 'confirm_budget': True}, headers=H('new-budget'))
    assert response.status_code == 200 and response.json()['budget_confirmed'] is True
    meter.check(account)


def test_existing_learner_keeps_unlimited_and_needs_no_new_confirmation(services):
    make_user(services.pool, "u1")
    with services.pool.connection() as conn:
        s = get_settings(conn, 'u1')
    assert s.daily_limit_usd is None and s.monthly_limit_usd is None and s.budget_confirmed
    PgUsageMeter(services.pool, clock=services.clock, tz=UTC).check(Account(user_id='u1', group='assistant'))


def test_every_paid_group_and_both_gateway_paths_wait_for_confirmation(services, tmp_path):
    class Reply(BaseModel):
        value: int
    client = TestClient(create_app(services))
    assert client.post('/users', json={'user_id': 'paid-gates', 'survey': SURVEY}).status_code == 201
    meter = PgUsageMeter(services.pool, clock=services.clock, tz=UTC)
    provider = ScriptedChatProvider('scripted', lambda *a: ChatTurn(text='fake answer'), lambda *a: '{"value":1}')
    ref = ModelRef(provider='scripted', model='fake')
    gateway = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                      CallLog(tmp_path / 'budget-calls.jsonl'), mode='record',
                      providers={'scripted': provider}, usage=UsageControl(meter, PriceTable({})))
    request = LLMRequest(purpose='budget.test', prompt_version='test', subject='first', system='', prompt='test')
    chat = ChatRequest(purpose='assistant.turn', prompt_version='test', subject='first', system='',
                       messages=(ChatMessage(role='user', text='test'),))
    for group in GROUPS:
        account = Account(user_id='paid-gates', group=group)
        with pytest.raises(BudgetNotConfirmed):
            gateway.call('generator', request, Reply, account=account)
        with pytest.raises(BudgetNotConfirmed):
            gateway.converse('generator', chat, account=account)
    assert provider.calls == [] and provider.chat_calls == []
    response = client.patch('/usage/settings', headers=H('paid-gates'), json={'confirm_budget': True})
    assert response.status_code == 200
    assert gateway.call('generator', request, Reply, account=account)[0].value == 1
    assert gateway.converse('generator', chat, account=account)[0].text == 'fake answer'
    assert len(provider.calls) == len(provider.chat_calls) == 1


def test_unregistered_uid_cannot_get_unlimited_paid_access_even_with_settings(services):
    with services.pool.connection() as conn:
        update_settings(conn, 'unknown-budget', now=services.clock(), daily_limit_usd=None)
    with pytest.raises(UnknownUser):
        PgUsageMeter(services.pool, clock=services.clock, tz=UTC).check(Account(user_id='unknown-budget', group='assistant'))


@pytest.mark.parametrize(('method', 'path', 'body'), [
    ('GET', '/usage', None), ('GET', '/usage/settings', None),
    ('PATCH', '/usage/settings', {'confirm_budget': True}),
])
def test_usage_api_requires_a_registered_identity(services, method, path, body):
    client = TestClient(create_app(services))
    response = client.request(method, path, headers=H('unknown'), json=body)
    assert response.status_code == 404
    with services.pool.connection() as conn:
        assert conn.execute("select 1 from usage_settings where user_id='unknown'").fetchone() is None


def test_registration_initializes_preexisting_picker_row_and_duplicate_preserves_confirmed_budget(services):
    client = TestClient(create_app(services))
    with services.pool.connection() as conn:
        update_settings(conn, 'before-signup', now=services.clock(), daily_limit_usd=None)
    body = {'user_id': 'before-signup', 'survey': SURVEY}
    assert client.post('/users', json=body).status_code == 201
    settings = client.get('/usage/settings', headers=H('before-signup')).json()
    assert (settings['daily_limit_usd'], settings['monthly_limit_usd'], settings['budget_confirmed']) == (5, 30, False)
    assert client.patch('/usage/settings', headers=H('before-signup'), json={'daily_limit_usd': 2, 'confirm_budget': True}).status_code == 200
    assert client.post('/users', json=body).status_code == 409
    settings = client.get('/usage/settings', headers=H('before-signup')).json()
    assert settings['daily_limit_usd'] == 2 and settings['budget_confirmed'] is True


@pytest.mark.parametrize('value', ['true', 1, None])
def test_confirmation_requires_an_explicit_json_boolean(services, value):
    client = TestClient(create_app(services))
    client.post('/users', json={'user_id': 'explicit', 'survey': SURVEY})
    assert client.patch('/usage/settings', headers=H('explicit'), json={'confirm_budget': value}).status_code == 422
    assert client.get('/usage/settings', headers=H('explicit')).json()['budget_confirmed'] is False


def test_confirmation_and_limits_roll_back_together_on_failure(services, monkeypatch):
    from evowiki.service import routes_usage

    client = TestClient(create_app(services), raise_server_exceptions=False)
    client.post('/users', json={'user_id': 'atomic-budget', 'survey': SURVEY})
    def fail(*args):
        raise RuntimeError('simulated response read failure')
    monkeypatch.setattr(routes_usage, 'get_settings', fail)
    response = client.patch('/usage/settings', headers=H('atomic-budget'), json={'daily_limit_usd': None, 'confirm_budget': True})
    assert response.status_code == 500
    with services.pool.connection() as conn:
        settings = get_settings(conn, 'atomic-budget')
    assert settings.daily_limit_usd == 5 and settings.budget_confirmed is False


def test_unconfirmed_assistant_stream_explains_budget_without_a_provider_call(services, tmp_path):
    from evowiki.assistant.contracts import MessageIn
    from evowiki.assistant.loop import run_turn
    from evowiki.service.sse import error_payload
    from tests.assistant.test_loop import INFO, setup

    client = TestClient(create_app(services))
    assert client.post('/users', json={'user_id': INFO.user_id, 'survey': SURVEY}).status_code == 201
    meter = PgUsageMeter(services.pool, clock=services.clock, tz=UTC)
    deps, provider, _ = setup(tmp_path, [ChatTurn(text='must not be called')], meter=meter)
    events = list(run_turn(deps, INFO, MessageIn(text='first lesson')))
    error = next(event for event in events if event.type == 'error')
    assert error.code == 'refused' and error.retryable is False
    assert '确认模型费用和使用预算' in error.detail
    assert provider.chat_calls == []
    with pytest.raises(BudgetNotConfirmed) as info:
        meter.check(Account(user_id=INFO.user_id, group='page'))
    assert error_payload(info.value) == {'code': 'refused', 'retryable': False, 'detail': error.detail}
