"""Single-server Always Free state machine, using private Object Storage receipts.

Adapted from the local guarded OCI launch procedure. No paid shape fallback,
blind retries, resource deletion, provider sessions, or media uploads are allowed.
"""
import base64
import datetime as dt
import html
import hashlib
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
            require(vm.source_details.image_id == self.config['image_id'], 'INSTANCE_IMAGE_CHANGED')
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

    def verify_network(self):
        subnet = self.network.get_subnet(self.config['subnet_id']).data
        require(subnet.compartment_id == self.config['compartment_id'] and
            subnet.security_list_ids == [self.config['security_list_id']] and
            subnet.route_table_id == self.config['route_table_id'] and
            not subnet.ipv6_cidr_block, 'SUBNET_CHANGED')
        rules = self.network.get_security_list(self.config['security_list_id']).data
        require(len(rules.ingress_security_rules) == 2, 'INGRESS_CHANGED')
        ssh=[r for r in rules.ingress_security_rules if r.protocol=='6']
        icmp=[r for r in rules.ingress_security_rules if r.protocol=='1']
        require(len(ssh)==1 and len(icmp)==1 and icmp[0].source=='0.0.0.0/0' and
            icmp[0].icmp_options.type==3 and icmp[0].icmp_options.code==4, 'INGRESS_CHANGED')
        rule = ssh[0]
        require(rule.source == self.config['owner_ssh_cidr'] and rule.protocol == '6' and
            not rule.is_stateless and rule.tcp_options and
            rule.tcp_options.destination_port_range.min == 22 and
            rule.tcp_options.destination_port_range.max == 22, 'SSH_NOT_RESTRICTED')
        routes = self.network.get_route_table(self.config['route_table_id']).data.route_rules
        require(len(routes) == 1 and routes[0].network_entity_id == self.config['internet_gateway_id'] and
            routes[0].destination == '0.0.0.0/0', 'ROUTING_CHANGED')

    def launch(self, fault_domain):
        from cloud_init import render
        self.verify_network()
        require(self.config.get('armed') is True, 'WATCH_NOT_ARMED')
        image = self.compute.get_image(self.config['image_id']).data
        require(image.compartment_id is None and image.lifecycle_state == 'AVAILABLE' and
            image.operating_system == 'Canonical Ubuntu' and image.operating_system_version == '24.04' and
            'aarch64' in image.display_name, 'PLATFORM_IMAGE_CHANGED')
        require(self.config['ssh_public_key'].startswith('ssh-ed25519 ') and
            len(self.config['ssh_public_key'].splitlines()) == 1, 'INVALID_OWNER_PUBLIC_KEY')
        require(hashlib.sha256(Path(__file__).with_name('bootstrap.sh').read_bytes()).hexdigest()==self.config['bootstrap_sha256'], 'BOOTSTRAP_REVISION_CHANGED')
        source = render(self.config['source_sha'], self.config['owner_ssh_cidr'])
        token = str(uuid.uuid4())
        self.state['launch_pending'] = {'retry_token':token, 'time':time.time()}
        self.receipt.save()  # Durable before any compute mutation.
        details = oci.core.models.LaunchInstanceDetails(
            compartment_id=self.config['compartment_id'], availability_domain=self.ad,
            fault_domain=fault_domain, display_name='open-video-downloader-free-arm64',
            shape=SHAPE, shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=2,memory_in_gbs=12),
            source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image.id,
                boot_volume_size_in_gbs=50,boot_volume_vpus_per_gb=10),
            create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=self.config['subnet_id'],
                assign_public_ip=True,assign_ipv6_ip=False),
            metadata={'ssh_authorized_keys':self.config['ssh_public_key'],
                'user_data':base64.b64encode(source.encode()).decode()},
            agent_config=oci.core.models.LaunchInstanceAgentConfigDetails(are_all_plugins_disabled=True),
            freeform_tags={'project':'open-video-downloader','ovd-deployment':self.state['deployment_id']})
        try:
            vm = self.compute.launch_instance(details,opc_retry_token=token).data
        except oci.exceptions.ServiceError as error:
            # Only Oracle's explicit capacity rejection is a retryable failure.
            # All transport/unknown outcomes retain launch_pending indefinitely.
            if error.status == 500 and 'out of host capacity' in (error.message or '').lower():
                instances, boots, volumes = self.inventory()
                if not instances and not boots and not volumes:
                    self.state.pop('launch_pending',None)
                    return self.receipt.finish('CAPACITY_UNAVAILABLE')
            raise
        self.state['instance_id'] = vm.id
        self.state.pop('launch_pending',None)
        self.receipt.save()
        # Installation runs on the VM independently of this bounded runner.
        return self.continue_installation(vm)

    def continue_installation(self, vm):
        self.verify_network()
        if vm.lifecycle_state != 'RUNNING':
            vm = oci.wait_until(self.compute,self.compute.get_instance(vm.id),
                'lifecycle_state','RUNNING',max_interval_seconds=15,max_wait_seconds=300).data
        instances, boots, volumes = self.inventory()
        require(len(boots)==1 and boots[0].size_in_gbs==50, 'BOOT_VOLUME_NOT_VERIFIED')
        if not volumes:
            require(not self.state.get('volume_pending'), 'UNCERTAIN_VOLUME_REQUIRES_RECONCILIATION')
            verify_published_free_allowance()
            self.state['volume_pending'] = {'retry_token':str(uuid.uuid4()),'time':time.time()}
            self.receipt.save()
            volume=self.storage.create_volume(oci.core.models.CreateVolumeDetails(
                compartment_id=self.config['compartment_id'],availability_domain=self.ad,
                display_name='open-video-downloader-private-data',size_in_gbs=150,vpus_per_gb=10,
                is_auto_tune_enabled=False,freeform_tags={'project':'open-video-downloader',
                    'ovd-deployment':self.state['deployment_id']}),
                opc_retry_token=self.state['volume_pending']['retry_token']).data
            self.state['volume_id']=volume.id
            self.state.pop('volume_pending',None)
            self.receipt.save()
        else:
            volume=volumes[0]
            self.state.pop('volume_pending',None)
        if volume.lifecycle_state!='AVAILABLE':
            volume=oci.wait_until(self.storage,self.storage.get_volume(volume.id),
                'lifecycle_state','AVAILABLE',max_interval_seconds=10,max_wait_seconds=120).data
        attachments=self.all(self.compute.list_volume_attachments,compartment_id=self.config['compartment_id'],instance_id=vm.id)
        live=[a for a in attachments if a.lifecycle_state!='DETACHED']
        require(len(live)<=1,'UNEXPECTED_VOLUME_ATTACHMENT')
        if live:
            a=live[0]
            require(a.volume_id==volume.id and a.attachment_type=='paravirtualized' and
                a.device=='/dev/oracleoci/oraclevdb' and not a.is_read_only,'VOLUME_ATTACHMENT_MISMATCH')
            self.state.pop('attach_pending',None)
        else:
            require(not self.state.get('attach_pending'),'UNCERTAIN_ATTACHMENT_REQUIRES_RECONCILIATION')
            self.state['attach_pending']={'retry_token':str(uuid.uuid4()),'time':time.time()}
            self.receipt.save()
            a=self.compute.attach_volume(oci.core.models.AttachParavirtualizedVolumeDetails(
                instance_id=vm.id,volume_id=volume.id,device='/dev/oracleoci/oraclevdb',
                is_read_only=False,is_shareable=False,is_pv_encryption_in_transit_enabled=True),
                opc_retry_token=self.state['attach_pending']['retry_token']).data
            self.state.pop('attach_pending',None)
        self.receipt.save()
        if a.lifecycle_state!='ATTACHED':
            return self.receipt.finish('PRIVATE_INSTALLATION_IN_PROGRESS')
        return self.inspect_private_installation(vm)

    def inspect_private_installation(self, vm):
        # Console history is authenticated by OCI, not unauthenticated ssh-keyscan.
        histories=self.all(self.compute.list_console_histories,compartment_id=self.config['compartment_id'],instance_id=vm.id)
        name='ovd-private-verification-'+self.state['deployment_id'][:8]
        live=[h for h in histories if h.display_name==name and h.lifecycle_state!='FAILED']
        for h in live:
            if h.lifecycle_state in {'REQUESTED','GETTING_HISTORY'}:
                return self.receipt.finish('PRIVATE_INSTALLATION_IN_PROGRESS')
            self.compute.delete_console_history(h.id)
        h=self.compute.capture_console_history(oci.core.models.CaptureConsoleHistoryDetails(instance_id=vm.id,display_name=name)).data
        h=oci.wait_until(self.compute,self.compute.get_console_history(h.id),'lifecycle_state','SUCCEEDED',max_interval_seconds=5,max_wait_seconds=60).data
        response=self.compute.get_console_history_content(h.id)
        output=response.data.text if hasattr(response.data,'text') else str(response.data)
        fingerprints=re.findall(r'OVD_HOST_ED25519_FINGERPRINT=(SHA256:[A-Za-z0-9+/]+)',output)
        if fingerprints:
            require(len(set(fingerprints))==1,'HOST_FINGERPRINT_CONFLICT')
            previous=self.state.get('host_ed25519_fingerprint')
            require(not previous or previous==fingerprints[-1],'HOST_FINGERPRINT_CHANGED')
            self.state['host_ed25519_fingerprint']=fingerprints[-1]
        boots=re.findall(r'OVD_PRIVATE_READY_BOOT=([a-f0-9-]{36})',output)
        self.compute.delete_console_history(h.id)
        if not boots:
            return self.receipt.finish('PRIVATE_INSTALLATION_IN_PROGRESS')
        require(self.state.get('host_ed25519_fingerprint'),'HOST_FINGERPRINT_NOT_VERIFIED')
        if not self.state.get('reboot_requested'):
            self.state['reboot_requested']={'initial_boot':boots[-1],'time':time.time()}
            self.receipt.save()  # A timed-out reboot request must never be repeated blindly.
            self.compute.instance_action(vm.id,'SOFTRESET')
            return self.receipt.finish('PRIVATE_REBOOT_VERIFICATION_IN_PROGRESS')
        if boots[-1]==self.state['reboot_requested']['initial_boot']:
            return self.receipt.finish('PRIVATE_REBOOT_VERIFICATION_IN_PROGRESS')
        self.state['private_installation_verified']=True
        self.state['verified_boot']=boots[-1]
        return self.receipt.finish('PRIVATE_INSTALLATION_READY_OWNER_ENROLLMENT_REQUIRED')

    def run(self):
        self.receipt.acquire()
        try:
            instances, boots, volumes = self.inventory()
            self.verify_network()
            if self.state.get('private_installation_verified'):
                return self.receipt.finish('PRIVATE_INSTALLATION_READY_OWNER_ENROLLMENT_REQUIRED')
            if instances:
                if not self.config.get('armed'):
                    return self.receipt.finish('APPROVED_INSTANCE_EXISTS')
                self.state.pop('launch_pending',None)
                return self.continue_installation(instances[0])
            if self.state.get('launch_pending'):
                return self.receipt.finish('UNCERTAIN_LAUNCH_REQUIRES_RECONCILIATION')
            available = self.capacity()
            if not available:
                return self.receipt.finish('CAPACITY_UNAVAILABLE')
            verify_published_free_allowance()
            if not self.config.get('armed'):
                return self.receipt.finish('CAPACITY_AVAILABLE_VERIFIED')
            return self.launch(available[0]['fault_domain'])
        except SafetyStop as error:
            return self.receipt.finish(str(error))
