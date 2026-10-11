"""Authentication boundaries use synthetic tokens only; no cloud calls."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

spec = importlib.util.spec_from_file_location('oracle_watch', Path(__file__).parents[1] / 'deploy/oracle/watch.py')
pytest.importorskip('oci')
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


@pytest.fixture
def env(monkeypatch):
    values = {'GITHUB_REPOSITORY':watch.REPOSITORY, 'GITHUB_REF':'refs/heads/main',
        'GITHUB_EVENT_NAME':'workflow_dispatch',
        'OCI_DOMAIN_URL':'https://example.identity.oraclecloud.com:443',
        'OCI_CLIENT_ID':'synthetic-client', 'OCI_CLIENT_SECRET':'synthetic-fixture',
        'ACTIONS_ID_TOKEN_REQUEST_URL':'https://example.com/oidc?request=fixture',
        'ACTIONS_ID_TOKEN_REQUEST_TOKEN':'synthetic-fixture'}
    for k,v in values.items():monkeypatch.setenv(k,v)
    get=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:{'value':'synthetic-jwt'}))
    post=Mock(return_value=SimpleNamespace(status_code=200,json=lambda:{'token':'synthetic-rpst'}))
    monkeypatch.setattr(watch.requests,'get',get);monkeypatch.setattr(watch.requests,'post',post)
    signer=Mock(return_value='synthetic-signer');monkeypatch.setattr(watch.oci.auth.signers,'SecurityTokenSigner',signer)
    return get,post,signer


@pytest.mark.parametrize(('key','value'),[
    ('GITHUB_REPOSITORY','untrusted/fork'),('GITHUB_REF','refs/heads/other'),
    ('GITHUB_EVENT_NAME','pull_request_target'),('GITHUB_EVENT_NAME','push'),
    ('OCI_DOMAIN_URL','http://example.identity.oraclecloud.com'),
    ('OCI_DOMAIN_URL','https://example.identity.oraclecloud.com.evil.example'),
    ('OCI_DOMAIN_URL','https://example.identity.oraclecloud.com:8443'),
    ('OCI_DOMAIN_URL','https://user@example.identity.oraclecloud.com'),
])
def test_untrusted_context_stops_before_network(env,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(watch.GuardStop):watch.authenticate()
    env[0].assert_not_called();env[1].assert_not_called()


def test_positive_exchange_uses_ephemeral_proof_and_tls(env):
    assert watch.authenticate()=='synthetic-signer'
    kw=env[1].call_args.kwargs
    assert kw['data']['rpst_exp']=='20'
    assert watch.serialization.load_der_public_key(watch.base64.b64decode(kw['data']['public_key'])).key_size == 3072
    assert kw.get('verify',True) is True and kw['allow_redirects'] is False
    assert env[2].call_args.args[0]=='synthetic-rpst'


def test_negative_audience_must_be_rejected(env):
    with pytest.raises(watch.GuardStop,match='AUDIENCE_REJECTION_NOT_VERIFIED'):
        watch.authenticate(negative_audience=True)
    env[2].assert_not_called()


def test_negative_audience_rejection_is_verified(env):
    env[1].return_value.status_code=401
    assert watch.authenticate(negative_audience=True) is None
    assert 'ovd-negative-audience-test' in env[0].call_args.args[0]
    env[2].assert_not_called()


def test_private_error_body_is_never_propagated(env):
    env[1].return_value.status_code=403
    env[1].return_value.text='synthetic-sensitive-error-body'
    with pytest.raises(watch.GuardStop,match='^OCI_TOKEN_EXCHANGE_HTTP_403$'):
        watch.authenticate()
