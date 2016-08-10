"""This module contains various helper classes that make handling targets and sets
of targets and results easiert."""
import logging
import ipaddress
import collections

from .messages import *

__all__ = ['Targets',
           'KnxTargets',
           'BusResultSet',
           'KnxTargetReport',
           'KnxBusTargetReport']

LOGGER = logging.getLogger(__name__)

class Targets:
    """A helper class that expands provided target definitions to a list of tuples."""
    def __init__(self, targets=set(), ports=3671):
        self.targets = set()
        self.ports = set()
        if isinstance(ports, list):
            for p in ports:
                self.ports.add(p)
        elif isinstance(ports, int):
            self.ports.add(ports)
        else:
            self.ports.add(3671)

        if isinstance(targets, (set, list)):
            self._parse(targets)

    def _parse(self, targets):
        """Parse all targets with ipaddress module (with CIDR notation support)."""
        for target in targets:
            try:
                _targets = ipaddress.ip_network(target, strict=False)
            except ValueError:
                LOGGER.error('Invalid target definition, ignoring it: {}'.format(target))
                continue

            if '/' in target:
                _targets = _targets.hosts()

            for _target in _targets:
                for port in self.ports:
                    self.targets.add((str(_target), port))


class KnxTargets:
    """A helper class that expands knx bus targets to lists."""
    def __init__(self, targets):
        self.targets = set()
        if not targets:
            self.targets = None
        elif not '-' in targets and self.is_valid_physical_address(targets):
            self.targets.add(targets)
        else:
            assert isinstance(targets, str)
            if '-' in targets and targets.count('-') < 2:
                # TODO: also parse dashes in octets
                try:
                    f, t = targets.split('-')
                except ValueError:
                    return
                if not self.is_valid_physical_address(f) or \
                        not self.is_valid_physical_address(t):
                    LOGGER.error('Invalid physical address')
                    # TODO: make it group address aware
                elif self.physical_address_to_int(t) <= \
                        self.physical_address_to_int(f):
                    LOGGER.error('From should be smaller then To')
                else:
                    self.targets = self.expand_targets(f, t)

    @staticmethod
    def target_gen(f, t):
        f = KnxMessage.pack_knx_address(f)
        t = KnxMessage.pack_knx_address(t)
        for i in range(f, t + 1):
            yield KnxMessage.parse_knx_address(i)

    @staticmethod
    def expand_targets(f, t):
        ret = set()
        f = KnxMessage.pack_knx_address(f)
        t = KnxMessage.pack_knx_address(t)
        for i in range(f, t + 1):
            ret.add(KnxMessage.parse_knx_address(i))
        return ret

    @staticmethod
    def physical_address_to_int(address):
        parts = address.split('.')
        return (int(parts[0]) << 12) + (int(parts[1]) << 8) + (int(parts[2]))

    @staticmethod
    def int_to_physical_address(address):
        return '{}.{}.{}'.format((address >> 12) & 0xf, (address >> 8) & 0xf, address & 0xff)

    @staticmethod
    def is_valid_physical_address(address):
        assert isinstance(address, str)
        try:
            parts = [int(i) for i in address.split('.')]
        except ValueError:
            return False
        if len(parts) is not 3:
            return False
        if (parts[0] < 1 or parts[0] > 15) or (parts[1] < 0 or parts[1] > 15):
            return False
        if parts[2] < 0 or parts[2] > 255:
            return False
        return True

    @staticmethod
    def is_valid_group_address(address):
        assert isinstance(address, str)
        try:
            parts = [int(i) for i in address.split('/')]
        except ValueError:
            return False
        if len(parts) < 2 or len(parts) > 3:
            return False
        if (parts[0] < 0 or parts[0] > 15) or (parts[1] < 0 or parts[1] > 15):
            return False
        if len(parts) is 3:
            if parts[2] < 0 or parts[2] > 255:
                return False
        return True


class BusResultSet:
    # TODO: implement

    def __init__(self):
        self.targets = collections.OrderedDict()

    def add(self, target):
        """Add a target to the result set, at the right position."""
        pass


class KnxTargetReport:

    def __init__(self, host, port, mac_address, knx_address, device_serial,
                 friendly_name, device_status, knx_medium, project_install_identifier,
                 supported_services, bus_devices):
        self.host = host
        self.port = port
        self.mac_address = mac_address
        self.knx_address = knx_address
        self.device_serial = device_serial
        self.friendly_name = friendly_name
        self.device_status = device_status
        self.knx_medium = knx_medium
        self.project_install_identifier = project_install_identifier
        self.supported_services = supported_services
        self.bus_devices = bus_devices

    def __str__(self):
        return self.host

    def __repr__(self):
        return self.host


class KnxBusTargetReport:

    def __init__(self, address, type, device_serial, manufacturer):
        self.address = address
        self.type = type
        self.device_serial = device_serial
        self.manufacturer = manufacturer

    def __str__(self):
        return self.address

    def __repr__(self):
        return self.address
