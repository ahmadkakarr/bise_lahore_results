"""USB identity/routing installation regressions; no OS changes or live audio."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from youthgames_register import usb_setup as usb,connections as network

PLAN={'schema_version':1,'stable_interface':'usb-test','usb_vendor':'04e8','usb_serial':'TESTPHONE123',
      'networkmanager_profile_uuid':'7a929a33-c9b0-51d7-9199-0bc73dd23682','route_table':20040}


class UsbSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.path=self.root/'plan.json';self.path.write_text(json.dumps(PLAN))

    def test_exact_phone_identity_rule_is_independent_of_port_and_mac(self):
        plan=usb.load(self.path);text=usb.rule(plan)
        self.assertIn('ID_USB_VENDOR_ID',text);self.assertIn('ID_USB_SERIAL_SHORT',text)
        self.assertIn('TESTPHONE123',text);self.assertIn('NAME:="usb-test"',text)
        self.assertIn('SUBSYSTEM=="net"',text);self.assertIn('rndis_host|cdc_ether|cdc_ncm',text)
        self.assertNotIn('ATTR{address}',text);self.assertNotIn('KERNEL==',text)
        self.assertNotIn('RUN',text)

    def test_invalid_identity_and_rule_injection_are_rejected(self):
        for key,value in [('usb_serial','bad";RUN="evil'),('stable_interface','wlan0\n'),('usb_vendor','*'),('route_table',254),('schema_version',True)]:
            self.path.write_text(json.dumps({**PLAN,key:value}))
            with self.subTest(key=key),self.assertRaises(ValueError):usb.load(self.path)

    def test_rule_publication_is_idempotent_and_preserves_other_configuration(self):
        path=self.root/'rule.rules';text=usb.rule(PLAN)
        self.assertTrue(usb.publish(path,text));before=path.stat().st_mtime_ns
        self.assertFalse(usb.publish(path,text));self.assertEqual(path.stat().st_mtime_ns,before)
        with self.assertRaises(ValueError):usb.publish(path,'changed phone')
        self.assertEqual(path.read_text(),text)
        link=self.root/'link.rules';link.symlink_to(path)
        with self.assertRaises(ValueError):usb.publish(link,text)

    def test_no_root_permission_does_not_publish_or_reload(self):
        with patch.object(usb.os,'geteuid',return_value=1000),patch.object(usb,'publish') as publish,patch.object(usb,'command') as command:
            with self.assertRaisesRegex(ValueError,'sudo'):usb.install(PLAN)
        publish.assert_not_called();command.assert_not_called()

    def test_invalid_existing_profile_blocks_before_any_change(self):
        with patch.object(usb.os,'geteuid',return_value=0),patch.object(usb,'profile_issues',return_value=['unsafe DNS']),patch.object(usb,'publish') as publish:
            with self.assertRaisesRegex(ValueError,'unsafe DNS'):usb.install(PLAN)
        publish.assert_not_called()

    def test_install_and_rerun_reload_only_udev_without_restarting_networks(self):
        path=self.root/'rule.rules'
        with patch.object(usb,'RULE_PATH',path),patch.object(usb.os,'geteuid',return_value=0),patch.object(usb,'profile_issues',return_value=[]),patch.object(usb,'devices',return_value=[]),patch.object(usb,'command',return_value='') as command:
            first=usb.install(PLAN);again=usb.install(PLAN)
        self.assertEqual(first['status'],'installed');self.assertEqual(again['status'],'already_installed')
        self.assertEqual(command.call_args_list.count(unittest.mock.call(['udevadm','control','--reload-rules'])),2)
        self.assertTrue(all(call.args[0][0]=='udevadm' for call in command.call_args_list))
        self.assertFalse(first['wifi_modified']);self.assertFalse(first['networkmanager_restarted'])

    def test_retry_after_reload_failure_reuses_published_identity(self):
        path=self.root/'rule.rules'
        def command(args):
            if args[1]=='control':raise ValueError('temporary reload failure')
            return ''
        with patch.object(usb,'RULE_PATH',path),patch.object(usb.os,'geteuid',return_value=0),patch.object(usb,'profile_issues',return_value=[]),patch.object(usb,'devices',return_value=[]):
            with patch.object(usb,'command',side_effect=command),self.assertRaises(ValueError):usb.install(PLAN)
            self.assertEqual(path.read_text(),usb.rule(PLAN))
            with patch.object(usb,'command',return_value=''):
                self.assertEqual(usb.install(PLAN)['status'],'already_installed')

    def test_other_phone_is_not_selected_and_same_phone_can_change_port(self):
        sys=self.root/'sys';nets=sys/'class/net';nets.mkdir(parents=True)
        devices=sys/'bus/usb/devices';devices.mkdir(parents=True)
        for name,serial in [('3-2','TESTPHONE123'),('3-4','OTHERPHONE')]:
            phone=devices/name;phone.mkdir();(phone/'idVendor').write_text('04e8');(phone/'serial').write_text(serial)
            (phone/'netif').mkdir()
        (nets/'usb-other').mkdir();(nets/'usb-other/device').symlink_to(devices/'3-4/netif')
        self.assertEqual(usb.devices(PLAN,sys),[{'interfaces':[],'tethering_network_present':False}])
        (nets/'enp0s20f0u2').mkdir();(nets/'enp0s20f0u2/device').symlink_to(devices/'3-2/netif')
        self.assertEqual(usb.devices(PLAN,sys)[0]['interfaces'],['enp0s20f0u2'])
        (nets/'enp0s20f0u2/device').unlink();(nets/'enp0s20f0u2').rename(nets/'usb-test')
        (nets/'usb-test/device').symlink_to(devices/'3-2/netif')
        self.assertEqual(usb.devices(PLAN,sys)[0]['interfaces'],['usb-test'])

    def profile(self,changes=None):
        fields={'connection.interface-name':'usb-test','connection.autoconnect':'yes','ipv4.method':'auto',
                'ipv4.route-table':'20040','ipv6.route-table':'20040','ipv4.ignore-auto-dns':'yes','ipv6.ignore-auto-dns':'yes',
                'ipv4.routing-rules':'priority 5190 from 0.0.0.0/0 oif usb-test table 20040',
                'ipv6.routing-rules':'priority 5190 from ::/0 oif usb-test table 20040',
                '802-3-ethernet.mac-address':'','ipv4.dns':'','ipv6.dns':''}
        fields.update(changes or {})
        return lambda args:fields[args[args.index('-g')+1]]

    def test_existing_profile_keeps_ipv4_ipv6_defaults_and_dns_isolated(self):
        with patch.object(usb,'command',side_effect=self.profile()):self.assertEqual(usb.profile_issues(PLAN),[])
        for changes in ({'ipv4.route-table':'254'},{'ipv6.route-table':'254'},
                        {'ipv4.ignore-auto-dns':'no'},{'ipv6.ignore-auto-dns':'no'},
                        {'ipv4.dns':'8.8.8.8'},{'ipv6.dns':'2001:4860:4860::8888'},
                        {'ipv4.routing-rules':'priority 5190 from all table 20040'},
                        {'ipv6.routing-rules':'priority 5190 oif usb-test table 20040, priority 5191 from all table 20040'},
                        {'connection.interface-name':'different-port'},
                        {'802-3-ethernet.mac-address':'00:11:22:33:44:55'}):
            with self.subTest(changes=changes),patch.object(usb,'command',side_effect=self.profile(changes)):
                self.assertTrue(usb.profile_issues(PLAN))

    def test_missing_interface_diagnosis_reports_usb_rename_without_switching_route(self):
        sys=self.root/'sys';nets=sys/'class/net';nets.mkdir(parents=True)
        self.assertIn('no USB tethering network device',network.unavailable_reason('enp0s20f0u4',sys))
        device=sys/'devices/usb3/3-2/interface';device.mkdir(parents=True)
        (nets/'enp0s20f0u2').mkdir();(nets/'enp0s20f0u2/device').symlink_to(device)
        reason=network.unavailable_reason('enp0s20f0u4',sys)
        self.assertIn('enp0s20f0u2',reason);self.assertIn('no automatic route substitution',reason)
        self.assertNotIn('USB network detected',network.unavailable_reason('wlan0',sys))

    def test_rule_verification_failure_cannot_publish_system_configuration(self):
        with patch.object(usb.os,'geteuid',return_value=0),patch.object(usb,'profile_issues',return_value=[]),patch.object(usb,'devices',return_value=[]),patch.object(usb,'command',side_effect=ValueError('invalid udev rule')),patch.object(usb,'publish') as publish:
            with self.assertRaisesRegex(ValueError,'invalid udev rule'):usb.install(PLAN)
        publish.assert_not_called()

    def test_reserved_interface_owned_by_another_device_blocks_install(self):
        with patch.object(usb.os,'geteuid',return_value=0),patch.object(usb,'profile_issues',return_value=[]),patch.object(usb,'devices',return_value=[]),patch.object(Path,'exists',lambda path:str(path)=='/sys/class/net/usb-test'),patch.object(usb,'publish') as publish:
            with self.assertRaisesRegex(ValueError,'another device'):usb.install(PLAN)
        publish.assert_not_called()

    def test_status_separates_phone_present_from_tethering_and_checks_exact_rule(self):
        path=self.root/'installed.rules';path.write_text(usb.rule(PLAN))
        with patch.object(usb,'devices',return_value=[{'interfaces':[],'tethering_network_present':False}]),patch.object(usb,'profile_issues',return_value=[]):
            state=usb.status(PLAN,path)
        self.assertTrue(state['phone_present']);self.assertEqual(state['observed_networks'],[])
        self.assertTrue(state['rule_installed']);self.assertEqual(state['applicant_posts'],0)
