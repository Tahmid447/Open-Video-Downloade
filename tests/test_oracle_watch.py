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
    with pytest.raises(watch.GuardStop,match='^OCI_TOKEN_EXCHANGE_HTTP_403_UNCLASSIFIED$'):
        watch.authenticate()

spec2=importlib.util.spec_from_file_location('oracle_provision',Path(__file__).parents[1]/'deploy/oracle/provision.py')
provision=importlib.util.module_from_spec(spec2);spec2.loader.exec_module(provision)


def resources():
    vm=SimpleNamespace(id='synthetic-instance',compartment_id='synthetic-compartment',shape=provision.SHAPE,
        shape_config=SimpleNamespace(ocpus=2,memory_in_gbs=12),freeform_tags={'ovd-deployment':'synthetic-deployment'},lifecycle_state='RUNNING')
    boot=SimpleNamespace(id='synthetic-boot',compartment_id=vm.compartment_id,size_in_gbs=50,vpus_per_gb=10,lifecycle_state='AVAILABLE')
    data=SimpleNamespace(id='synthetic-data',compartment_id=vm.compartment_id,size_in_gbs=150,vpus_per_gb=10,lifecycle_state='AVAILABLE',freeform_tags=vm.freeform_tags)
    state={'deployment_id':'synthetic-deployment'}
    return vm,boot,data,state


def test_exact_allocation_passes():
    vm,boot,data,state=resources()
    provision.check_inventory([vm],[boot],[data],state,vm.compartment_id)


@pytest.mark.parametrize('change',['duplicate','paid-shape','cpu','ram','disk','performance','unknown','disappeared','terminating'])
def test_resource_drift_fails_closed(change):
    vm,boot,data,state=resources();instances=[vm]
    if change=='duplicate':instances.append(vm)
    if change=='paid-shape':vm.shape='VM.Standard.E4.Flex'
    if change=='cpu':vm.shape_config.ocpus=4
    if change=='ram':vm.shape_config.memory_in_gbs=24
    if change=='disk':data.size_in_gbs=151
    if change=='performance':data.vpus_per_gb=20
    if change=='unknown':vm.freeform_tags={'ovd-deployment':'other'}
    if change=='disappeared':state['instance_id']='another-instance'
    if change=='terminating':vm.lifecycle_state='TERMINATING'
    with pytest.raises(provision.SafetyStop):
        provision.check_inventory(instances,[boot],[data],state,vm.compartment_id)


def test_missing_server_never_recreates_known_instance():
    with pytest.raises(provision.SafetyStop,match='APPROVED_INSTANCE_DISAPPEARED'):
        provision.check_inventory([],[],[],{'deployment_id':'synthetic','instance_id':'old-instance'},'synthetic')


def test_receipt_conflict_prevents_work():
    client=Mock();config={'namespace':'synthetic','bucket':'synthetic'}
    client.get_object.return_value=SimpleNamespace(data=SimpleNamespace(content=b'{"version":1,"deployment_id":"synthetic","lease_until":0}'),headers={'etag':'old'})
    client.put_object.side_effect=RuntimeError('synthetic compare-and-swap conflict')
    receipt=provision.Receipt(client,config)
    with pytest.raises(RuntimeError,match='compare-and-swap'):receipt.acquire()
    assert client.put_object.call_args.kwargs['if_match']=='old'


def test_changed_free_terms_prevent_launch(monkeypatch):
    monkeypatch.setattr(provision.requests,'get',Mock(return_value=SimpleNamespace(status_code=200,text='Trial credits for larger resources')))
    with pytest.raises(provision.SafetyStop,match='FREE_TERMS_CHANGED'):
        provision.verify_published_free_allowance()

spec3=importlib.util.spec_from_file_location('cloud_init',Path(__file__).parents[1]/'deploy/oracle/cloud_init.py')
cloud_init=importlib.util.module_from_spec(spec3);spec3.loader.exec_module(cloud_init)


@pytest.mark.parametrize('cidr',['0.0.0.0/0','10.0.0.1/32','8.8.8.0/24','::/0'])
def test_bootstrap_rejects_broad_or_private_ssh(cidr):
    with pytest.raises(ValueError):cloud_init.render('a'*40,cidr)


def test_bootstrap_is_hash_pinned_and_shell_parses():
    import subprocess
    script=cloud_init.render('a'*40,'8.8.8.8/32')
    subprocess.run(['bash','-n'],input=script,text=True,check=True)
    assert 'sha256sum --check --status' in script
    assert '/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/' in script
    assert 'PRIVATE KEY' not in script


def prepared_launcher(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules,'cloud_init',cloud_init)
    job=provision.Watch.__new__(provision.Watch)
    job.config={'compartment_id':'synthetic-compartment','image_id':'synthetic-image',
        'ssh_public_key':'ssh-ed25519 synthetic-public-fixture','source_sha':'a'*40,
        'owner_ssh_cidr':'8.8.8.8/32','subnet_id':'synthetic-subnet','armed':True}
    job.state={'deployment_id':'synthetic-deployment'}
    job.ad='synthetic-ad';job.receipt=Mock();job.verify_network=Mock();job.compute=Mock()
    job.compute.get_image.return_value.data=SimpleNamespace(id='synthetic-image',compartment_id=None,
        lifecycle_state='AVAILABLE',operating_system='Canonical Ubuntu',operating_system_version='24.04',display_name='Ubuntu-aarch64')
    job.continue_installation=Mock(return_value={'status':'PRIVATE_INSTALLATION_IN_PROGRESS'})
    job.inventory=Mock(return_value=([],[],[]))
    return job


def test_launch_request_is_fixed_and_durably_marked(monkeypatch):
    job=prepared_launcher(monkeypatch)
    def launch(details,**kwargs):
        assert job.receipt.save.called and job.state.get('launch_pending')
        assert details.shape=='VM.Standard.A1.Flex' and details.shape_config.ocpus==2
        assert details.shape_config.memory_in_gbs==12
        assert details.source_details.boot_volume_size_in_gbs==50
        assert details.source_details.boot_volume_vpus_per_gb==10
        assert details.create_vnic_details.assign_ipv6_ip is False
        assert kwargs['opc_retry_token']==job.state['launch_pending']['retry_token']
        return SimpleNamespace(data=SimpleNamespace(id='synthetic-instance'))
    job.compute.launch_instance.side_effect=launch
    job.launch(None)
    assert job.state['instance_id']=='synthetic-instance' and 'launch_pending' not in job.state


def test_ambiguous_launch_retains_receipt_and_never_retries(monkeypatch):
    job=prepared_launcher(monkeypatch)
    job.compute.launch_instance.side_effect=TimeoutError('synthetic network timeout')
    with pytest.raises(TimeoutError):job.launch(None)
    assert job.state['launch_pending']
    assert job.compute.launch_instance.call_count==1
    job.continue_installation.assert_not_called()


def test_capacity_rejection_reconciles_before_clearing_receipt(monkeypatch):
    job=prepared_launcher(monkeypatch)
    job.compute.launch_instance.side_effect=watch.oci.exceptions.ServiceError(500,'InternalError',{},'Out of host capacity.')
    job.launch(None)
    job.inventory.assert_called_once()
    assert 'launch_pending' not in job.state
    job.receipt.finish.assert_called_with('CAPACITY_UNAVAILABLE')


def test_unarmed_watch_cannot_launch(monkeypatch):
    job=prepared_launcher(monkeypatch);job.config['armed']=False
    with pytest.raises(provision.SafetyStop,match='WATCH_NOT_ARMED'):job.launch(None)
    job.compute.launch_instance.assert_not_called()
