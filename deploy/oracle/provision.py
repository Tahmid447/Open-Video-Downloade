"""Single-server Always Free state machine, using private Object Storage receipts.

Adapted from the local guarded OCI launch procedure. No paid shape fallback,
blind retries, resource deletion, provider sessions, or media uploads are allowed.
"""
import base64
import datetime as dt
import html
import json
import re
import time
import uuid
from pathlib import Path

import oci
from oci._vendor import requests

REGION = 'ap-tokyo-1'
SHAPE = 'VM.Standard.A1.Flex'
STATE_OBJECT = 'capacity-watch/state.json'
FREE_DOC = 'https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm'


class SafetyStop(Exception):
    pass


def require(value, label):
    if not value:
        raise SafetyStop(label)


def check_inventory(instances, boots, volumes, state, compartment):
    """Unknown, terminating, resized or duplicate allocations always stop work."""
    require(len(instances) <= 1 and len(boots) <= 1 and len(volumes) <= 1, 'RESOURCE_COUNT_CHANGED')
    for vm in instances:
        require(vm.compartment_id == compartment and vm.shape == SHAPE and
            vm.shape_config.ocpus == 2 and vm.shape_config.memory_in_gbs == 12 and
            vm.freeform_tags.get('ovd-deployment') == state['deployment_id'], 'INSTANCE_MISMATCH')
        require(vm.lifecycle_state in {'PROVISIONING','STARTING','RUNNING','STOPPING','STOPPED'}, 'INSTANCE_STATE_UNSAFE')
        require(not state.get('instance_id') or vm.id == state['instance_id'], 'INSTANCE_ID_CHANGED')
    for disk in boots + volumes:
        require(disk.compartment_id == compartment and disk.lifecycle_state in {'PROVISIONING','AVAILABLE','RESTORING'}, 'VOLUME_STATE_UNSAFE')
        require(disk.vpus_per_gb in (0,10), 'PAID_VOLUME_PERFORMANCE')
    for disk in boots:
        require(bool(instances) and disk.size_in_gbs == 50, 'BOOT_VOLUME_MISMATCH')
    for disk in volumes:
        require(disk.size_in_gbs == 150 and disk.freeform_tags.get('ovd-deployment') == state['deployment_id'], 'DATA_VOLUME_MISMATCH')
        require(not state.get('volume_id') or disk.id == state['volume_id'], 'DATA_VOLUME_ID_CHANGED')
    require(sum(v.size_in_gbs for v in boots + volumes) <= 200, 'STORAGE_ALLOWANCE_EXCEEDED')
    require(not state.get('instance_id') or bool(instances), 'APPROVED_INSTANCE_DISAPPEARED')
    require(not state.get('volume_id') or bool(volumes), 'APPROVED_VOLUME_DISAPPEARED')


def verify_published_free_allowance():
    response = requests.get(FREE_DOC, timeout=30, allow_redirects=False)
    require(response.status_code == 200, 'FREE_TERMS_UNAVAILABLE')
    text = html.unescape(re.sub('<[^>]+>', ' ', response.text))
    text = re.sub(r'\s+', ' ', text)
    # Deliberately stop if the published terms change; never use trial limits.
    require(all(re.search(pattern, text, re.I) for pattern in (
        r'1,500\s+OCPU\s+hours', r'9,000\s+GB\s+hours',
        r'200\s+GB', r'home region', r'Always Free')), 'FREE_TERMS_CHANGED')


class Receipt:
    def __init__(self, client, config):
        self.client, self.config = client, config
        self.etag = None
        response = client.get_object(config['namespace'], config['bucket'], STATE_OBJECT)
        require(len(response.data.content) < 32768, 'RECEIPT_TOO_LARGE')
        self.state = json.loads(response.data.content)
        self.etag = response.headers['etag']
        require(self.state.get('version') == 1 and self.state.get('deployment_id'), 'INVALID_RECEIPT')

    def save(self):
        body = json.dumps(self.state, sort_keys=True).encode()
        require(len(body) < 32768, 'RECEIPT_TOO_LARGE')
        response = self.client.put_object(self.config['namespace'], self.config['bucket'], STATE_OBJECT,
            body, if_match=self.etag, content_type='application/json')
        self.etag = response.headers['etag']

    def acquire(self):
        now = time.time()
        require(self.state.get('lease_until', 0) < now, 'ANOTHER_WATCH_IS_RUNNING')
        self.state['lease_until'] = now + 900
        self.save()  # OCI If-Match prevents races beyond GitHub concurrency.

    def finish(self, status):
        changed = self.state.get('status') != status
        self.state.update(status=status, checked_at=dt.datetime.now(dt.timezone.utc).isoformat(), lease_until=0)
        self.save()
        return {'status':status, 'changed':changed}


class Watch:
    def __init__(self, config, signer):
        self.config = config
        c = {'tenancy':config['tenancy'], 'region':REGION}
        k = dict(signer=signer, retry_strategy=oci.retry.NoneRetryStrategy(), timeout=(15,45))
        self.iam = oci.identity.IdentityClient(c, **k)
        self.compute = oci.core.ComputeClient(c, **k)
        self.storage = oci.core.BlockstorageClient(c, **k)
        self.network = oci.core.VirtualNetworkClient(c, **k)
        self.limits = oci.limits.LimitsClient(c, **k)
        self.quotas = oci.limits.QuotasClient(c, **k)
        self.objects = oci.object_storage.ObjectStorageClient(c, **k)
        self.receipt = Receipt(self.objects, config)
        self.state = self.receipt.state

    @staticmethod
    def all(fn, **kw):
        return oci.pagination.list_call_get_all_results(fn, **kw).data

    def inventory(self):
        root = self.config['tenancy']
        regions = self.iam.list_region_subscriptions(root).data
        require([r.region_name for r in regions if r.is_home_region] == [REGION], 'HOME_REGION_CHANGED')
        require(all(r.region_name == REGION for r in regions), 'REGION_SUBSCRIPTIONS_CHANGED')
        ads = self.iam.list_availability_domains(root).data
        require(len(ads) == 1, 'AVAILABILITY_DOMAINS_CHANGED')
        self.ad = ads[0].name
        compartments = self.all(self.iam.list_compartments, compartment_id=root,
            compartment_id_in_subtree=True, access_level='ANY', lifecycle_state='ACTIVE')
        expected = {root, self.config['compartment_id']}
        require({root, *(v.id for v in compartments)} == expected, 'COMPARTMENTS_CHANGED')
        instances, boots, volumes = [], [], []
        for compartment in sorted(expected):
            instances += self.all(self.compute.list_instances, compartment_id=compartment)
            boots += self.all(self.storage.list_boot_volumes, compartment_id=compartment, availability_domain=self.ad)
            volumes += self.all(self.storage.list_volumes, compartment_id=compartment)
        live = lambda values: [v for v in values if v.lifecycle_state != 'TERMINATED']
        instances, boots, volumes = map(live, (instances, boots, volumes))
        check_inventory(instances, boots, volumes, self.state, self.config['compartment_id'])
        # A boot disk must be attached to the sole approved server, not just sized correctly.
        if boots and instances:
            attachments = self.all(self.compute.list_boot_volume_attachments,
                availability_domain=self.ad, compartment_id=self.config['compartment_id'], instance_id=instances[0].id)
            require(any(a.boot_volume_id == boots[0].id and a.lifecycle_state in {'ATTACHING','ATTACHED'} for a in attachments), 'UNKNOWN_BOOT_ATTACHMENT')
        quota = self.quotas.get_quota(self.config['quota_id']).data
        require(quota.lifecycle_state == 'ACTIVE' and quota.statements == self.config['quota_statements'], 'HARD_QUOTAS_CHANGED')
        availability = self.limits.get_resource_availability(service_name='block-storage',
            limit_name='total-free-storage-gb-regional', compartment_id=root).data
        used = sum(v.size_in_gbs for v in boots + volumes)
        require(availability.used == used and availability.available >= 200 - used, 'FREE_STORAGE_GUARD_STOP')
        for vm in instances:
            self.state['instance_id'] = vm.id
        for disk in volumes:
            self.state['volume_id'] = disk.id
        self.receipt.save()
        return instances, boots, volumes

    def capacity(self):
        fds = [None] + [v.name for v in self.iam.list_fault_domains(self.config['tenancy'], self.ad).data]
        reports = []
        for fd in fds:
            details = oci.core.models.CreateComputeCapacityReportDetails(
                compartment_id=self.config['compartment_id'], availability_domain=self.ad,
                shape_availabilities=[oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                    fault_domain=fd, instance_shape=SHAPE,
                    instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(ocpus=2,memory_in_gbs=12))])
            report = self.compute.create_compute_capacity_report(details).data
            reports.extend({'fault_domain':fd,'status':r.availability_status} for r in report.shape_availabilities)
        self.state['capacity_reports'] = reports
        self.receipt.save()
        return [r for r in reports if r['status'] == 'AVAILABLE']

    def run(self):
        self.receipt.acquire()
        try:
            instances, boots, volumes = self.inventory()
            if instances:
                return self.receipt.finish('APPROVED_INSTANCE_EXISTS')
            if self.state.get('launch_pending'):
                return self.receipt.finish('UNCERTAIN_LAUNCH_REQUIRES_RECONCILIATION')
            available = self.capacity()
            if not available:
                return self.receipt.finish('CAPACITY_UNAVAILABLE')
            verify_published_free_allowance()
            return self.receipt.finish('CAPACITY_AVAILABLE_VERIFIED')
        except SafetyStop as error:
            return self.receipt.finish(str(error))
