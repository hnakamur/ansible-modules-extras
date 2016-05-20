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
    image:
        description:
          - Specifies the image to use in the format [remote:]image.
          - Required when the container is not created yet and the state is
            not absent.
        required: false
    state:
        choices:
          - started
          - stopped
          - restarted
          - frozen
          - absent
        description:
          - Define the state of a container.
        required: false
        default: started
    ephemeral:
        description:
          - Whether to create an ephemeral container or not.
          - Used only when the container is not created yet.
        required: false
        default: false
    profiles:
        description:
          - Profile list to apply to the container.
          - Used only when the container is not created yet.
          - An empty list means no profile.
        required: false
        default: ['default']
    config:
        description:
          - A dictionary to configure the container.
          - Only used when the container is not created yet.
        required: false
    force:
        description:
          - Whether to force the operation or not.
          - Used only when state is one of restarted, stopped or absent.
          - Means force the shutdown for restarted and stopped, force the removal of the container for absent.
        required: false
        default: false
    force_local:
        description:
          - Whether to force using the local unix socket or not.
        required: false
        default: false
    no_alias:
        description:
          - Whether to ignore aliases for the cantainer for not.
        required: false
        default: false
    timeout_for_addresses:
        description:
          - A timeout of waiting for IPv4 addresses are set to the all network
            interfaces in the container after starting or restarting.
          - If this value is equal to or less than 0, Ansible does not
            wait for IPv4 addresses.
        required: false
        default: 0
requirements:
  - 'lxc command'
notes:
  - Containers must have a unique name. If you attempt to create a container
    with a name that already existed in the users namespace the module will
    simply return as "unchanged".
  - There are two ways to can run commands in containers, using the command
    module or using the ansible lxd connection plugin bundled in Ansible >=
    2.1, the later requires python to be installed in the container which can
    be done with the command module.
  - You can copy a file from the host to the container
    with `command=lxc file push filename container_name/dir/filename`
    on localhost. See the first example below.
  - You can copy a file in the creatd container to the localhost
    with `command=lxc file pull container_name/dir/filename filename`.
    See the first example below.
"""

EXAMPLES = """
- hosts: localhost
  connection: local
  tasks:
    - name: Start the container if it exists. Create and launch the container if not.
      lxd_container:
        name: myubuntu
        image: images:ubuntu/xenial/amd64
        state: started
        timeout_for_addresses: 5
    - name: Install python in the created container "nettest"
      command: lxc exec myubuntu -- apt install -y python
    - name: Copy somefile.txt to /tmp/renamed.txt in the created container "myubuntu"
      command: lxc file push somefile.txt myubuntu/tmp/renamed.txt
    - name: Copy /etc/hosts in the created container "myubuntu" to localhost with name "myubuntu-hosts"
      command: lxc file pull myubuntu/etc/hosts myubuntu-hosts

- hosts: localhost
  connection: local
  tasks:
    - name: Start the container with specified profiles.
      lxd_container:
        name: myubuntu2
        image: images:ubuntu/xenial/amd64
        state: started
        timeout_for_addresses: 5
        profiles:
          - default
          - docker

- hosts: localhost
  connection: local
  tasks:
    - name: Start the container with specified configs.
      lxd_container:
        name: myubuntu3
        image: images:ubuntu/xenial/amd64
        state: started
        timeout_for_addresses: 5
        config:
          security.privileged: true
          security.nesting: true

- hosts: localhost
  connection: local
  tasks:
    - name: Stop the container if it exists. Create, launch and stop the container if not.
      lxd_container:
        name: myubuntu
        image: images:ubuntu/xenial/amd64
        state: stopped

- hosts: localhost
  connection: local
  tasks:
    - name: Restart the container if exists. Create and start the container if not.
      lxd_container:
        name: myubuntu
        image: images:ubuntu/xenial/amd64
        state: restarted
"""

RETURN="""
lxd_container:
  description: container information
  returned: success
  type: object
  contains:
    addresses:
      description: mapping from the network device name to a list of IPv4 addresses in the container
      returned: when state is started or restarted and timeout_for_addresses is long enough for addresses to be set.
      type: object
      sample: {"eth0": ["10.155.92.191"]}
    old_state:
      description: the old state of the container
      returned: when state is started or restarted
      sample: "stopped"
    logs:
      description: list of actions performed for the container
      returned: success
      type: list
      sample: ["launch", "stop"]
"""

from distutils.spawn import find_executable

from requests.exceptions import ConnectionError

# LXD_ANSIBLE_STATES is a map of states that contain values of methods used
# when a particular state is evoked.
LXD_ANSIBLE_STATES = {
    'started': '_started',
    'stopped': '_stopped',
    'restarted': '_restarted',
    'frozen': '_frozen',
    'absent': '_destroyed',
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
        self.image = self.module.params.get('image', None)
        self.state = self.module.params['state']
        self.ephemeral = self.module.params['ephemeral']
        self.profiles = self.module.params['profiles']
        self.config = self.module.params.get('config', None)
        self.force = self.module.params['force']
        self.force_local = self.module.params['force_local']
        self.no_alias = self.module.params['no_alias']
        self.timeout_for_addresses = self.module.params['timeout_for_addresses']
        self.lxc_path = module.params['executable'] or module.get_bin_path('lxc', True)
        self.addresses = None
        self.logs = []

    def _launch_container(self):
        cmd = [self.lxc_path, 'launch']
        if self.force_local:
            cmd.append('--force-local')
        if self.no_alias:
            cmd.append('--no-alias')
        cmd.append(self.image)
        cmd.append(self.container_name)
        if self.ephemeral:
            cmd.append('-e')
        if len(self.profiles) == 0:
            cmd.append('-p')
        else:
            for p in self.profiles:
                cmd.append('-p')
                cmd.append(p)
        if self.config is not None:
            for key, value in self.config.iteritems():
                cmd.append('-c')
                cmd.append('{0}={1}'.format(key, value))
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        self.logs.append('launch')

    def _start_container(self):
        cmd = [self.lxc_path, 'start']
        if self.force_local:
            cmd.append('--force-local')
        if self.no_alias:
            cmd.append('--no-alias')
        cmd.append(self.container_name)
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        self.logs.append('start')

    def _stop_container(self):
        cmd = [self.lxc_path, 'stop']
        if self.force:
            cmd.append('--force')
        if self.force_local:
            cmd.append('--force-local')
        if self.no_alias:
            cmd.append('--no-alias')
        cmd.append(self.container_name)
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        self.logs.append('stop')

    def _restart_container(self):
        cmd = [self.lxc_path, 'restart']
        if self.force:
            cmd.append('--force')
        if self.force_local:
            cmd.append('--force-local')
        if self.no_alias:
            cmd.append('--no-alias')
        cmd.append(self.container_name)
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        self.logs.append('restart')

    def _pause_container(self):
        cmd = [self.lxc_path, 'pause']
        if self.force_local:
            cmd.append('--force-local')
        if self.no_alias:
            cmd.append('--no-alias')
        cmd.append(self.container_name)
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        self.logs.append('pause')

    def _delete_container(self):
        cmd = [self.lxc_path, 'delete']
        if self.force:
            cmd.append('--force')
        if self.force_local:
            cmd.append('--force-local')
        if self.no_alias:
            cmd.append('--no-alias')
        cmd.append(self.container_name)
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        self.logs.append('delete')

    def _get_container_status(self):
        cmd = [self.lxc_path, 'info', self.container_name]
        (rc, out, err) = self.module.run_command(cmd, check_rc=False)
        if rc == 0:
            for line in out.split('\n'):
                if line.startswith("Status: "):
                    return line[len("Status: "):]
        return None

    def _get_container_addresses(self):
        cmd = [self.lxc_path, 'info', self.container_name]
        (rc, out, err) = self.module.run_command(cmd, check_rc=True)
        in_ips = False
        addresses_dict = dict()
        for line in out.split('\n'):
            if line.startswith("Ips:"):
                in_ips = True
            elif in_ips:
                if line.startswith("Resources:"): 
                    in_ips = False
                else:
                    words = line.strip().split()
                    interface = words[0].rstrip(':')
                    family = words[1]
                    address = words[2]
                    if interface != 'lo':
                        addresses = addresses_dict.get(interface, None)
                        if addresses is None:
                            addresses = []
                            addresses_dict[interface] = addresses
                        if family == 'inet':
                            addresses.append(address)
        return addresses_dict

    @staticmethod
    def _has_all_ipv4_addresses(addresses):
        return len(addresses) > 0 and all([len(v) > 0 for v in addresses.itervalues()])

    def _get_addresses(self):
        if self.timeout_for_addresses <= 0:
            return
        due = datetime.datetime.now() + datetime.timedelta(seconds=self.timeout_for_addresses)
        while datetime.datetime.now() < due:
            time.sleep(1)
            addresses = self._get_container_addresses()
            if self._has_all_ipv4_addresses(addresses):
                self.addresses = addresses
                return
        self._on_timeout()

    def _started(self):
        """Ensure a container is started.

        If the container does not exist the container will be created.
        """
        if self.old_state is None:
            self._launch_container()
        else:
            if self.old_state != 'started':
                self._start_container()
        self._get_addresses()

    def _stopped(self):
        if self.old_state is None:
            self._launch_container()
            self._stop_container()
        else:
            if self.old_state == 'frozen':
                self._start_container()
                self._pause_container()
            elif self.old_state != 'stopped':
                self._stop_container()

    def _restarted(self):
        if self.old_state is None:
            self._launch_container()
        else:
            if self.old_state == 'frozen':
                self._start_container()
                self._restart_container()
            elif self.old_state == 'started':
                self._restart_container()
            else:
                self._start_container()
        self._get_addresses()

    def _frozen(self):
        if self.old_state is None:
            self._launch_container()
            self._pause_container()
        else:
            if self.old_state == 'started':
                self._pause_container()
            elif self.old_state == 'stopped':
                self._start_container()
                self._pause_container()

    def _destroyed(self):
        if self.old_state is not None:
            self._delete_container()

    def _on_timeout(self):
        state_changed = len(self.logs) > 0
        self.module.fail_json(
            failed=True,
            msg='timeout for getting addresses',
            changed=state_changed,
            logs=self.logs)

    def run(self):
        """Run the main method."""

        status = self._get_container_status()
        old_state = ANSIBLE_LXD_STATES.get(status, None)
        if status is not None and old_state is None:
            self.module.fail_json(
                failed=True,
                msg='unsupported container status',
                status=status)
        self.old_state = old_state

        action = getattr(self, LXD_ANSIBLE_STATES[self.state])
        action()

        state_changed = len(self.logs) > 0
        result_json = {
            "changed" : state_changed,
            "old_state" : self.old_state,
            "logs" : self.logs
        }
        if self.addresses is not None:
            result_json['addresses'] = self.addresses
        self.module.exit_json(**result_json)


def main():
    """Ansible Main module."""

    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type='str', required=True),
            image=dict(type='str', required=False),
            ephemeral=dict(type='bool', required=False, default=False),
            profiles=dict(type='list', required=False, default=['default']),
            config=dict(type='dict', required=False),
            force=dict(type='bool', required=False, default=False),
            force_local=dict(type='bool', required=False, default=False),
            no_alias=dict(type='bool', required=False, default=False),
            timeout_for_addresses=dict(type='int', default=0),
            state=dict(
                choices=LXD_ANSIBLE_STATES.keys(),
                default='started'
            ),
            executable=dict(required=False, type='path')
        ),
        supports_check_mode=False,
    )

    # We screenscrape a huge amount of git commands so use C locale anytime we
    # call run_command()
    module.run_command_environ_update = dict(LANG='C', LC_ALL='C', LC_MESSAGES='C', LC_CTYPE='C')

    lxd_manage = LxdContainerManagement(module=module)
    lxd_manage.run()


# import module bits
from ansible.module_utils.basic import *
if __name__ == '__main__':
    main()
