#
# Copyright 2014 Red Hat, Inc.
# Copyright 2015 Hewlett-Packard Development Company, L.P.
#
# Author: Endre Karlson <endre.karlson@hp.com>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import math
import uuid

from oslo_config import cfg
from oslo_log import log
import tooz.coordination

from designate.i18n import _LE
from designate.i18n import _LW


LOG = log.getLogger(__name__)

OPTS = [
    cfg.StrOpt('backend_url',
               default=None,
               help='The backend URL to use for distributed coordination. If '
                    'unset services that need coordination will function as '
                    'a standalone service.'),
    cfg.FloatOpt('heartbeat_interval',
                 default=1.0,
                 help='Number of seconds between heartbeats for distributed '
                      'coordination.'),
    cfg.FloatOpt('run_watchers_interval',
                 default=10.0,
                 help='Number of seconds between checks to see if group '
                      'membership has changed')

]
cfg.CONF.register_opts(OPTS, group='coordination')

CONF = cfg.CONF


class CoordinationMixin(object):
    def __init__(self, *args, **kwargs):
        super(CoordinationMixin, self).__init__(*args, **kwargs)

        self._coordination_id = ":".join([CONF.host, str(uuid.uuid4())])
        self._coordinator = None
        if CONF.coordination.backend_url is not None:
            self._init_coordination()
        else:
            msg = _LW("No coordination backend configured, distributed "
                      "coordination functionality will be disabled."
                      " Please configure a coordination backend.")
            LOG.warn(msg)

    def _init_coordination(self):
        backend_url = cfg.CONF.coordination.backend_url
        self._coordinator = tooz.coordination.get_coordinator(
            backend_url, self._coordination_id)
        self._coordination_started = False

        self.tg.add_timer(cfg.CONF.coordination.heartbeat_interval,
                          self._coordinator_heartbeat)
        self.tg.add_timer(cfg.CONF.coordination.run_watchers_interval,
                          self._coordinator_run_watchers)

    def start(self):
        super(CoordinationMixin, self).start()

        if self._coordinator is not None:
            self._coordinator.start()

            self._coordinator.create_group(self.service_name)
            self._coordinator.join_group(self.service_name)

            self._coordination_started = True

    def stop(self):
        if self._coordinator is not None:
            self._coordination_started = False

            self._coordinator.leave_group(self.service_name)
            self._coordinator.stop()

        super(CoordinationMixin, self).stop()

    def _coordinator_heartbeat(self):
        if not self._coordination_started:
            return

        try:
            self._coordinator.heartbeat()
        except tooz.coordination.ToozError:
            LOG.exception(_LE('Error sending a heartbeat to coordination '
                          'backend.'))

    def _coordinator_run_watchers(self):
        if not self._coordination_started:
            return

        self._coordinator.run_watchers()


class Partitioner(object):
    def __init__(self, coordinator, group_id, my_id, partitions):
        self._coordinator = coordinator
        self._group_id = group_id
        self._my_id = my_id
        self._partitions = partitions

        self._started = False
        self._my_partitions = None
        self._callbacks = []

    def _warn_no_backend(self):
        LOG.warning(_LW('No coordination backend configure, assuming we are '
                        'the only worker. Please configure a coordination '
                        'backend'))

    def _get_members(self, group_id):
        get_members_req = self._coordinator.get_members(group_id)
        try:
            return get_members_req.get()
        except tooz.ToozError:
            self.join_group(group_id)

    def _on_group_change(self, event):
        LOG.debug("Received member change %s" % event)
        members, self._my_partitions = self._update_partitions()

        self._run_callbacks(members, event)

    def _partition(self, members, me, partitions):
        member_count = len(members)
        partition_count = len(partitions)
        partition_size = int(
            math.ceil(float(partition_count) / float(member_count)))

        my_index = members.index(me)
        my_start = partition_size * my_index
        my_end = my_start + partition_size

        return partitions[my_start:my_end]

    def _run_callbacks(self, members, event):
        for cb in self._callbacks:
            cb(self.my_partitions, members, event)

    def _update_partitions(self):
        # Recalculate partitions - we need to sort the list of members
        # alphabetically so that it's the same order across all nodes.
        members = sorted(list(self._get_members(self._group_id)))
        partitions = self._partition(
            members, self._my_id, self._partitions)
        return members, partitions

    @property
    def my_partitions(self):
        return self._my_partitions

    def start(self):
        """Allow the partitioner to start timers after the coordinator has
        gotten it's connections up.
        """
        LOG.debug("Starting partitioner")
        if self._coordinator:
            self._coordinator.watch_join_group(
                self._group_id, self._on_group_change)
            self._coordinator.watch_leave_group(
                self._group_id, self._on_group_change)

            # We need to get our partitions now. Events doesn't help in this
            # case since they require state change in the group that we wont
            # get when starting initially
            self._my_partitions = self._update_partitions()[1]
        else:
            self._my_partitions = self._partitions
            self._run_callbacks(None, None)

        self._started = True

    def watch_partition_change(self, callback):
        LOG.debug("Watching for change %s" % self._group_id)
        self._callbacks.append(callback)
        if self._started:
            if not self._coordinator:
                self._warn_no_backend()
            callback(self._my_partitions, None, None)

    def unwatch_partition_change(self, callback):
        self._callbacks.remove(callback)
