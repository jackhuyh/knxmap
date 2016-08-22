import asyncio
import argparse
import binascii
import collections
import codecs
import logging
import os
import socket
import struct
import sys
import time
import functools
try:
    # Python 3.4
    from asyncio import JoinableQueue as Queue
except ImportError:
    # Python 3.5 renamed it to Queue
    from asyncio import Queue

from .core import *
from .messages import *
from .gateway import *
from .bus import *
from .manufacturers import *
from .targets import *

__all__ = ['KnxScanner']

LOGGER = logging.getLogger(__name__)


class KnxScanner:
    """The main scanner instance that takes care of scheduling workers for the targets."""
    def __init__(self, targets=None, max_workers=100, loop=None, ):
        self.loop = loop or asyncio.get_event_loop()
        # The number of concurrent workers for discovering KNXnet/IP gateways
        self.max_workers = max_workers
        # q contains all KNXnet/IP gateways
        self.q = Queue(loop=self.loop)
        # bus_queues is a dict containing a bus queue for each KNXnet/IP gateway
        self.bus_queues = dict()
        # bus_protocols is a list of all bus protocol instances for proper connection shutdown
        self.bus_protocols = list()
        # knx_gateways is a list of KnxTargetReport objects, one for each found KNXnet/IP gateway
        self.knx_gateways = list()
        # bus_devices is a list of KnxBusTargetReport objects, one for each found bus device
        self.bus_devices = set()
        self.bus_info = False
        self.t0 = time.time()
        self.t1 = None
        if targets:
            self.set_targets(targets)
        else:
            self.targets = set()

    def set_targets(self, targets):
        self.targets = targets
        for target in self.targets:
            self.add_target(target)

    def add_target(self, target):
        self.q.put_nowait(target)

    def add_bus_queue(self, gateway, bus_targets):
        self.bus_queues[gateway] = Queue(loop=self.loop)
        for target in bus_targets:
            self.bus_queues[gateway].put_nowait(target)
        return self.bus_queues[gateway]

    @asyncio.coroutine
    def knx_bus_worker(self, transport, protocol, queue):
        """A worker for communicating with devices on the bus."""
        try:
            while True:
                target = queue.get_nowait()
                LOGGER.info('BUS: target: {}'.format(target))
                if not protocol.tunnel_established:
                    LOGGER.error('KNX tunnel is not open!')
                    return

                alive = yield from protocol.tpci_connect(target)

                if alive:
                    # DeviceDescriptorRead
                    tunnel_request = protocol.make_tunnel_request(target)
                    tunnel_request.apci_device_descriptor_read(sequence=protocol.tpci_seq_counts.get(target))
                    descriptor = yield from protocol.send_data(tunnel_request.get_message(), target)

                    if not isinstance(descriptor, KnxTunnellingRequest) or not \
                            descriptor.body.get('cemi').get('apci') == CEMI_APCI_TYPES.get('A_DeviceDescriptor_Response'):
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.tpci_unnumbered_control_data('DISCONNECT')
                        protocol.send_data(tunnel_request.get_message(), target)
                        queue.task_done()
                        continue

                    ret = yield from protocol.tpci_send_ncd(target)
                    if not ret:
                        # TODO: if this is False, can we continue with the KNX connection?
                        LOGGER.error('ERROR OCCURED AFTER READING DEVICE DESCRIPTOR')

                    if isinstance(descriptor, KnxTunnellingRequest) and \
                                descriptor.body.get('cemi').get('apci') == \
                                CEMI_APCI_TYPES.get('A_DeviceDescriptor_Response') and \
                                not self.bus_info:
                            t = KnxBusTargetReport(
                                address=target,
                                type=None,
                                device_serial=None,
                                manufacturer=None)
                            self.bus_devices.add(t)
                            tunnel_request = protocol.make_tunnel_request(target)
                            tunnel_request.tpci_unnumbered_control_data('DISCONNECT')
                            protocol.send_data(tunnel_request.get_message(), target)
                            queue.task_done()
                            continue

                    dev_desc = struct.unpack('!H', descriptor.body.get('cemi').get('data'))[0]
                    manufacturer = None
                    serial = None

                    if dev_desc > 0x13:
                        # System 1 devices do not have interface objects or a serial number
                        # PropertyValueRead
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.apci_property_value_read(
                            sequence=protocol.tpci_seq_counts.get(target),
                            object_index=0,
                            property_id=DEVICE_OBJECTS.get('PID_MANUFACTURER_ID'))
                        manufacturer = yield from protocol.send_data(tunnel_request.get_message(), target)
                        if isinstance(manufacturer, KnxTunnellingRequest):
                            if manufacturer.body.get('cemi').get('data'):
                                manufacturer = manufacturer.body.get('cemi').get('data')[4:]
                            else:
                                LOGGER.info('manufacturer: data not included')
                        else:
                            LOGGER.info('NOT KnxTunnellingRequest: {}'.format(manufacturer))
                    else:
                        # MemoryRead manufacturer ID
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.apci_memory_read(
                            sequence=protocol.tpci_seq_counts.get(target),
                            memory_address=0x0104,
                            read_count=1)
                        manufacturer = yield from protocol.send_data(tunnel_request.get_message(), target)
                        # TODO: check if it returned a proper response
                        if isinstance(manufacturer, KnxTunnellingRequest):
                            if manufacturer.body.get('cemi').get('data'):
                                manufacturer = manufacturer.body.get('cemi').get('data')[2:]
                            else:
                                LOGGER.info('manufacturer: data not included')
                        else:
                            LOGGER.info('NOT KnxTunnellingRequest: {}'.format(manufacturer))

                    ret = yield from protocol.tpci_send_ncd(target)
                    if not ret:
                        manufacturer = 'COULD NOT READ MANUFACTURER'
                    else:
                        if isinstance(manufacturer, (str, bytes)):
                            manufacturer = int.from_bytes(manufacturer, 'big')
                            manufacturer = get_manufacturer_by_id(manufacturer)

                    if dev_desc <= 0x13:
                        # MemoryRead application program
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.apci_memory_read(
                            sequence=protocol.tpci_seq_counts.get(target),
                            memory_address=0x0104,
                            read_count=4)
                        application_program = yield from protocol.send_data(tunnel_request.get_message(), target)
                        if isinstance(application_program, KnxTunnellingRequest):
                            if application_program.body.get('cemi').get('data'):
                                application_program = application_program.body.get('cemi').get('data')[2:]
                            else:
                                LOGGER.info('application_program: data not included')
                        else:
                            LOGGER.info('NOT KnxTunnellingRequest: {}'.format(application_program))
                        yield from protocol.tpci_send_ncd(target)

                    if dev_desc > 0x13:
                        # PropertyValueRead
                        # Read the serial number
                        tunnel_request = protocol.make_tunnel_request(target)
                        tunnel_request.apci_property_value_read(
                            sequence=protocol.tpci_seq_counts.get(target),
                            object_index=0,
                            property_id=DEVICE_OBJECTS.get('PID_SERIAL_NUMBER'))
                        serial = yield from protocol.send_data(tunnel_request.get_message(), target)
                        if isinstance(serial, KnxTunnellingRequest):
                            if serial.body.get('cemi').get('data'):
                                serial = serial.body.get('cemi').get('data')[4:]
                            else:
                                LOGGER.info('serial: data not included')
                        else:
                            LOGGER.info('NOT KnxTunnellingRequest: {}'.format(serial))

                        ret = yield from protocol.tpci_send_ncd(target)
                        if not ret:
                            serial = 'COULD NOT READ SERIAL'
                        else:
                            if isinstance(serial, (str, bytes)):
                                serial = codecs.encode(serial, 'hex').decode().upper()

                        # DEV

                        # PropertyValueRead
                        # tunnel_request = protocol.make_tunnel_request(target)
                        # tunnel_request.apci_property_value_read(
                        #     sequence=protocol.tpci_seq_counts.get(target),
                        #     object_index=2,
                        #     num_elements=1,
                        #     start_index=0,
                        #     property_id=52)
                        # additional = yield from protocol.send_data(tunnel_request.get_message(), target)
                        # print("ADDITIONAL ADDRESSES")
                        # print(additional)
                        # if isinstance(additional, KnxTunnellingRequest):
                        #     print(additional.body)
                        # else:
                        #     LOGGER.info('NOT KnxTunnellingRequest: {}'.format(additional))
                        #
                        # # NCD
                        # ret = yield from protocol.tpci_send_ncd(target)
                        #
                        # if not ret:
                        #     serial = 'COULD NOT READ ADDITIONAL INDIVIDUAL ADDRESSES'
                        # else:
                        #     if isinstance(serial, (str, bytes)):
                        #         serial = codecs.encode(serial, 'hex').decode().upper()


                        # Memory read device state
                        # tunnel_request = protocol.make_tunnel_request(target)
                        # tunnel_request.apci_memory_read(
                        #     sequence=protocol.tpci_seq_counts.get(target),
                        #     memory_address=0x0060,
                        #     read_count=1)
                        # run_state = yield from protocol.send_data(tunnel_request.get_message(), target)
                        # yield from protocol.tpci_send_ncd(target)
                        # if isinstance(run_state, KnxTunnellingRequest):
                        #     if run_state.body.get('cemi').get('data'):
                        #         run_state = run_state.body.get('cemi').get('data')[2:]
                        #     else:
                        #         LOGGER.info('run_state: data not included')
                        # else:
                        #     LOGGER.info('NOT KnxTunnellingRequest: {}'.format(run_state))
                        #
                        # print("RUN STATE")
                        # print(run_state)

                        # for obj in range(1,20):
                        #     for prop in range(1,150):
                        #         tunnel_request = protocol.make_tunnel_request(target)
                        #         tunnel_request.apci_property_value_read(
                        #             sequence=protocol.tpci_seq_counts.get(target),
                        #             object_index=obj,
                        #             start_index=1,
                        #             property_id=prop)
                        #         prop_ret = yield from protocol.send_data(tunnel_request.get_message(), target)
                        #         if isinstance(prop_ret, KnxTunnellingRequest):
                        #             print("--- obj: {}, prop: {}".format(obj, prop))
                        #             if prop_ret.body:
                        #                 #print(prop_ret.body)
                        #                 print("property data: {}".format(prop_ret.body.get('cemi').get('data')[2:]))
                        #             print("---")
                        #         else:
                        #             LOGGER.debug('unknown response for obj: {}, prop: {}'.format(obj, prop))
                        #
                        #         # NCD
                        #         ret = yield from protocol.tpci_send_ncd(target)

                    if descriptor:
                        t = KnxBusTargetReport(
                            address=target,
                            type=DEVICE_DESCRIPTORS.get(dev_desc) or 'Unknown',
                            device_serial=serial or 'Unavailable',
                            manufacturer=manufacturer or 'Unknown')
                        self.bus_devices.add(t)

                    # Properly close the TPCI layer
                    yield from protocol.tpci_disconnect(target)

                queue.task_done()
        except asyncio.CancelledError:
            pass
        except asyncio.QueueEmpty:
            pass

    @asyncio.coroutine
    def bus_scan(self, knx_gateway, bus_targets):
        queue = self.add_bus_queue(knx_gateway.host, bus_targets)
        LOGGER.info('Scanning {} bus device(s) on {}'.format(queue.qsize(), knx_gateway.host))

        # DEV: test configuration request
        # future = asyncio.Future()
        # bus_con = KnxTunnelConnection(future, connection_type=0x03) # DEVICE_MGMT_CONNECTION
        # transport, bus_protocol = yield from self.loop.create_datagram_endpoint(
        #     lambda: bus_con, remote_addr=(knx_gateway.host, knx_gateway.port))
        # self.bus_protocols.append(bus_protocol)
        #
        # connected = yield from future
        # if connected:
        #     conf_req = bus_protocol.make_configuration_request()
        #     print("bla")
        #     print(conf_req)
        #     bla = conf_req.get_message()
        #     print(bla)
        #     print("blubb")
        #     resp = yield from bus_protocol.send_data(conf_req.get_message())
        #     LOGGER.info('CONFIGURATION RESPONSE')
        #     print(resp)

        future = asyncio.Future()
        transport, bus_protocol = yield from self.loop.create_datagram_endpoint(
            functools.partial(KnxTunnelConnection, future),
            remote_addr=(knx_gateway.host, knx_gateway.port))
        self.bus_protocols.append(bus_protocol)

        # Make sure the tunnel has been established
        connected = yield from future

        if connected:
            workers = [asyncio.Task(self.knx_bus_worker(transport, bus_protocol, queue), loop=self.loop)]
            self.t0 = time.time()
            yield from queue.join()
            self.t1 = time.time()
            for w in workers:
                w.cancel()
            bus_protocol.knx_tunnel_disconnect()

        for i in self.bus_devices:
            knx_gateway.bus_devices.append(i)

        LOGGER.info('Bus scan took {} seconds'.format(self.t1 - self.t0))

    @asyncio.coroutine
    def knx_search_worker(self):
        """Send a KnxDescription request to see if target is a KNX device."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, struct.pack('256s', str.encode(self.iface)))

            protocol = KnxGatewaySearch()
            waiter = asyncio.Future(loop=self.loop)
            transport = self.loop._make_datagram_transport(
                sock, protocol, ('224.0.23.12', 3671), waiter)

            try:
                # Wait until connection_made() has been called on the transport
                yield from waiter
            except:
                LOGGER.error('Creating multicast transport failed!')
                transport.close()
                return

            # Wait SEARCH_TIMEOUT seconds for responses to our multicast packets
            yield from asyncio.sleep(self.search_timeout)

            if protocol.responses:
                # If protocol received SEARCH_RESPONSE packets, print them
                for response in protocol.responses:
                    peer = response[0]
                    response = response[1]
                    t = KnxTargetReport(
                        host=peer[0],
                        port=peer[1],
                        mac_address=response.body.get('dib_dev_info').get('knx_mac_address'),
                        knx_address=response.body.get('dib_dev_info').get('knx_address'),
                        device_serial=response.body.get('dib_dev_info').get('knx_device_serial'),
                        friendly_name=response.body.get('dib_dev_info').get('device_friendly_name'),
                        device_status=response.body.get('dib_dev_info').get('device_status'),
                        knx_medium=response.body.get('dib_dev_info').get('knx_medium'),
                        project_install_identifier=response.body.get('dib_dev_info').get('project_install_identifier'),
                        supported_services=[
                            KNX_SERVICES[k] for k, v in
                            response.body.get('dib_supp_sv_families').get('families').items()],
                        bus_devices=[])

                    self.knx_gateways.append(t)
        except asyncio.CancelledError:
            pass

    @asyncio.coroutine
    def search_gateways(self):
        self.t0 = time.time()
        yield from asyncio.ensure_future(asyncio.Task(self.knx_search_worker(), loop=self.loop))
        self.t1 = time.time()
        LOGGER.info('Scan took {} seconds'.format(self.t1 - self.t0))

    @asyncio.coroutine
    def knx_description_worker(self):
        """Send a KnxDescription request to see if target is a KNX device."""
        try:
            while True:
                target = self.q.get_nowait()
                LOGGER.debug('Scanning {}'.format(target))
                for _try in range(self.desc_retries):
                    LOGGER.debug('Sending {}. KnxDescriptionRequest to {}'.format(_try, target))
                    future = asyncio.Future()
                    yield from self.loop.create_datagram_endpoint(
                        functools.partial(KnxGatewayDescription, future, timeout=self.desc_timeout),
                        remote_addr=target)
                    response = yield from future
                    if response:
                        break

                if response and isinstance(response, KnxDescriptionResponse):
                    t = KnxTargetReport(
                        host=target[0],
                        port=target[1],
                        mac_address=response.body.get('dib_dev_info').get('knx_mac_address'),
                        knx_address=response.body.get('dib_dev_info').get('knx_address'),
                        device_serial=response.body.get('dib_dev_info').get('knx_device_serial'),
                        friendly_name=response.body.get('dib_dev_info').get('device_friendly_name'),
                        device_status=response.body.get('dib_dev_info').get('device_status'),
                        knx_medium=response.body.get('dib_dev_info').get('knx_medium'),
                        project_install_identifier=response.body.get('dib_dev_info').get('project_install_identifier'),
                        supported_services=[
                            KNX_SERVICES[k] for k,v in
                            response.body.get('dib_supp_sv_families').get('families').items()],
                        bus_devices=[])

                    self.knx_gateways.append(t)
                self.q.task_done()
        except (asyncio.CancelledError, asyncio.QueueEmpty) as e:
            pass

    @asyncio.coroutine
    def scan(self, targets=None, search_mode=False, search_timeout=5, iface=None,
             desc_timeout=2, desc_retries=2, bus_targets=None, bus_info=False,
             bus_monitor_mode=False, group_monitor_mode=False):
        """The function that will be called by run_until_complete(). This is the main coroutine."""
        if targets:
            self.set_targets(targets)

        if search_mode:
            self.iface = iface
            self.search_timeout = search_timeout
            LOGGER.info('Make sure there are no filtering rules that drop UDP multicast packets!')
            yield from self.search_gateways()
            for t in self.knx_gateways:
                self.print_knx_target(t)
            LOGGER.info('Searching done')

        elif bus_monitor_mode or group_monitor_mode:
            LOGGER.info('Starting bus monitor')
            future = asyncio.Future()
            transport, protocol = yield from self.loop.create_datagram_endpoint(
                functools.partial(KnxBusMonitor, future, group_monitor=group_monitor_mode),
                remote_addr=list(self.targets)[0])
            self.bus_protocols.append(protocol)
            yield from future
            LOGGER.info('Stopping bus monitor')

        else:
            self.desc_timeout = desc_timeout
            self.desc_retries = desc_retries
            workers = [asyncio.Task(self.knx_description_worker(), loop=self.loop)
                       for _ in range(self.max_workers if len(self.targets) > self.max_workers else len(self.targets))]

            self.t0 = time.time()
            yield from self.q.join()
            self.t1 = time.time()
            for w in workers:
                w.cancel()

            if bus_targets and self.knx_gateways:
                self.bus_info = bus_info
                bus_scanners = [asyncio.Task(self.bus_scan(g, bus_targets), loop=self.loop) for g in self.knx_gateways]
                yield from asyncio.wait(bus_scanners)
            else:
                LOGGER.info('Scan took {} seconds'.format(self.t1 - self.t0))

            for t in self.knx_gateways:
                self.print_knx_target(t)

    @staticmethod
    def print_knx_target(knx_target):
        """Print a target of type KnxTargetReport in a well formatted way."""
        # TODO: make this better, and prettier.
        out = dict()
        out[knx_target.host] = collections.OrderedDict()
        o = out[knx_target.host]
        o['Port'] = knx_target.port
        o['MAC Address'] = knx_target.mac_address
        o['KNX Bus Address'] = knx_target.knx_address
        o['KNX Device Serial'] = knx_target.device_serial
        o['KNX Medium'] = KNX_MEDIUMS.get(knx_target.knx_medium)
        o['Device Friendly Name'] = binascii.b2a_qp(knx_target.friendly_name.strip().replace(b'\x00', b'')).decode()
        o['Device Status'] = knx_target.device_status
        o['Project Install Identifier'] = knx_target.project_install_identifier
        o['Supported Services'] = knx_target.supported_services
        if knx_target.bus_devices:
            o['Bus Devices'] = list()

            # Sort the device list based on KNX addresses
            x = dict()
            for i in knx_target.bus_devices:
                x[KnxMessage.pack_knx_address(str(i))] = i
            bus_devices = collections.OrderedDict(sorted(x.items()))

            for k, d in bus_devices.items():
                _d = dict()
                _d[d.address] = collections.OrderedDict()
                if d.type:
                    _d[d.address]['Type'] = d.type
                if d.device_serial:
                    _d[d.address]['Device Serial'] = d.device_serial
                if d.manufacturer:
                    _d[d.address]['Manufacturer'] = d.manufacturer
                o['Bus Devices'].append(_d)

        print()
        def print_fmt(d, indent=0):
            for key, value in d.items():
                if indent is 0:
                    print('   ' * indent + str(key))
                elif isinstance(value, (dict, collections.OrderedDict)):
                    if not len(value.keys()):
                        print('   ' * indent + str(key))
                    else:
                        print('   ' * indent + str(key) + ': ')
                else:
                    print('   ' * indent + str(key) + ': ', end="", flush=True)

                if key == 'Bus Devices':
                    print()
                    for i in value:
                        print_fmt(i, indent + 1)
                elif isinstance(value, list):
                    for i, v in enumerate(value):
                        if i is 0:
                            print()
                        print('   ' * (indent + 1) + str(v))
                elif isinstance(value, (dict, collections.OrderedDict)):
                    print_fmt(value, indent + 1)
                else:
                    print(value)

        print_fmt(out)
        print()
