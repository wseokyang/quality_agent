import json
import time

import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from quality_api.auth import issue_token
from quality_api.config import Settings


def test_missing_and_forged_auth_rejected(client, settings):
    assert client.get('/quality/v1/cases').status_code == 401
    now = int(time.time())
    claims = {'sub': 'untrusted', 'user_id': 'alice', 'instance_id': 'test-instance', 'chat_id': 'chat-1',
              'kind': 'human', 'iss': settings.jwt_issuer, 'aud': settings.jwt_audience,
              'iat': now, 'exp': now + 60, 'epoch': settings.recovery_epoch}
    token = jwt.encode(claims, settings.model_jwt_secret, algorithm='HS256', headers={'kid': 'model_delegate'})
    assert client.get('/quality/v1/cases', headers={'Authorization': 'Bearer ' + token}).status_code == 401
    assert client.get('/quality/v1/cases', headers={'X-User-Id': 'alice', 'X-Role': 'human'}).status_code == 401


def test_expired_epoch_and_excessive_lifetime_rejected(client, settings, headers):
    old = headers()
    settings.recovery_epoch = 'rotated-epoch'
    assert client.get('/quality/v1/cases', headers=old).status_code == 401
    long = issue_token(settings, kind='human', user_id='alice', instance_id='test-instance', ttl=3600)
    assert client.get('/quality/v1/cases', headers={'Authorization': 'Bearer ' + long}).status_code == 401


def test_model_bootstrap_only_and_header_identity_ignored(client, headers):
    response = client.get('/quality/v1/context/current', headers=headers(kind='model_delegate'))
    assert response.status_code == 200
    assert response.json()['data']['case'] is None
    assert client.get('/quality/v1/cases', headers=headers(kind='model_delegate')).status_code == 401
    h = headers()
    h['X-User-Id'] = 'administrator'
    assert client.get('/quality/v1/cases', headers=h).status_code == 200


def test_ui_ticket_one_use_and_no_signing_secret(client, headers, settings):
    ticket = client.post('/quality/v1/ui/tickets', headers=headers()).json()['data']['ticket']
    exchanged = client.post('/quality/v1/ui/exchange', json={'ticket': ticket})
    assert exchanged.status_code == 200
    data = exchanged.json()['data']
    assert data['context']['user_id'] == 'alice'
    assert data['expires_in'] <= 300
    for secret in settings.signing_keys.values():
        assert secret not in exchanged.text
    assert client.post('/quality/v1/ui/exchange', json={'ticket': ticket}).status_code == 401
    token = data['access_token']
    assert client.get('/quality/v1/cases', headers={'Authorization': 'Bearer ' + token}).status_code == 200


def test_health_readiness_and_non_disclosure(client, database, settings):
    assert client.get('/health/live').status_code == 200
    assert client.get('/health/ready').status_code == 200
    with database.engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='not-supported'"))
    response = client.get('/health/ready')
    assert response.status_code == 503
    assert settings.database_url not in response.text
    assert client.get('/health/live').status_code == 200


def test_invalid_json_and_request_limit(client, settings, headers):
    h = {**headers(), 'Content-Type': 'application/json'}
    assert client.post('/quality/v1/cases', headers=h, content='{"purpose":NaN}').status_code == 422
    settings.max_request_bytes = 100
    assert client.post('/quality/v1/cases', headers=h, content='x' * 101).status_code == 413


def test_recovery_blocks_changes_not_live(client, settings, headers):
    settings.recovery_mode = True
    response = client.post('/quality/v1/cases', headers=headers(), json={'title': 't', 'purpose': 'p'})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'RECOVERY_MODE'
    assert client.get('/health/live').status_code == 200
    assert client.get('/health/ready').status_code == 503


def test_operator_drain_authorization(client, headers):
    assert client.post('/quality/v1/ops/drain', headers=headers(), json={'enabled': True}).status_code == 403
    assert client.post('/quality/v1/ops/drain', headers=headers(kind='worker'), json={'enabled': True}).status_code == 403
    assert client.post('/quality/v1/ops/drain', headers=headers(kind='worker', subject='quality:operator'), json={'enabled': True}).status_code == 200
    assert client.get('/health/ready').json()['draining'] is True
    assert client.post('/quality/v1/cases/fake/runs', headers=headers(), json={}).status_code == 503


def test_request_id_and_validation_do_not_echo_body(client, headers):
    response = client.get('/quality/v1/cases', headers=headers())
    assert response.json()['request_id'] == response.headers['x-request-id']
    bad = client.post('/quality/v1/cases', headers=headers(), json={'title': 't', 'purpose': 'p', 'secret': 'do-not-echo-me'})
    assert bad.status_code == 422
    assert 'do-not-echo-me' not in bad.text


def test_config_does_not_expose_secrets(settings):
    assert settings.human_jwt_secret not in repr(settings)
    with pytest.raises(ValidationError) as caught:
        Settings(**{**settings.model_dump(), 'human_jwt_secret': 'do-not-echo-config-secret'})
    assert 'do-not-echo-config-secret' not in str(caught.value)


def test_tool_registry_rejects_remote_refs(settings):
    values = settings.model_dump()
    values['tool_registry_json'] = json.dumps([{'id': 'bad', 'version': '1', 'effect': 'read',
                                               'input_schema': {'$ref': 'https://example.invalid/schema'}}])
    with pytest.raises(ValidationError):
        Settings(**values)
