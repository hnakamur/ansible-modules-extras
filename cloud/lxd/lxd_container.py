#!/usr/bin/python
# -*- coding: utf-8 -*-

# (c) 2016, Hiroaki Nakamura <hnakamur@gmail.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.


DOCUMENTATION = """
---
module: lxd_container
short_description: Manage LXD Containers
version_added: 2.2.0
description:
  - Management of LXD containers
author: "Hiroaki Nakamura (@hnakamur)"
options:
    name:
        description:
          - Name of a container.
        required: true
    config:
        description:
          - A config dictionary for creating a container.
            See https://github.com/lxc/lxd/blob/master/doc/rest-api.md#post-1
          - Required when the container is not created yet and the state is
            not absent.
        required: false
    state:
        choices:
          - started
          - stopped
          - restarted
          - absent
          - frozen
        description:
          - Define the state of a container.
        required: false
        default: started
    timeout:
        description:
          - A timeout of one LXC REST API call.
          - This is also used as a timeout for waiting until IPv4 addresses
            are set to the all network interfaces in the container after
            starting or restarting.
        required: false
        default: 30
    wait_for_ipv4_addresses:
        description:
          - If this is true, the lxd_module waits until IPv4 addresses
            are set to the all network interfaces in the container after
            starting or restarting.
        required: false
        default: false
    force_stop:
        description:
          - If this is true, the lxd_module forces to stop the container
            when it stops or restarts the container.
        required: false
        default: false
requirements:
  - 'pylxd >= 2.0.2'
notes:
  - Containers must have a unique name. If you attempt to create a container
    with a name that already existed in the users namespace the module will
    simply return as "unchanged".
  - There are two ways to can run commands in containers, using the command
    module or using the ansible lxd connection plugin bundled in Ansible >=
    2.1, the later requires python to be installed in the container which can
    be done with the command module.
  - You can copy a file from the host to the container
    with the Ansible `copy` and `template` module and the `lxd` connection plugin.
    See the example below.
  - You can copy a file in the creatd container to the localhost
    with `command=lxc file pull container_name/dir/filename filename`.
    See the first example below.
"""

EXAMPLES = """
- hosts: localhost
  connection: local
  tasks:
    - name: Create a started container
      lxd_container:
        name: mycontainer
        state: started
        config:
          source:
            type: image
            mode: pull
            server: https://images.linuxcontainers.org
            protocol: lxd
            alias: "ubuntu/xenial/amd64"
          profiles: ["default"]
    - name: Install python in the created container "mycontainer"
      command: lxc exec mycontainer -- apt install -y python
    - name: Copy /etc/hosts in the created container "mycontainer" to localhost with name "mycontainer-hosts"
      command: lxc file pull mycontainer/etc/hosts mycontainer-hosts


# Note your container must be in the inventory for the below example.
#
# [containers]
# mycontainer ansible_connection=lxd
#
- hosts:
    - mycontainer
  tasks:
    - template: src=foo.j2 dest=/etc/bar

- hosts: localhost
  connection: local
  tasks:
    - name: Create a stopped container
      lxd_container:
        name: mycontainer
        state: stopped
        config:
          source:
            type: image
            mode: pull
            server: https://images.linuxcontainers.org
            protocol: lxd
            alias: "ubuntu/xenial/amd64"
          profiles: ["default"]

- hosts: localhost
  connection: local
  tasks:
    - name: Restart a container
      lxd_container:
        name: mycontainer
        state: restarted
        config:
          source:
            type: image
            mode: pull
            server: https://images.linuxcontainers.org
            protocol: lxd
            alias: "ubuntu/xenial/amd64"
          profiles: ["default"]
"""

RETURN="""
lxd_container:
  description: container information
  returned: success
  type: object
  contains:
    addresses:
      description: mapping from the network device name to a list of IPv4 addresses in the container
      returned: when state is started or restarted
      type: object
      sample: {"eth0": ["10.155.92.191"]}
    old_state:
      description: the old state of the container
      returned: when state is started or restarted
      sample: "stopped"
    actions:
      description: list of actions performed for the container
      returned: success
      type: list
      sample: ["create", "start"]
"""

from distutils.spawn import find_executable

try:
    from pylxd.client import Client
    from pylxd.exceptions import ClientConnectionFailed, NotFound
except ImportError:
    HAS_PYLXD = False
else:
    HAS_PYLXD = True

# LXD_ANSIBLE_STATES is a map of states that contain values of methods used
# when a particular state is evoked.
LXD_ANSIBLE_STATES = {
    'started': '_started',
    'stopped': '_stopped',
    'restarted': '_restarted',
    'absent': '_destroyed',
    'frozen': '_frozen'
}

# ANSIBLE_LXD_STATES is a map of states of lxd containers to the Ansible
# lxc_container module state parameter value.
ANSIBLE_LXD_STATES = {
    'Running': 'started',
    'Stopped': 'stopped',
    'Frozen': 'frozen',
}

try:
    callable(all)
except NameError:
    # For python <2.5
    # This definition is copied from https://docs.python.org/2/library/functions.html#all
    def all(iterable):
        for element in iterable:
            if not element:
                return False
        return True

class LxdContainerManagement(object):
    def __init__(self, module):
        """Management of LXC containers via Ansible.

        :param module: Processed Ansible Module.
        :type module: ``object``
        """
        self.module = module
        self.container_name = self.module.params['name']
        self.config = self.module.params.get('config', None)
        self.state = self.module.params['state']
        self.timeout = self.module.params['timeout']
        self.wait_for_ipv4_addresses = self.module.params['wait_for_ipv4_addresses']
        self.force_stop = self.module.params['force_stop']
        self.addresses = None
        try:
            self.client = Client()
        except ClientConnectionFailed:
            self.module.fail_json(msg="Cannot connect to lxd server")
        self.actions = []

    def _create_container(self):
        config = self.config.copy()
        config['name'] = self.container_name
        self.client.containers.create(config, wait=True)
        # NOTE: get container again for the updated state
        self.container = self._get_container()
        self.actions.append('create')

    def _start_container(self):
        self.container.start(wait=True)
        self.actions.append('starte')

    def _stop_container(self):
        self.container.stop(force=self.force_stop, wait=True)
        self.actions.append('stop')

    def _restart_container(self):
        self.container.restart(force=self.force_stop, wait=True)
        self.actions.append('restart')

    def _delete_container(self):
        self.container.delete(wait=True)
        self.actions.append('delete')

    def _freeze_container(self):
        self.container.freeze(wait=True)
        self.actions.append('freeze')

    def _unfreeze_container(self):
        self.container.unfreeze(wait=True)
        self.actions.append('unfreeze')

    def _get_container(self):
        try:
            return self.client.containers.get(self.container_name)
        except NotFound:
            return None
        except ClientConnectionFailed:
            self.module.fail_json(msg="Cannot connect to lxd server")

    @staticmethod
    def _container_to_module_state(container):
        if container is None:
            return "absent"
        else:
            return ANSIBLE_LXD_STATES[container.status]

    def _container_ipv4_addresses(self, ignore_devices=['lo']):
        container = self._get_container()
        network = container is not None and container.state().network or {}
        network = dict((k, v) for k, v in network.iteritems() if k not in ignore_devices) or {}
        addresses = dict((k, [a['address'] for a in v['addresses'] if a['family'] == 'inet']) for k, v in network.iteritems()) or {}
        return addresses

    @staticmethod
    def _has_all_ipv4_addresses(addresses):
        return len(addresses) > 0 and all([len(v) > 0 for v in addresses.itervalues()])

    def _get_addresses(self):
        if not self.wait_for_ipv4_addresses:
            return
        due = datetime.datetime.now() + datetime.timedelta(seconds=self.timeout)
        while datetime.datetime.now() < due:
            time.sleep(1)
            addresses = self._container_ipv4_addresses()
            if self._has_all_ipv4_addresses(addresses):
                self.addresses = addresses
                return
        self._on_timeout()

    def _started(self):
        """Ensure a container is started.

        If the container does not exist the container will be created.
        """
        if self.container is None:
            self._create_container()
            self._start_container()
        else:
            if self.container.status == 'Frozen':
                self._unfreeze_container()
            elif self.container.status == 'Stopped':
                self._start_container()
            if self._needs_to_apply_configs():
                self._apply_configs()
        self._get_addresses()

    def _stopped(self):
        if self.container is None:
            self._create_container()
        else:
            if self.container.status == 'Stopped':
                if self._needs_to_apply_configs():
                    self._start_container()
                    self._apply_configs()
                    self._stop_container()
            else:
                if self.container.status == 'Frozen':
                    self._unfreeze_container()
                if self._needs_to_apply_configs():
                    self._apply_configs()
                self._stop_container()

    def _restarted(self):
        if self.container is None:
            self._create_container()
            self._start_container()
        else:
            if self.container.status == 'Frozen':
                self._unfreeze_container()
            if self._needs_to_apply_configs():
                self._apply_configs()
            self._restart_container()
        self._get_addresses()

    def _destroyed(self):
        if self.container is not None:
            if self.container.status == 'Frozen':
                self._unfreeze_container()
            if self.container.status == 'Running':
                self._stop_container()
            self._delete_container()

    def _frozen(self):
        if self.container is None:
            self._create_container()
            self._start_container()
            self._freeze_container()
        else:
            if self._needs_to_apply_configs():
                if self.container.status == 'Frozen':
                    self._unfreeze_container()
                self._apply_configs()
                self._freeze_container()
            else:
                if self.container.status != 'Frozen':
                    if self.container.status == 'Stopped':
                        self._start_container()
                    self._freeze_container()

    def _on_timeout(self):
        state_changed = len(self.actions) > 0
        self.module.fail_json(
            failed=True,
            msg='timeout for getting addresses',
            changed=state_changed,
            logs=self.actions)

    def _needs_to_apply_configs(self):
        return (
            self._needs_to_apply_config() or
            self._needs_to_apply_devices() or
            self._needs_to_apply_profiles()
        )

    def _needs_to_apply_config(self):
        if 'config' not in self.config:
            return False
        old_configs = dict((k, v) for k, v in self.container.config.items() if not k.startswith('volatile.'))
        return self.config['config'] != old_configs

    def _needs_to_apply_devices(self):
        if 'devices' not in self.config:
            return False
        return self.config['devices'] != self.container.devices

    def _needs_to_apply_profiles(self):
        if 'profiles' not in self.config:
            return False
        return self.config['profiles'] != self.container.profiles

    def _apply_configs(self):
        for k, v in self.config['config'].items():
            self.container.config[k] = v
        if 'devices' in self.config:
            self.container.devices = self.config['devices']
        if 'profiles' in self.config:
            self.container.profiles = self.config['profiles']
        self.container.update()
        self.actions.append('apply_configs')

    def run(self):
        """Run the main method."""

        self.container = self._get_container()
        self.old_state = self._container_to_module_state(self.container)

        action = getattr(self, LXD_ANSIBLE_STATES[self.state])
        action()

        state_changed = len(self.actions) > 0
        result_json = {
            "changed" : state_changed,
            "old_state" : self.old_state,
            "actions" : self.actions
        }
        if self.addresses is not None:
            result_json['addresses'] = self.addresses
        self.module.exit_json(**result_json)


def main():
    """Ansible Main module."""

    module = AnsibleModule(
        argument_spec=dict(
            name=dict(
                type='str',
                required=True
            ),
            config=dict(
                type='dict',
            ),
            state=dict(
                choices=LXD_ANSIBLE_STATES.keys(),
                default='started'
            ),
            timeout=dict(
                type='int',
                default=30
            ),
            wait_for_ipv4_addresses=dict(
                type='bool',
                default=False
            ),
            force_stop=dict(
                type='bool',
                default=False
            )
        ),
        supports_check_mode=False,
    )

    if not HAS_PYLXD:
        module.fail_json(
            msg='The `pylxd` module is not importable. Check the requirements.'
        )

    lxd_manage = LxdContainerManagement(module=module)
    lxd_manage.run()


# import module bits
from ansible.module_utils.basic import *
if __name__ == '__main__':
    main()
